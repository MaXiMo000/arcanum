"""Binder: turns syntax trees into typed query plans with every name resolved.

Names resolve through a chain of levels, one per SELECT scope. A subquery used
as an expression opens a child level that runs once per outer row, so a name
found further up becomes an Outer reference and marks the subquery as
correlated. Derived tables and CTE bodies cannot see the other tables in their
FROM clause (there is no LATERAL), so their lookups skip straight to the
enclosing query's parent.

A SELECT is planned in SQL's logical order:
    FROM -> WHERE -> GROUP BY / aggregates -> HAVING -> window functions
    -> select list (+ hidden ORDER BY keys) -> DISTINCT -> ORDER BY -> LIMIT
"""

import contextlib
import itertools
import math
from dataclasses import dataclass, fields, is_dataclass, replace

from ..errors import ExecutionError, SqlError
from ..sql import nodes as n
from ..sql.parser import parse
from . import plan as p
from .containers import TABLE_FUNCTIONS
from .executor import evaluate_constant
from .expr import (
    BOOLEAN,
    DATE,
    DECIMAL,
    INTEGER,
    INTERVAL,
    JSONB,
    NULL,
    NUMERIC,
    REAL,
    TEMPORAL,
    TEXT,
    TIMESTAMP,
    AggCall,
    Col,
    Const,
    Op,
    Outer,
    SubqueryExpr,
    WindowCall,
    and_all,
    assignable,
    cast,
    children,
    column_indexes,
    conjuncts,
    contains,
    element,
    format_expr,
    is_array,
    literal_type,
    map_children,
    shift,
    to_outer,
    unify,
    walk,
)
from .functions import (
    AGGREGATES,
    FUNCTIONS,
    RANKING,
    STABLE,
    VARIADIC,
    WINDOW_ONLY,
)

_COMPARISONS = {"=", "!=", "<", "<=", ">", ">="}
_SIGNATURE_TYPES = {"N": "NUMERIC", "I": INTEGER, "T": TEXT, "D": DATE, "S": "TIMESTAMP", "V": INTERVAL, "A": "ANY",
                    "R": "ARRAY", "J": JSONB, "B": BOOLEAN}
_SIGNATURE_ALLOWS = {"N": NUMERIC, "I": (INTEGER,), "T": (TEXT,), "D": (DATE,), "S": TEMPORAL, "V": (INTERVAL,),
                     "J": (JSONB,), "B": (BOOLEAN,)}
_RUNTIME_CASTS = {(DATE, TIMESTAMP), (DECIMAL, REAL)}  # conversions Python needs to mix the two at run time
_CONVERTIBLE = (DECIMAL, DATE, TIMESTAMP, INTERVAL)  # types a text literal converts to where one is expected
_TEMPORAL_RESULTS = {  # (op, left, right) -> result type of date and time arithmetic
    ("+", DATE, INTEGER): DATE, ("+", INTEGER, DATE): DATE, ("-", DATE, INTEGER): DATE, ("-", DATE, DATE): INTEGER,
    ("-", TIMESTAMP, TIMESTAMP): INTERVAL, ("-", TIMESTAMP, DATE): INTERVAL, ("-", DATE, TIMESTAMP): INTERVAL,
    ("+", DATE, INTERVAL): TIMESTAMP, ("+", TIMESTAMP, INTERVAL): TIMESTAMP, ("+", INTERVAL, DATE): TIMESTAMP,
    ("+", INTERVAL, TIMESTAMP): TIMESTAMP, ("-", DATE, INTERVAL): TIMESTAMP, ("-", TIMESTAMP, INTERVAL): TIMESTAMP,
    ("+", INTERVAL, INTERVAL): INTERVAL, ("-", INTERVAL, INTERVAL): INTERVAL,
}


@dataclass
class Entry:
    """One column visible in a scope."""
    table: str | None
    name: str
    type: str
    expr: object  # how to read it from the scope's row
    star: bool = True  # included in SELECT *
    unqualified: bool = True  # reachable without a table prefix


@dataclass
class CteInfo:
    plan: object  # None while a recursive CTE's own body is being bound
    columns: list
    work_id: int | None = None


class Level:
    def __init__(self, parent=None, owner=None, cte_parent=None):
        self.parent = parent  # where names not found here are looked up next
        self.owner = owner  # the expression-subquery level this scope belongs to
        self.cte_parent = cte_parent  # where CTE names not found here are looked up next
        self.entries = []
        self.ctes = {}
        self.correlated = False


class Binder:
    def __init__(self, catalog, sql):
        self.catalog = catalog
        self.sql = sql
        self._ids = itertools.count()

    def error(self, message, node=None):
        return SqlError(message, self.sql, getattr(node, "pos", None))

    # ---- statements -------------------------------------------------------------

    def bind_query(self, query):
        return self.query(Level(), query)

    def bind_insert(self, stmt):
        table = self.table(stmt.table)
        if stmt.columns is None:
            positions = list(range(len(table.columns)))
        else:
            positions = [self.column_of(table, name) for name in stmt.columns]
            if len(set(positions)) != len(positions):
                raise self.error("a column is listed twice in INSERT")
        source = self.query(Level(), stmt.source)
        if len(source.columns) != len(positions):
            raise self.error(f"INSERT has {len(positions)} target columns but the values have {len(source.columns)}")
        for pos, column in zip(positions, source.columns, strict=True):
            target = table.columns[pos]
            if not assignable(column.type, target.type):
                raise self.error(f"column {target.name} is {target.type} but the value is {column.type}")
        return table, positions, source

    def bind_update(self, stmt):
        table = self.table(stmt.table)
        level = self.table_level(table)
        where = self.condition(level, stmt.where, "WHERE") if stmt.where is not None else None
        assignments, seen = [], set()
        for name, node in stmt.assignments:
            pos = self.column_of(table, name)
            if pos in seen:
                raise self.error(f"column {name} is assigned twice")
            seen.add(pos)
            value = self.plain_expr(level, node, "UPDATE")
            target = table.columns[pos]
            if not assignable(value.type, target.type):
                raise self.error(f"column {name} is {target.type} but the value is {value.type}", node)
            assignments.append((pos, value))
        return table, where, assignments

    def bind_joined(self, table, from_node, where_node):
        """UPDATE ... FROM and DELETE ... USING. Returns (the target-only part of WHERE, bound against the
        target's rows; the other tables' plan filtered by the rest, evaluated per target row with the target
        row as its outer row; the level that sees both, for SET expressions)."""
        outer = self.table_level(table)
        sub = self.subexpr_level(outer)
        plan, sub.entries = self.from_item(sub, from_node)
        if any(e.table == table.name for e in sub.entries):
            raise self.error(f"table name {table.name} is specified more than once; give one an alias")
        own, rest = [], []
        if where_node is not None:
            for c in conjuncts(self.condition(sub, where_node, "WHERE")):
                if any(isinstance(x, (Col, SubqueryExpr)) for x in walk(c)):
                    rest.append(c)
                else:
                    own.append(_from_outer(c))  # only the target's columns: usable to find target rows
        if rest:
            plan = p.Filter(plan, and_all(rest), plan.columns)
        return and_all(own), plan, sub

    def bind_delete(self, stmt):
        table = self.table(stmt.table)
        level = self.table_level(table)
        return table, self.condition(level, stmt.where, "WHERE") if stmt.where is not None else None

    def table(self, name, node=None):
        table = self.catalog.tables.get(name)
        if table is None:
            if name in self.catalog.views:
                raise self.error(f"{name} is a view; Arcanum's views are read-only", node)
            raise self.error(f"table {name} does not exist", node)
        return table

    def view(self, level, item):
        """A view, expanded in place like a subquery in FROM, with its own column names."""
        stmt = parse(self.catalog.views[item.name])
        expanding = getattr(self, "_expanding", set())
        if item.name in expanding:
            raise self.error(f"infinite recursion: view {item.name} refers to itself", item)
        self._expanding = expanding | {item.name}
        sql, self.sql = self.sql, self.catalog.views[item.name]
        try:
            plan = self.query(self.derived_level(level), stmt.query)
        finally:
            self._expanding, self.sql = expanding, sql
        if stmt.columns:
            if len(stmt.columns) > len(plan.columns):
                raise self.error(f"view {item.name} names more columns than its query has", item)
            names = stmt.columns + [c.name for c in plan.columns[len(stmt.columns):]]
            plan = p.Project(plan, [Col(i, c.type, c.name) for i, c in enumerate(plan.columns)],
                             [p.Column(name, c.type) for name, c in zip(names, plan.columns, strict=True)])
        return plan

    def column_of(self, table, name):
        pos = table.column(name)
        if pos is None:
            raise self.error(f"column {name} does not exist in table {table.name}")
        return pos

    def table_level(self, table):
        level = Level()
        level.entries = [Entry(table.name, c.name, c.type, Col(i, c.type, c.name)) for i, c in enumerate(table.columns)]
        return level

    # ---- queries ------------------------------------------------------------------

    def query(self, level, q):
        if q.with_ is not None:
            self.with_clause(level, q.with_)
        if isinstance(q.body, n.Select):
            return self.select(level, q.body, q.order_by, q.limit, q.offset)
        plan = self.set_body(level, q.body)
        if q.order_by:
            keys = []
            for item in q.order_by:
                k = self.output_position(item.expr, [c.name for c in plan.columns])
                if k is None:
                    raise self.error("ORDER BY after UNION, INTERSECT, EXCEPT or VALUES must name an output "
                                     "column or position", item.expr)
                column = plan.columns[k]
                keys.append((Col(k, column.type, column.name), item.desc, item.nulls_first))
            plan = p.Sort(plan, keys, plan.columns)
        return self.limit(plan, q.limit, q.offset)

    def subexpr_level(self, level):
        sub = Level(parent=level, cte_parent=level)
        sub.owner = sub
        return sub

    def derived_level(self, level):
        return Level(parent=level.parent, owner=level.owner, cte_parent=level)

    def set_body(self, level, body):
        if isinstance(body, n.Select):
            return self.select(level, body, [], None, None)
        if isinstance(body, n.Values):
            return self.values(level, body)
        if isinstance(body, n.Query):
            return self.query(self.derived_level(level), body)
        left = self.set_body(level, body.left)
        right = self.set_body(level, body.right)
        if len(left.columns) != len(right.columns):
            raise self.error(f"each side of {body.op} must have the same number of columns "
                             f"({len(left.columns)} vs {len(right.columns)})")
        columns = []
        for a, b in zip(left.columns, right.columns, strict=True):
            t = unify(a.type, b.type)
            if t is None:
                raise self.error(f"{body.op} cannot combine {a.type} and {b.type} in column {a.name}")
            columns.append(p.Column(a.name, t))
        return p.SetOp(body.op, body.all, left, right, columns)

    def values(self, level, v):
        level.entries = []
        width = len(v.rows[0])
        types = [NULL] * width
        rows = []
        for row in v.rows:
            if len(row) != width:
                raise self.error("all VALUES rows must have the same number of columns")
            rows.append([self.plain_expr(level, e, "VALUES") for e in row])
        for i in range(width):
            # Text literals take the type of the rest of their column, as PostgreSQL's untyped literals do:
            # VALUES (19.99), ('5') is a numeric column.
            for b in rows:
                if not _text_literal(b[i]):
                    t = unify(types[i], b[i].type)
                    if t is None:
                        raise self.error(f"VALUES column {i + 1} mixes {types[i]} and {b[i].type}")
                    types[i] = t
            if any(_text_literal(b[i]) for b in rows):
                if types[i] in (NULL, TEXT):
                    types[i] = TEXT
                else:
                    for b in rows:
                        if _text_literal(b[i]):
                            try:
                                b[i] = Const(cast(b[i].value, types[i]), types[i])
                            except ExecutionError as error:
                                raise self.error(f"VALUES column {i + 1}: {error}") from None
        return p.Values(rows, [p.Column(f"column{i + 1}", t) for i, t in enumerate(types)])

    def limit(self, plan, limit, offset):
        if limit is None and offset is None:
            return plan
        count = self.constant_int(limit, "LIMIT") if limit is not None else None
        skip = self.constant_int(offset, "OFFSET") if offset is not None else 0
        return p.Limit(plan, count, skip or 0, plan.columns)

    def constant_int(self, node, clause):
        e = self.expr(Level(), node)
        if any(isinstance(x, (Col, SubqueryExpr, AggCall, WindowCall)) for x in walk(e)) or e.type not in (INTEGER, NULL):
            raise self.error(f"{clause} must be a constant integer", node)
        value = evaluate_constant(e)
        if value is not None and value < 0:
            raise self.error(f"{clause} must not be negative", node)
        return value

    # ---- WITH ----------------------------------------------------------------------

    def with_clause(self, level, w):
        for cte in w.ctes:
            if cte.name in level.ctes:
                raise self.error(f"CTE {cte.name} is defined twice")
            if w.recursive and _references(cte.query, cte.name):
                level.ctes[cte.name] = self.recursive_cte(level, cte)
            else:
                plan = self.query(self.derived_level(level), cte.query)
                level.ctes[cte.name] = CteInfo(plan, self.cte_columns(cte, plan.columns))

    def cte_columns(self, cte, columns):
        if cte.columns is None:
            return list(columns)
        if len(cte.columns) != len(columns):
            raise self.error(f"CTE {cte.name} names {len(cte.columns)} columns but its query returns {len(columns)}")
        return [p.Column(name, c.type) for name, c in zip(cte.columns, columns, strict=True)]

    def recursive_cte(self, level, cte):
        q, body = cte.query, cte.query.body
        if (not isinstance(body, n.SetOp) or body.op != "UNION" or q.order_by or q.limit is not None
                or q.offset is not None or q.with_ is not None):
            raise self.error(f"recursive CTE {cte.name} must have the form: anchor UNION [ALL] recursive part")
        if _references(body.left, cte.name):
            raise self.error(f"the anchor part of recursive CTE {cte.name} must not refer to {cte.name}")
        anchor = self.set_body(self.derived_level(level), body.left)
        columns = self.cte_columns(cte, anchor.columns)
        work_id = next(self._ids)
        level.ctes[cte.name] = CteInfo(None, columns, work_id)
        recursive = self.set_body(self.derived_level(level), body.right)
        if len(recursive.columns) != len(columns):
            raise self.error(f"the two parts of recursive CTE {cte.name} return different numbers of columns")
        for a, b in zip(columns, recursive.columns, strict=True):
            if not assignable(b.type, a.type):
                raise self.error(f"column {a.name} of recursive CTE {cte.name} is {a.type} in the anchor "
                                 f"but {b.type} in the recursive part")
        return CteInfo(p.RecursiveCTE(anchor, recursive, body.all, work_id, cte.name, columns), columns)

    def find_cte(self, level, name):
        while level is not None:
            if name in level.ctes:
                return level.ctes[name]
            level = level.cte_parent
        return None

    # ---- SELECT ---------------------------------------------------------------------

    def select(self, level, s, order_by, limit, offset):
        plan, level.entries = self.from_item(level, s.from_) if s.from_ is not None else (p.OneRow(), [])
        if s.where is not None:
            plan = p.Filter(plan, self.condition(level, s.where, "WHERE"), plan.columns)

        items = []  # (bound expression, output name)
        for item in s.columns:
            if isinstance(item.expr, n.Star):
                items.extend(self.expand_star(level, item.expr))
            else:
                items.append((self.expr(level, item.expr), item.alias or _default_name(item.expr)))
        groups = [self.group_key(level, g, items) for g in s.group_by]
        distinct_on = [self.order_key(level, e, items) for e in s.distinct_on]
        having = self.condition(level, s.having, "HAVING", aggregates=True) if s.having is not None else None
        order = [(self.order_key(level, o.expr, items), o.desc, o.nulls_first) for o in order_by]

        exprs = [e for e, _ in items]
        order_exprs = [k for k, _, _ in order if not isinstance(k, int)]
        sets = None
        if s.grouping_sets is not None:
            sets = [tuple(sorted({s.group_by.index(g) for g in grouping_set})) for grouping_set in s.grouping_sets]
        if (groups or sets is not None or having is not None
                or any(contains(e, AggCall) for e in exprs + order_exprs)):
            aggs = []

            def rewrite(e):
                return self.rewrite_grouped(e, groups, aggs, sets is not None)

            exprs = [rewrite(e) for e in exprs]
            order = [(k if isinstance(k, int) else rewrite(k), d, nf) for k, d, nf in order]
            distinct_on = [k if isinstance(k, int) else rewrite(k) for k in distinct_on]
            having = rewrite(having) if having is not None else None
            columns = [p.Column(format_expr(e), e.type) for e in groups]
            if sets is not None:
                columns.append(p.Column("grouping", INTEGER))
            columns += [p.Column(format_expr(e), e.type) for e in aggs]
            plan = p.Aggregate(plan, groups, aggs, columns, sets)
            if having is not None:
                plan = p.Filter(plan, having, plan.columns)

        first = None
        if distinct_on:
            on = tuple(exprs[k] if isinstance(k, int) else k for k in distinct_on)
            by = tuple((exprs[k] if isinstance(k, int) else k, d, nf) for k, d, nf in order)
            first = WindowCall("ROW_NUMBER", (), on, by, None, INTEGER)
        calls = [first] if first is not None else []
        for e in exprs + [k for k, _, _ in order if not isinstance(k, int)]:
            for node in walk(e):
                if isinstance(node, WindowCall):
                    if any(contains(c, WindowCall) for c in children(node)):
                        raise self.error("window functions cannot be nested")
                    if node not in calls:
                        calls.append(node)
        if calls:
            base = len(plan.columns)
            plan = p.Window(plan, calls, plan.columns + [p.Column(format_expr(c), c.type) for c in calls])

            def replace_calls(e):
                return _replace_windows(e, calls, base)

            if first is not None:  # DISTINCT ON keeps the first row of each group in ORDER BY order
                plan = p.Filter(plan, Op("=", (Col(base, INTEGER, "row_number"), Const(1, INTEGER)), BOOLEAN),
                                plan.columns)

            exprs = [replace_calls(e) for e in exprs]
            order = [(k if isinstance(k, int) else replace_calls(k), d, nf) for k, d, nf in order]

        columns = [p.Column(name, e.type) for e, (_, name) in zip(exprs, items, strict=True)]
        hidden, keys = [], []
        for k, desc, nulls_first in order:
            if not isinstance(k, int):
                match = next((i for i, e in enumerate(exprs) if e == k), None)
                if match is None:
                    if s.distinct:
                        raise self.error("with SELECT DISTINCT, ORDER BY expressions must appear in the select list")
                    match = len(exprs) + len(hidden)
                    hidden.append(k)
                k = match
            keys.append((k, desc, nulls_first))
        all_columns = columns + [p.Column(format_expr(h), h.type) for h in hidden]
        plan = p.Project(plan, exprs + hidden, all_columns)
        if s.distinct:
            plan = p.Distinct(plan, plan.columns)
        if keys:
            plan = p.Sort(plan, [(Col(k, all_columns[k].type, all_columns[k].name), d, nf) for k, d, nf in keys],
                          plan.columns)
        plan = self.limit(plan, limit, offset)
        if hidden:
            plan = p.Project(plan, [Col(i, c.type, c.name) for i, c in enumerate(columns)], columns)
        return plan

    def expand_star(self, level, star):
        if star.table is not None and not any(e.table == star.table for e in level.entries):
            raise self.error(f"missing FROM entry for table {star.table}", star)
        if star.table is None and not level.entries:
            raise self.error("SELECT * needs a FROM clause", star)
        return [(e.expr, e.name) for e in level.entries if e.star and star.table in (None, e.table)]

    def output_position(self, node, names):
        """ORDER BY/GROUP BY by position (1-based) or by output name; None if neither."""
        if isinstance(node, n.Literal) and type(node.value) is int:
            if not 1 <= node.value <= len(names):
                raise self.error(f"position {node.value} is not in the select list")
            return node.value - 1
        if isinstance(node, n.ColumnRef) and node.table is None:
            matches = [i for i, name in enumerate(names) if name == node.name]
            if len(matches) > 1:
                raise self.error(f"{node.name} is ambiguous: several output columns have that name", node)
            if matches:
                return matches[0]
        return None

    def order_key(self, level, node, items):
        if isinstance(node, n.ColumnRef) and node.table is None:
            matches = [i for i, (_, name) in enumerate(items) if name == node.name]
            if len({items[i][0] for i in matches}) > 1:
                raise self.error(f"ORDER BY {node.name} is ambiguous", node)
            if matches:
                return matches[0]
        position = self.output_position(node, [name for _, name in items]) if isinstance(node, n.Literal) else None
        return position if position is not None else self.expr(level, node)

    def group_key(self, level, node, items):
        e = None
        if isinstance(node, n.Literal) and type(node.value) is int:
            e = items[self.output_position(node, [name for _, name in items])][0]
        elif (isinstance(node, n.ColumnRef) and node.table is None
              and not any(en.name == node.name and en.unqualified for en in level.entries)):
            matches = [ex for ex, name in items if name == node.name]
            if matches:
                e = matches[0]
        if e is None:
            e = self.expr(level, node)
        if contains(e, AggCall) or contains(e, WindowCall):
            raise self.error("aggregate and window functions are not allowed in GROUP BY", node)
        return e

    def rewrite_grouped(self, e, groups, aggs, sets=False):
        """Re-express e over the Aggregate operator's output row: group values, (with grouping sets) the
        GROUPING() bits, then aggregates."""
        for i, g in enumerate(groups):
            if e == g:
                return Col(i, g.type, format_expr(g))
        if isinstance(e, Op) and e.op == "GROUPING_CALL":
            positions = []
            for a in e.args:
                if a not in groups:
                    raise self.error(f"the arguments of grouping must be GROUP BY expressions, not {format_expr(a)}")
                positions.append(groups.index(a))
            if not sets:
                return Const(0, INTEGER)
            return Op("GROUPING", (Col(len(groups), INTEGER, "grouping"), Const(tuple(positions), TEXT),
                                   Const(len(groups), INTEGER)), INTEGER)
        if isinstance(e, AggCall):
            if any(contains(a, AggCall) or contains(a, WindowCall) for a in e.args):
                raise self.error("aggregate function calls cannot be nested")
            if e not in aggs:
                aggs.append(e)
            return Col(len(groups) + sets + aggs.index(e), e.type, format_expr(e))
        if isinstance(e, Col):
            raise self.error(f"column {e.name} must appear in GROUP BY or be used in an aggregate function")
        if isinstance(e, SubqueryExpr) and e.correlated:
            raise self.error("Arcanum doesn't support correlated subqueries in grouped queries yet")
        return map_children(e, lambda c: self.rewrite_grouped(c, groups, aggs, sets))

    # ---- FROM ------------------------------------------------------------------------

    def from_item(self, level, item):
        if isinstance(item, n.TableRef):
            cte = self.find_cte(level, item.name)
            if cte is not None:
                columns = cte.columns
                plan = cte.plan if cte.plan is not None else p.WorkTableScan(cte.work_id, item.name, columns)
            elif item.name in self.catalog.views:
                plan = self.view(level, item)
                columns = plan.columns
            else:
                table = self.table(item.name, item)
                columns = [p.Column(c.name, c.type) for c in table.columns]
                plan = p.TableScan(table, columns)
            alias = item.alias or item.name
            return plan, [Entry(alias, c.name, c.type, Col(i, c.type, c.name)) for i, c in enumerate(columns)]
        if isinstance(item, n.SubqueryRef):
            plan = self.query(self.derived_level(level), item.query)
            return self.aliased(plan, item.alias, item.columns, item)
        if isinstance(item, n.FunctionRef):
            return self.function_scan(self.derived_level(level), item)
        return self.join(level, item)

    def aliased(self, plan, alias, names, node):
        """FROM entries for plan's columns under alias, renamed by AS alias (a, b, ...)."""
        names = list(names or [])
        if len(names) > len(plan.columns):
            raise self.error(f"{alias} has {len(plan.columns)} columns but {len(names)} names were given", node)
        names += [c.name for c in plan.columns[len(names):]]
        return plan, [Entry(alias, name, c.type, Col(i, c.type, name))
                      for i, (name, c) in enumerate(zip(names, plan.columns, strict=True))]

    def function_scan(self, level, item):
        """A set-returning function in FROM; its arguments are bound in level (which sees the FROM items
        before it when it is the right side of a join)."""
        if item.name not in TABLE_FUNCTIONS:
            raise self.error(f"function {item.name.lower()} does not return rows; only unnest, generate_series, "
                             "jsonb_each, jsonb_array_elements, ... can be used in FROM", item)
        args = [self.plain_expr(level, a, "FROM") for a in item.args]
        types = [a.type for a in args]
        _, names, column_types = TABLE_FUNCTIONS[item.name]
        if item.name == "UNNEST":
            args = [self.coerce(a, "TEXT[]", node) if isinstance(a, Const) and a.type == TEXT else a
                    for a, node in zip(args, item.args, strict=True)]
            if not args or not all(is_array(a.type) or a.type == NULL for a in args):
                raise self.error("unnest takes one or more arrays", item)
            column_types = [element(a.type) if is_array(a.type) else TEXT for a in args]
            names = ["unnest"] * len(args)
        elif item.name == "GENERATE_SERIES":
            if len(args) not in (2, 3):
                raise self.error("generate_series takes a start, a stop and an optional step", item)
            if all(t in (INTEGER, NULL) for t in types):
                column_types = [INTEGER]
            elif types[0] in TEMPORAL or types[1] in TEMPORAL:
                args = [self.coerce(a, TIMESTAMP, node) if i < 2 else self.coerce(a, INTERVAL, node)
                        for i, (a, node) in enumerate(zip(args, item.args, strict=True))]
                if len(args) != 3 or args[2].type not in (INTERVAL, NULL):
                    raise self.error("generate_series over timestamps needs an interval step", item)
                column_types = [TIMESTAMP]
            elif all(t in NUMERIC for t in types):
                column_types = [DECIMAL if DECIMAL in types else REAL if REAL in types else INTEGER]
            else:
                raise self.error(f"generate_series({', '.join(types)}) is not supported", item)
        else:
            want = [JSONB] if item.name.startswith(("JSON", "JSONB")) else [TEXT, TEXT]
            if len(args) != len(want):
                raise self.error(f"{item.name.lower()} takes {len(want)} argument{'s' if len(want) > 1 else ''}",
                                 item)
            args = [self.coerce(a, t, node) for a, t, node in zip(args, want, item.args, strict=True)]
            for a, t, node in zip(args, want, item.args, strict=True):
                self.require(a, (t,), node, item.name.lower())
        alias = item.alias or item.name.lower()
        if item.alias and not item.columns and len(column_types) == 1:
            names = [item.alias]  # unnest(tags) AS tag: the column is called tag, as in PostgreSQL
        plan = p.FunctionScan(item.name, args, [p.Column(name, t) for name, t in zip(names, column_types,
                                                                                      strict=True)])
        return self.aliased(plan, alias, item.columns, item)

    def lateral(self, level, item, left_entries):
        """The right side of a join that may refer to the left side's columns (LATERAL, or a function)."""
        left_level = Level(parent=level.parent, owner=level.owner, cte_parent=level)
        left_level.entries = left_entries
        sub = self.subexpr_level(left_level)
        if isinstance(item, n.FunctionRef):
            return self.function_scan(sub, item)
        plan = self.query(sub, item.query)
        return self.aliased(plan, item.alias, item.columns, item)

    def join(self, level, item):
        left, left_entries = self.from_item(level, item.left)
        if isinstance(item.right, n.FunctionRef) or (isinstance(item.right, n.SubqueryRef) and item.right.lateral):
            return self.lateral_join(level, item, left, left_entries)
        right, right_entries = self.from_item(level, item.right)
        repeated = {e.table for e in left_entries if e.table} & {e.table for e in right_entries if e.table}
        if repeated:
            raise self.error(f"table name {sorted(repeated)[0]} appears more than once in FROM; give one an alias")
        width = len(left.columns)
        right_entries = [replace(e, expr=shift(e.expr, width)) for e in right_entries]
        entries = left_entries + right_entries
        conditions = []
        if item.using is not None:
            for name in item.using:
                lefts = [e for e in left_entries if e.name == name and e.unqualified]
                rights = [e for e in right_entries if e.name == name and e.unqualified]
                if len(lefts) != 1 or len(rights) != 1:
                    raise self.error(f"column {name} in USING must appear exactly once on each side of the join")
                left_entry, right_entry = lefts[0], rights[0]
                left_expr, right_expr, t = self.comparable(left_entry.expr, right_entry.expr, None)
                conditions.append(Op("=", (left_expr, right_expr), BOOLEAN))
                merged = {"RIGHT": right_entry.expr,
                          "FULL": Op("COALESCE", (left_entry.expr, right_entry.expr), t)}.get(item.kind, left_entry.expr)
                li = next(i for i, e in enumerate(entries) if e is left_entry)
                entries[li] = replace(left_entry, star=False, unqualified=False)
                entries.insert(li, Entry(None, name, t, merged))
                ri = next(i for i, e in enumerate(entries) if e is right_entry)
                entries[ri] = replace(right_entry, star=False, unqualified=False)
        elif item.on is not None:
            saved, level.entries = level.entries, entries
            conditions.append(self.condition(level, item.on, "ON"))
            level.entries = saved

        left_keys, right_keys, residual = [], [], []
        for c in (part for cond in conditions for part in conjuncts(cond)):
            if isinstance(c, Op) and c.op == "=" and not any(isinstance(x, SubqueryExpr) for x in walk(c)):
                a, b = c.args
                ia, ib = column_indexes(a), column_indexes(b)
                if ia and ib and max(ia) < width <= min(ib):
                    left_keys.append(a)
                    right_keys.append(shift(b, -width))
                    continue
                if ia and ib and max(ib) < width <= min(ia):
                    left_keys.append(b)
                    right_keys.append(shift(a, -width))
                    continue
            residual.append(c)
        plan = p.Join(item.kind, left, right, left_keys, right_keys, and_all(residual), left.columns + right.columns)
        return plan, entries

    def lateral_join(self, level, item, left, left_entries):
        if item.kind not in ("INNER", "LEFT", "CROSS"):
            raise self.error(f"{item.kind} JOIN cannot be LATERAL; use an inner or left join")
        right, right_entries = self.lateral(level, item.right, left_entries)
        repeated = {e.table for e in left_entries if e.table} & {e.table for e in right_entries if e.table}
        if repeated:
            raise self.error(f"table name {sorted(repeated)[0]} appears more than once in FROM; give one an alias")
        width = len(left.columns)
        entries = left_entries + [replace(e, expr=shift(e.expr, width)) for e in right_entries]
        condition = None
        if item.using is not None:
            raise self.error("Arcanum doesn't support USING with LATERAL yet; write ON")
        if item.on is not None:
            saved, level.entries = level.entries, entries
            condition = self.condition(level, item.on, "ON")
            level.entries = saved
        kind = "LEFT" if item.kind == "LEFT" else "INNER"
        return p.LateralJoin(left, right, kind, condition, left.columns + right.columns), entries

    # ---- expressions -----------------------------------------------------------------

    def condition(self, level, node, clause, aggregates=False):
        e = self.expr(level, node)
        if e.type not in (BOOLEAN, NULL):
            raise self.error(f"{clause} must be a boolean condition, not {e.type}", node)
        if contains(e, WindowCall):
            raise self.error(f"window functions are not allowed in {clause}", node)
        if not aggregates and contains(e, AggCall):
            raise self.error(f"aggregate functions are not allowed in {clause}", node)
        return e

    def plain_expr(self, level, node, clause):
        e = self.expr(level, node)
        if contains(e, AggCall) or contains(e, WindowCall):
            raise self.error(f"aggregate and window functions are not allowed in {clause}", node)
        return e

    def expr(self, level, node):
        method = getattr(self, "_bind_" + type(node).__name__, None)
        if method is None:
            raise self.error(f"{type(node).__name__} is not allowed in an expression")
        return method(level, node)

    def require(self, e, allowed, node, what):
        if e.type != NULL and e.type not in allowed:
            names = ", ".join(allowed[:-1]) + " or " + allowed[-1] if len(allowed) > 1 else allowed[0]
            raise self.error(f"{what} expects {names}, not {e.type}", node)

    def comparable(self, a, b, node):
        """a and b made comparable: their common type, with a text literal read as the other side's type
        (d > '2024-01-01'), numeric literals kept exact next to NUMERIC, and dates widened to timestamps.
        Returns (a, b, type)."""
        t = self.common_type(a, b)
        if t is None:
            raise self.error(f"cannot compare {a.type} with {b.type}", node)
        return self.coerce(a, t, node), self.coerce(b, t, node), t

    def common_type(self, a, b):
        for x, y in ((a, b), (b, a)):
            if isinstance(x, Const) and x.type == TEXT and _convertible(y.type):
                return y.type
            if isinstance(x, Const) and x.type in NUMERIC and y.type == DECIMAL:
                return DECIMAL  # price = 9.99: compare exactly (and keep the index usable)
        return unify(a.type, b.type)

    def coerce(self, e, t, node=None):
        """e as a value of type t: constants are converted now, a few pairs at run time, the rest as they are."""
        if e.type in (t, NULL) or t == NULL:
            return e
        if isinstance(e, Const) and ((e.type == TEXT and _convertible(t)) or (e.type, t) in _RUNTIME_CASTS
                                     or (e.type in NUMERIC and t == DECIMAL)):
            try:
                return Const(cast(e.value, t), t)
            except ExecutionError as error:
                raise self.error(str(error), node) from None
        if (e.type, t) in _RUNTIME_CASTS:
            return Op("CAST", (e,), t)
        return e

    def resolve(self, level, ref):
        crossed, depth, current = [], 0, level
        while current is not None:
            if ref.table is None:
                matches = [e for e in current.entries if e.name == ref.name and e.unqualified]
            else:
                matches = [e for e in current.entries if e.table == ref.table and e.name == ref.name]
            if len(matches) > 1:
                raise self.error(f"column reference {ref.name} is ambiguous", ref)
            if matches:
                for crossed_level in crossed:
                    crossed_level.owner.correlated = True
                return matches[0].expr if depth == 0 else to_outer(matches[0].expr, depth)
            if ref.table is not None and any(e.table == ref.table for e in current.entries):
                raise self.error(f"column {ref.table}.{ref.name} does not exist", ref)
            if current.parent is not None:
                crossed.append(current)
                depth += 1
            current = current.parent
        if ref.table is not None:
            raise self.error(f"missing FROM entry for table {ref.table}", ref)
        raise self.error(f"column {ref.name} does not exist", ref)

    def subquery(self, level, query, node):
        sub = self.subexpr_level(level)
        plan = self.query(sub, query)
        return plan, sub.correlated

    def _bind_Literal(self, level, node):
        v = node.value
        if type(v) is int and not -(2**63) <= v < 2**63:
            raise self.error("integer literal is out of the 64-bit range")
        if type(v) is float and not math.isfinite(v):
            raise self.error("number literal is out of range")
        return Const(v, literal_type(v))

    def _bind_ColumnRef(self, level, node):
        return self.resolve(level, node)

    def _bind_Star(self, level, node):
        raise self.error("* can only be used in a select list or COUNT(*)", node)

    def _bind_Unary(self, level, node):
        operand = self.expr(level, node.operand)
        if node.op == "NOT":
            self.require(operand, (BOOLEAN,), node, "NOT")
            return Op("NOT", (operand,), BOOLEAN)
        self.require(operand, (*NUMERIC, INTERVAL), node, f"unary {node.op}")
        return operand if node.op == "+" else Op("NEG", (operand,), operand.type)

    def _bind_Binary(self, level, node):
        op = node.op
        left, right = self.expr(level, node.left), self.expr(level, node.right)
        if op in ("AND", "OR"):
            self.require(left, (BOOLEAN,), node, op)
            self.require(right, (BOOLEAN,), node, op)
            return Op(op, (left, right), BOOLEAN)
        if op in _COMPARISONS:
            left, right, _ = self.comparable(left, right, node)
            return Op(op, (left, right), BOOLEAN)
        container = self.container_operator(op, left, right, node)
        if container is not None:
            return container
        if op in ("~", "~*", "!~", "!~*"):
            self.require(left, (TEXT,), node.left, f"operator {op}")
            self.require(right, (TEXT,), node.right, f"operator {op}")
            return Op(op, (left, right), BOOLEAN)
        if op == "||":
            self.require(left, (TEXT, INTEGER, REAL, DECIMAL, DATE, TIMESTAMP, INTERVAL), node, "||")  # (arrays above)
            self.require(right, (TEXT, INTEGER, REAL, DECIMAL, DATE, TIMESTAMP, INTERVAL), node, "||")
            return Op("||", (left, right), TEXT)
        temporal = self.temporal_arithmetic(op, left, right, node)
        if temporal is not None:
            return temporal
        if op == "^":
            self.require(left, NUMERIC, node.left, "operator ^")
            self.require(right, NUMERIC, node.right, "operator ^")
            exact = DECIMAL in (left.type, right.type)
            return Op("^", (left, right), DECIMAL if exact else REAL)
        allowed = (INTEGER, DECIMAL) if op == "%" else NUMERIC
        self.require(left, allowed, node, f"operator {op}")
        self.require(right, allowed, node, f"operator {op}")
        t = self.common_type(left, right)
        if t == NULL:
            return Op(op, (left, right), NULL)
        return Op(op, (self.coerce(left, t, node), self.coerce(right, t, node)), t)

    def container_operator(self, op, left, right, node):
        """Array and JSONB operators, or None when op is not one of them for these operands."""
        if op in ("->", "->>", "#>", "#>>", "?", "?|", "?&") or (op == "-" and left.type == JSONB):
            left = self.coerce(left, JSONB, node.left)
            self.require(left, (JSONB,), node.left, f"operator {op}")
            if op in ("#>", "#>>", "?|", "?&"):
                right = self.coerce(right, "TEXT[]", node.right)
                if right.type not in ("TEXT[]", NULL):
                    raise self.error(f"operator {op} expects a text array, not {right.type}", node.right)
            elif op == "?":
                self.require(right, (TEXT,), node.right, "operator ?")
            else:
                self.require(right, (TEXT, INTEGER), node.right, f"operator {op}")
            result = {"->": JSONB, "#>": JSONB, "->>": TEXT, "#>>": TEXT, "-": JSONB}.get(op, BOOLEAN)
            return Op("J-" if op == "-" else op, (left, right), result)
        if op in ("@>", "<@", "&&"):
            t = self.common_type(left, right)
            if t is None or not ((t == JSONB and op != "&&") or is_array(t)):
                raise self.error(f"operator {op} does not apply to {left.type} and {right.type}", node)
            return Op(op, (self.coerce(left, t, node.left), self.coerce(right, t, node.right)), BOOLEAN)
        if op == "||" and (JSONB in (left.type, right.type) or is_array(left.type) or is_array(right.type)):
            if JSONB in (left.type, right.type):
                return Op("ARR||", (self.coerce(left, JSONB, node.left), self.coerce(right, JSONB, node.right)), JSONB)
            if is_array(left.type) and is_array(right.type):
                t = unify(left.type, right.type)
            elif is_array(left.type):
                t = left.type if unify(element(left.type), right.type) else None
            else:
                t = right.type if unify(left.type, element(right.type)) else None
            if t is None:
                raise self.error(f"operator || cannot combine {left.type} and {right.type}", node)
            return Op("ARR||", (left, right), t)
        return None

    def _bind_Subscript(self, level, node):
        base, index = self.expr(level, node.expr), self.expr(level, node.index)
        if base.type == JSONB:
            self.require(index, (TEXT, INTEGER), node.index, "a jsonb subscript")
            return Op("->", (base, index), JSONB)
        if not is_array(base.type):
            raise self.error(f"cannot subscript type {base.type}: it is not an array", node.expr)
        self.require(index, (INTEGER,), node.index, "an array subscript")
        return Op("[]", (base, index), element(base.type))

    def _bind_ArrayLiteral(self, level, node):
        items = [self.expr(level, item) for item in node.items]
        if not items:
            raise self.error("cannot tell the type of an empty array; write ARRAY[]::integer[]")
        t = NULL
        for item in items:
            if not (isinstance(item, Const) and item.type == TEXT and t not in (NULL, TEXT)):
                t = unify(t, item.type)
                if t is None:
                    raise self.error(f"ARRAY elements mix {item.type} with other types", node)
        if is_array(t) or t in (INTERVAL, JSONB):
            raise self.error(f"Arcanum doesn't support arrays of {t} yet", node)
        t = TEXT if t == NULL else t
        return Op("ARRAY", tuple(self.coerce(item, t) for item in items), t + "[]")

    def _bind_Quantified(self, level, node):
        left, right = self.expr(level, node.left), self.expr(level, node.right)
        if isinstance(right, Const) and right.type == TEXT:
            right = self.coerce(right, (left.type if left.type != NULL else TEXT) + "[]", node.right)
        if not is_array(right.type):
            raise self.error(f"{'ALL' if node.all else 'ANY'} expects an array, not {right.type}", node.right)
        left, _, _ = self.comparable(left, Const(None, element(right.type)), node.left)
        return Op("ALL" if node.all else "ANY", (left, right, Const(node.op, TEXT)), BOOLEAN)

    def temporal_arithmetic(self, op, left, right, node):
        """Date, timestamp and interval arithmetic, or None when neither side is one of them."""
        types = (left.type, right.type)
        if not any(t in (DATE, TIMESTAMP, INTERVAL) for t in types):
            return None
        # A text literal next to a date or interval is an interval ('1 day') or, for date - '...', a date.
        if isinstance(right, Const) and right.type == TEXT:
            right = self.coerce(right, INTERVAL, node)
        if isinstance(left, Const) and left.type == TEXT:
            left = self.coerce(left, INTERVAL, node)
        types = (left.type, right.type)
        if op in ("*", "/") and INTERVAL in types:
            other = right if left.type == INTERVAL else left
            if other.type not in NUMERIC or (op == "/" and right.type == INTERVAL):
                raise self.error(f"cannot apply {op} to {types[0]} and {types[1]}", node)
            return Op("IV*" if op == "*" else "IV/", (left, right), INTERVAL)
        result = _TEMPORAL_RESULTS.get((op, *types))
        if result is None:
            raise self.error(f"operator {op} does not apply to {types[0]} and {types[1]}", node)
        return Op("DT+" if op == "+" else "DT-", (left, right), result)

    def _bind_IsNull(self, level, node):
        return Op("NOTNULL" if node.negated else "ISNULL", (self.expr(level, node.expr),), BOOLEAN)

    def _bind_IsDistinct(self, level, node):
        left, right, _ = self.comparable(self.expr(level, node.left), self.expr(level, node.right), node)
        return Op("NOTDISTINCT" if node.negated else "DISTINCT", (left, right), BOOLEAN)

    def _bind_Between(self, level, node):
        e, low, high = (self.expr(level, x) for x in (node.expr, node.low, node.high))
        e, low, _ = self.comparable(e, low, node.low)
        e, high, _ = self.comparable(e, high, node.high)
        result = Op("AND", (Op(">=", (e, low), BOOLEAN), Op("<=", (e, high), BOOLEAN)), BOOLEAN)
        return Op("NOT", (result,), BOOLEAN) if node.negated else result

    def _bind_InList(self, level, node):
        needle = self.expr(level, node.expr)
        items = [self.expr(level, item) for item in node.items]
        t = needle.type
        for item, item_node in zip(items, node.items, strict=True):
            t = self.comparable(Const(None, t) if t != needle.type else needle, item, item_node)[2]
        needle = self.coerce(needle, t, node.expr)
        items = [self.coerce(item, t, item_node) for item, item_node in zip(items, node.items, strict=True)]
        result = Op("IN", (needle, *items), BOOLEAN)
        return Op("NOT", (result,), BOOLEAN) if node.negated else result

    def _bind_InSubquery(self, level, node):
        needle = self.expr(level, node.expr)
        plan, correlated = self.subquery(level, node.query, node)
        if len(plan.columns) != 1:
            raise self.error("a subquery after IN must return exactly one column")
        needle = self.comparable(needle, Col(0, plan.columns[0].type), node.expr)[0]
        result = SubqueryExpr("IN", plan, BOOLEAN, needle, correlated)
        return Op("NOT", (result,), BOOLEAN) if node.negated else result

    def _bind_Exists(self, level, node):
        plan, correlated = self.subquery(level, node.query, node)
        return SubqueryExpr("EXISTS", plan, BOOLEAN, None, correlated)

    def _bind_Subquery(self, level, node):
        plan, correlated = self.subquery(level, node.query, node)
        if len(plan.columns) != 1:
            raise self.error("a subquery used as a value must return exactly one column")
        return SubqueryExpr("SCALAR", plan, plan.columns[0].type, None, correlated)

    def _bind_Like(self, level, node):
        e, pattern = self.expr(level, node.expr), self.expr(level, node.pattern)
        word = "ILIKE" if node.insensitive else "LIKE"
        self.require(e, (TEXT,), node.expr, word)
        self.require(pattern, (TEXT,), node.pattern, word)
        result = Op(word, (e, pattern), BOOLEAN)
        return Op("NOT", (result,), BOOLEAN) if node.negated else result

    def _bind_Case(self, level, node):
        operand = self.expr(level, node.operand) if node.operand is not None else None
        args, t = [], NULL
        for when, result in node.whens:
            condition = self.expr(level, when)
            if operand is not None:
                left, right, _ = self.comparable(operand, condition, when)
                condition = Op("=", (left, right), BOOLEAN)
            else:
                self.require(condition, (BOOLEAN,), when, "CASE WHEN")
            value = self.expr(level, result)
            t = self._unify_branch(t, value, result)
            args += [condition, value]
        default = self.expr(level, node.else_) if node.else_ is not None else Const(None, NULL)
        t = self._unify_branch(t, default, node.else_)
        args[1::2] = [self.coerce(v, t) for v in args[1::2]]
        return Op("CASE", (*args, self.coerce(default, t)), t)

    def _unify_branch(self, t, value, node):
        result = unify(t, value.type)
        if result is None:
            raise self.error(f"CASE results mix {t} and {value.type}", node)
        return result

    def _bind_Cast(self, level, node):
        if isinstance(node.expr, n.ArrayLiteral) and not node.expr.items:
            if not is_array(node.type):
                raise self.error(f"cannot cast an empty array to {node.type}", node)
            return Const((), node.type)
        e = self.expr(level, node.expr)
        if node.type == INTERVAL and e.type not in (TEXT, INTERVAL, NULL):
            raise self.error(f"cannot cast {e.type} to INTERVAL", node.expr)
        result = Op("CAST", (e,), node.type)
        if isinstance(e, Const):  # constant: convert now when it can be (a bad one fails when it runs)
            with contextlib.suppress(ExecutionError):
                result = Const(None if e.value is None else cast(e.value, node.type), node.type)
        if node.modifiers:  # NUMERIC(p, s): round to s digits after the point
            result = Op("ROUND", (result, Const(node.modifiers[1], INTEGER)), DECIMAL)
        return result

    def _bind_Call(self, level, node):
        name = node.name
        if node.over is not None:
            return self.window_call(level, node)
        if name in AGGREGATES:
            return self.aggregate(level, node)
        if name in WINDOW_ONLY:
            raise self.error(f"{name.lower()} requires an OVER clause", node)
        if node.star or node.distinct:
            raise self.error(f"{name.lower()}() does not accept * or DISTINCT", node)
        if node.order_by or node.filter is not None:
            raise self.error(f"ORDER BY and FILTER only apply to aggregate functions, not {name.lower()}", node)
        args = [self.expr(level, a) for a in node.args]
        if name in ("COALESCE", "IFNULL", "GREATEST", "LEAST"):
            if not args or (name == "IFNULL" and len(args) != 2):
                raise self.error(f"{name.lower()} needs {'two arguments' if name == 'IFNULL' else 'arguments'}", node)
            t = NULL
            for a, a_node in zip(args, node.args, strict=True):
                t = unify(t, a.type) if not (isinstance(a, Const) and a.type == TEXT and t in _CONVERTIBLE) else t
                if t is None:
                    raise self.error(f"{name.lower()} arguments mix incompatible types", a_node)
            args = [self.coerce(a, t, a_node) for a, a_node in zip(args, node.args, strict=True)]
            return Op("COALESCE" if name == "IFNULL" else name, tuple(args), t)
        if name in VARIADIC:
            return Op(name, tuple(args), JSONB)
        if name == "GROUPING":
            if not args:
                raise self.error("grouping needs at least one argument", node)
            return Op("GROUPING_CALL", tuple(args), INTEGER)
        if name in ("CONCAT", "CONCAT_WS"):
            if not args or (name == "CONCAT_WS" and len(args) < 2):
                raise self.error(f"{name.lower()} needs {'a separator and values' if name == 'CONCAT_WS' else 'arguments'}",
                                 node)
            return Op(name, tuple(args), TEXT)
        if name == "NULLIF":
            if len(args) != 2:
                raise self.error("nullif needs two arguments", node)
            a, b, _ = self.comparable(args[0], args[1], node)
            return Op("NULLIF", (a, b), args[0].type)
        # date_part / EXTRACT and date_trunc take a field name; a timestamp field given as text converts.
        stamps = {"DATE_PART": (1,), "DATE_TRUNC": (1,), "TO_CHAR": (0,), "AGE": (0, 1)}.get(name, ())
        args = [self.coerce(a, TIMESTAMP, a_node) if i in stamps and isinstance(a, Const) and a.type == TEXT else a
                for i, (a, a_node) in enumerate(zip(args, node.args, strict=True))]
        spec = FUNCTIONS.get(name)
        if spec is None:
            if name in TABLE_FUNCTIONS:
                raise self.error(f"{name.lower()}() returns rows: use it in FROM, as in SELECT ... FROM "
                                 f"{name.lower()}(...) AS t", node)
            raise self.error(f"function {name.lower()} does not exist", node)
        signatures, result, _ = spec
        # A text literal where every signature wants jsonb or an array reads as one ('{a,b}' as a path).
        for i, a in enumerate(args):
            if isinstance(a, Const) and a.type == TEXT:
                wanted = {sig[i] for sig in signatures if i < len(sig)}
                if wanted == {"J"}:
                    args[i] = self.coerce(a, JSONB, node.args[i])
                elif wanted == {"R"}:
                    args[i] = self.coerce(a, "TEXT[]", node.args[i])
        types = [a.type for a in args]
        if not any(_matches(sig, types) for sig in signatures):
            expected = " or ".join(f"{name.lower()}({', '.join(_SIGNATURE_TYPES[c] for c in sig)})" for sig in signatures)
            raise self.error(f"{name.lower()}({', '.join(types)}) does not match {expected}", node)
        if name in STABLE:
            return Op(name, (), result)
        types = [a.type for a in args]
        t = types[0] if result == "same" else types[1] if result == "array" else result
        if name in ("ARRAY_APPEND", "ARRAY_PREPEND", "ARRAY_REMOVE", "ARRAY_POSITION") and NULL not in types:
            array, item = (types[1], types[0]) if name == "ARRAY_PREPEND" else (types[0], types[1])
            if unify(element(array), item) is None:
                raise self.error(f"{name.lower()}: {item} does not go in {array}", node)
        if name == "ARRAY_CAT" and unify(types[0], types[1]) is None:
            raise self.error(f"array_cat cannot combine {types[0]} and {types[1]}", node)
        if name in ("ROUND", "TRUNC") and types[0] == DECIMAL:
            t = DECIMAL  # exact in, exact out
        if name == "DATE_PART":
            t = _date_part_type(args[0])
        return Op(name, tuple(args), t)

    def aggregate(self, level, node):
        name = node.name
        order = tuple((self.expr(level, o.expr), o.desc, o.nulls_first) for o in node.order_by)
        keep = self.condition(level, node.filter, "FILTER") if node.filter is not None else None
        if any(contains(x, AggCall) for x, _, _ in order) or (keep is not None and contains(keep, AggCall)):
            raise self.error("aggregate function calls cannot be nested", node)
        if node.distinct and order and any(x not in [self.expr(level, a) for a in node.args] for x, _, _ in order):
            raise self.error("in an aggregate with DISTINCT, ORDER BY expressions must appear in the argument list",
                             node)
        if node.star:
            if name != "COUNT":
                raise self.error(f"{name.lower()}(*) is not valid; only count(*) is", node)
            return AggCall("COUNT", (), False, INTEGER, order, keep)
        if name in ("JSONB_OBJECT_AGG", "JSON_OBJECT_AGG"):
            if len(node.args) != 2:
                raise self.error(f"{name.lower()} takes a key and a value", node)
            args = tuple(self.expr(level, a) for a in node.args)
            self.require(args[0], (TEXT,), node.args[0], name.lower())
            return AggCall(name, args, node.distinct, JSONB, order, keep)
        if name == "STRING_AGG":
            if len(node.args) != 2:
                raise self.error("string_agg takes a value and a delimiter", node)
            args = tuple(self.expr(level, a) for a in node.args)
            for a, a_node in zip(args, node.args, strict=True):
                self.require(a, (TEXT,), a_node, "string_agg")
            return AggCall(name, args, node.distinct, TEXT, order, keep)
        if len(node.args) != 1:
            raise self.error(f"{name.lower()} takes exactly one argument", node)
        arg = self.expr(level, node.args[0])
        if name in ("SUM", "AVG"):
            self.require(arg, NUMERIC, node, name.lower())
        if name in ("BOOL_AND", "BOOL_OR", "EVERY"):
            self.require(arg, (BOOLEAN,), node, name.lower())
        if name == "ARRAY_AGG":
            if is_array(arg.type) or arg.type in (INTERVAL, JSONB):
                raise self.error(f"Arcanum doesn't support array_agg over {arg.type} yet", node)
            return AggCall(name, (arg,), node.distinct, (TEXT if arg.type == NULL else arg.type) + "[]", order, keep)
        if name in ("JSONB_AGG", "JSON_AGG"):
            return AggCall(name, (arg,), node.distinct, JSONB, order, keep)
        t = INTEGER if name == "COUNT" else (DECIMAL if arg.type == DECIMAL else REAL) if name == "AVG" else arg.type
        return AggCall(name, (arg,), node.distinct, t, order, keep)

    def window_call(self, level, node):
        name, spec = node.name, node.over
        if name not in WINDOW_ONLY and name not in AGGREGATES:
            raise self.error(f"{name.lower()} is not a window function", node)
        if node.distinct:
            raise self.error("Arcanum doesn't support DISTINCT in window functions yet", node)
        if node.order_by or node.filter is not None or name in ("STRING_AGG", "ARRAY_AGG", "JSONB_AGG", "JSON_AGG",
                                                                 "JSONB_OBJECT_AGG", "JSON_OBJECT_AGG"):
            raise self.error(f"Arcanum doesn't support {name.lower()} with ORDER BY, FILTER or as a window function yet",
                             node)
        partition = tuple(self.plain_expr_or_agg(level, e) for e in spec.partition_by)
        order = tuple((self.plain_expr_or_agg(level, o.expr), o.desc, o.nulls_first) for o in spec.order_by)
        frame = self.frame(spec.frame) if spec.frame is not None else None
        if name in AGGREGATES:
            agg = self.aggregate(level, n.Call(name, node.args, star=node.star, pos=node.pos))
            args, t = agg.args, agg.type
        elif name in RANKING:
            if node.args or node.star:
                raise self.error(f"{name.lower()} takes no arguments", node)
            args, t = (), INTEGER
        elif name in ("LAG", "LEAD"):
            if not 1 <= len(node.args) <= 3:
                raise self.error(f"{name.lower()} takes one to three arguments", node)
            args = tuple(self.expr(level, a) for a in node.args)
            if len(args) > 1 and not (isinstance(args[1], Const) and args[1].type == INTEGER and args[1].value >= 0):
                raise self.error(f"the offset of {name.lower()} must be a non-negative integer constant", node.args[1])
            t = args[0].type
            if len(args) == 3:
                t = unify(t, args[2].type)
                if t is None:
                    raise self.error(f"the default of {name.lower()} does not match its value type", node.args[2])
        else:
            if len(node.args) != 1:
                raise self.error(f"{name.lower()} takes exactly one argument", node)
            args = (self.expr(level, node.args[0]),)
            t = args[0].type
        return WindowCall(name, tuple(args), partition, order, frame, t)

    def plain_expr_or_agg(self, level, node):
        e = self.expr(level, node)
        if contains(e, WindowCall):
            raise self.error("window functions cannot be nested", node)
        return e

    def frame(self, f):
        def bound(b):
            if b.kind in ("PRECEDING", "FOLLOWING"):
                if f.mode == "RANGE":
                    raise self.error("Arcanum doesn't support RANGE frames with an offset yet; use ROWS")
                offset = self.expr(Level(), b.offset)
                if not (isinstance(offset, Const) and offset.type == INTEGER and offset.value >= 0):
                    raise self.error("window frame offsets must be non-negative integer constants")
                return b.kind, offset.value
            return b.kind, None

        start, end = bound(f.start), bound(f.end)
        if start[0] == "UNBOUNDED FOLLOWING" or end[0] == "UNBOUNDED PRECEDING":
            raise self.error("invalid window frame bounds")
        return (f.mode, *start, *end)


def _matches(signature, types):
    if len(signature) != len(types):
        return False
    return all(t == NULL or c == "A" or (is_array(t) if c == "R" else t in _SIGNATURE_ALLOWS[c])
               for c, t in zip(signature, types, strict=True))


def _convertible(t):
    """Types a text literal converts to where one is expected, as PostgreSQL's untyped literals do."""
    return t in _CONVERTIBLE or t == JSONB or is_array(t)


def _from_outer(e):
    """Turn references to the enclosing row (depth 1) back into columns of the row itself."""
    if isinstance(e, Outer) and e.depth == 1:
        return Col(e.index, e.type, e.name)
    return map_children(e, _from_outer)


def _text_literal(e):
    return isinstance(e, Const) and e.type == TEXT


def _date_part_type(field):
    """EXTRACT(year ...) is a whole number; seconds and epochs have fractions."""
    from .datetimes import FIELDS, INTEGRAL_FIELDS  # noqa: PLC0415 (only needed here)

    if isinstance(field, Const) and isinstance(field.value, str):
        name = field.value.lower()
        if name not in FIELDS:
            raise SqlError(f"unit {name!r} is not recognized; use one of {', '.join(sorted(FIELDS))}")
        return INTEGER if name in INTEGRAL_FIELDS else REAL
    return REAL


def _replace_windows(e, calls, base):
    if isinstance(e, WindowCall):
        return Col(base + calls.index(e), e.type, format_expr(e))
    return map_children(e, lambda c: _replace_windows(c, calls, base))


def _default_name(node):
    if isinstance(node, n.ColumnRef):
        return node.name
    if isinstance(node, n.Call):
        return node.name.lower()
    if isinstance(node, n.Cast):
        return _default_name(node.expr)
    return "?column?"


def _references(node, name):
    """Whether a syntax tree mentions table `name` anywhere in a FROM clause."""
    if isinstance(node, n.TableRef):
        return node.name == name
    if isinstance(node, (list, tuple)):
        return any(_references(x, name) for x in node)
    if is_dataclass(node):
        return any(_references(getattr(node, f.name), name) for f in fields(node))
    return False
