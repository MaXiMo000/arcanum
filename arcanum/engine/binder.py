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

import itertools
import math
from dataclasses import dataclass, fields, is_dataclass, replace

from ..errors import SqlError
from ..sql import nodes as n
from . import plan as p
from .executor import evaluate_constant
from .expr import (
    AGGREGATES,
    BOOLEAN,
    FUNCTIONS,
    INTEGER,
    NULL,
    NUMERIC,
    RANKING,
    REAL,
    TEXT,
    WINDOW_ONLY,
    AggCall,
    Col,
    Const,
    Op,
    SubqueryExpr,
    WindowCall,
    and_all,
    assignable,
    children,
    column_indexes,
    conjuncts,
    contains,
    format_expr,
    literal_type,
    map_children,
    shift,
    to_outer,
    unify,
    walk,
)

_COMPARISONS = {"=", "!=", "<", "<=", ">", ">="}
_SIGNATURE_TYPES = {"N": "NUMERIC", "I": INTEGER, "T": TEXT, "A": "ANY"}


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

    def bind_delete(self, stmt):
        table = self.table(stmt.table)
        level = self.table_level(table)
        return table, self.condition(level, stmt.where, "WHERE") if stmt.where is not None else None

    def table(self, name, node=None):
        table = self.catalog.tables.get(name)
        if table is None:
            raise self.error(f"table {name} does not exist", node)
        return table

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
            bound = [self.plain_expr(level, e, "VALUES") for e in row]
            for i, b in enumerate(bound):
                t = unify(types[i], b.type)
                if t is None:
                    raise self.error(f"VALUES column {i + 1} mixes {types[i]} and {b.type}")
                types[i] = t
            rows.append(bound)
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
        having = self.condition(level, s.having, "HAVING", aggregates=True) if s.having is not None else None
        order = [(self.order_key(level, o.expr, items), o.desc, o.nulls_first) for o in order_by]

        exprs = [e for e, _ in items]
        order_exprs = [k for k, _, _ in order if not isinstance(k, int)]
        if groups or having is not None or any(contains(e, AggCall) for e in exprs + order_exprs):
            aggs = []

            def rewrite(e):
                return self.rewrite_grouped(e, groups, aggs)

            exprs = [rewrite(e) for e in exprs]
            order = [(k if isinstance(k, int) else rewrite(k), d, nf) for k, d, nf in order]
            having = rewrite(having) if having is not None else None
            columns = [p.Column(format_expr(e), e.type) for e in groups + aggs]
            plan = p.Aggregate(plan, groups, aggs, columns)
            if having is not None:
                plan = p.Filter(plan, having, plan.columns)

        calls = []
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

    def rewrite_grouped(self, e, groups, aggs):
        """Re-express e over the Aggregate operator's output row: group values, then aggregates."""
        for i, g in enumerate(groups):
            if e == g:
                return Col(i, g.type, format_expr(g))
        if isinstance(e, AggCall):
            if any(contains(a, AggCall) or contains(a, WindowCall) for a in e.args):
                raise self.error("aggregate function calls cannot be nested")
            if e not in aggs:
                aggs.append(e)
            return Col(len(groups) + aggs.index(e), e.type, format_expr(e))
        if isinstance(e, Col):
            raise self.error(f"column {e.name} must appear in GROUP BY or be used in an aggregate function")
        if isinstance(e, SubqueryExpr) and e.correlated:
            raise self.error("Arcanum doesn't support correlated subqueries in grouped queries yet")
        return map_children(e, lambda c: self.rewrite_grouped(c, groups, aggs))

    # ---- FROM ------------------------------------------------------------------------

    def from_item(self, level, item):
        if isinstance(item, n.TableRef):
            cte = self.find_cte(level, item.name)
            if cte is not None:
                columns = cte.columns
                plan = cte.plan if cte.plan is not None else p.WorkTableScan(cte.work_id, item.name, columns)
            else:
                table = self.table(item.name, item)
                columns = [p.Column(c.name, c.type) for c in table.columns]
                plan = p.TableScan(table, columns)
            alias = item.alias or item.name
            return plan, [Entry(alias, c.name, c.type, Col(i, c.type, c.name)) for i, c in enumerate(columns)]
        if isinstance(item, n.SubqueryRef):
            plan = self.query(self.derived_level(level), item.query)
            return plan, [Entry(item.alias, c.name, c.type, Col(i, c.type, c.name)) for i, c in enumerate(plan.columns)]
        return self.join(level, item)

    def join(self, level, item):
        left, left_entries = self.from_item(level, item.left)
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
                t = self.comparable(left_entry.expr, right_entry.expr, None)
                conditions.append(Op("=", (left_entry.expr, right_entry.expr), BOOLEAN))
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
            raise self.error(f"{what} expects {' or '.join(allowed)}, not {e.type}", node)

    def comparable(self, a, b, node):
        t = unify(a.type, b.type)
        if t is None:
            raise self.error(f"cannot compare {a.type} with {b.type}", node)
        return t

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
        self.require(operand, NUMERIC, node, f"unary {node.op}")
        return operand if node.op == "+" else Op("NEG", (operand,), operand.type)

    def _bind_Binary(self, level, node):
        op = node.op
        left, right = self.expr(level, node.left), self.expr(level, node.right)
        if op in ("AND", "OR"):
            self.require(left, (BOOLEAN,), node, op)
            self.require(right, (BOOLEAN,), node, op)
            return Op(op, (left, right), BOOLEAN)
        if op in _COMPARISONS:
            self.comparable(left, right, node)
            return Op(op, (left, right), BOOLEAN)
        if op == "||":
            self.require(left, (TEXT, INTEGER, REAL), node, "||")
            self.require(right, (TEXT, INTEGER, REAL), node, "||")
            return Op("||", (left, right), TEXT)
        allowed = (INTEGER,) if op == "%" else NUMERIC
        self.require(left, allowed, node, f"operator {op}")
        self.require(right, allowed, node, f"operator {op}")
        types = (left.type, right.type)
        t = REAL if REAL in types else INTEGER if INTEGER in types else NULL
        return Op(op, (left, right), t)

    def _bind_IsNull(self, level, node):
        return Op("NOTNULL" if node.negated else "ISNULL", (self.expr(level, node.expr),), BOOLEAN)

    def _bind_Between(self, level, node):
        e, low, high = (self.expr(level, x) for x in (node.expr, node.low, node.high))
        self.comparable(e, low, node.low)
        self.comparable(e, high, node.high)
        result = Op("AND", (Op(">=", (e, low), BOOLEAN), Op("<=", (e, high), BOOLEAN)), BOOLEAN)
        return Op("NOT", (result,), BOOLEAN) if node.negated else result

    def _bind_InList(self, level, node):
        needle = self.expr(level, node.expr)
        items = [self.expr(level, item) for item in node.items]
        for item, item_node in zip(items, node.items, strict=True):
            self.comparable(needle, item, item_node)
        result = Op("IN", (needle, *items), BOOLEAN)
        return Op("NOT", (result,), BOOLEAN) if node.negated else result

    def _bind_InSubquery(self, level, node):
        needle = self.expr(level, node.expr)
        plan, correlated = self.subquery(level, node.query, node)
        if len(plan.columns) != 1:
            raise self.error("a subquery after IN must return exactly one column")
        self.comparable(needle, plan.columns[0], node.expr)
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
        self.require(e, (TEXT,), node.expr, "LIKE")
        self.require(pattern, (TEXT,), node.pattern, "LIKE")
        result = Op("LIKE", (e, pattern), BOOLEAN)
        return Op("NOT", (result,), BOOLEAN) if node.negated else result

    def _bind_Case(self, level, node):
        operand = self.expr(level, node.operand) if node.operand is not None else None
        args, t = [], NULL
        for when, result in node.whens:
            condition = self.expr(level, when)
            if operand is not None:
                self.comparable(operand, condition, when)
                condition = Op("=", (operand, condition), BOOLEAN)
            else:
                self.require(condition, (BOOLEAN,), when, "CASE WHEN")
            value = self.expr(level, result)
            t = self._unify_branch(t, value, result)
            args += [condition, value]
        default = self.expr(level, node.else_) if node.else_ is not None else Const(None, NULL)
        t = self._unify_branch(t, default, node.else_)
        return Op("CASE", (*args, default), t)

    def _unify_branch(self, t, value, node):
        result = unify(t, value.type)
        if result is None:
            raise self.error(f"CASE results mix {t} and {value.type}", node)
        return result

    def _bind_Cast(self, level, node):
        return Op("CAST", (self.expr(level, node.expr),), node.type)

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
        args = [self.expr(level, a) for a in node.args]
        if name in ("COALESCE", "IFNULL"):
            if not args or (name == "IFNULL" and len(args) != 2):
                raise self.error(f"{name.lower()} needs {'two arguments' if name == 'IFNULL' else 'arguments'}", node)
            t = NULL
            for a, a_node in zip(args, node.args, strict=True):
                t = unify(t, a.type)
                if t is None:
                    raise self.error(f"{name.lower()} arguments mix incompatible types", a_node)
            return Op("COALESCE", tuple(args), t)
        if name == "NULLIF":
            if len(args) != 2:
                raise self.error("nullif needs two arguments", node)
            self.comparable(args[0], args[1], node)
            return Op("NULLIF", tuple(args), args[0].type)
        spec = FUNCTIONS.get(name)
        if spec is None:
            raise self.error(f"function {name.lower()} does not exist", node)
        signatures, result, _ = spec
        types = [a.type for a in args]
        if not any(_matches(sig, types) for sig in signatures):
            expected = " or ".join(f"{name.lower()}({', '.join(_SIGNATURE_TYPES[c] for c in sig)})" for sig in signatures)
            raise self.error(f"{name.lower()}({', '.join(types)}) does not match {expected}", node)
        return Op(name, tuple(args), types[0] if result == "same" else result)

    def aggregate(self, level, node):
        name = node.name
        if node.star:
            if name != "COUNT":
                raise self.error(f"{name.lower()}(*) is not valid; only count(*) is", node)
            return AggCall("COUNT", (), False, INTEGER)
        if len(node.args) != 1:
            raise self.error(f"{name.lower()} takes exactly one argument", node)
        arg = self.expr(level, node.args[0])
        if name in ("SUM", "AVG"):
            self.require(arg, NUMERIC, node, name.lower())
        t = INTEGER if name == "COUNT" else REAL if name == "AVG" else arg.type
        return AggCall(name, (arg,), node.distinct, t)

    def window_call(self, level, node):
        name, spec = node.name, node.over
        if name not in WINDOW_ONLY and name not in AGGREGATES:
            raise self.error(f"{name.lower()} is not a window function", node)
        if node.distinct:
            raise self.error("Arcanum doesn't support DISTINCT in window functions yet", node)
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
    allowed = {"N": NUMERIC, "I": (INTEGER,), "T": (TEXT,)}
    return all(t == NULL or c == "A" or t in allowed[c] for c, t in zip(signature, types, strict=True))


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
