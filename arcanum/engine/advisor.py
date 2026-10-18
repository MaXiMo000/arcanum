"""Query advisor: reads a finished plan and its statement, and points out what
could run faster or may be a mistake, with a ready fix where one is clear.

Plan rules use actual row counts when the statement was traced (EXPLAIN
ANALYZE or a query run with trace=True) and the optimizer's estimates
otherwise. Each hint is a dict:

    level   "warn" (likely a problem), "tip" (could be better), "good" (worth knowing)
    code    a stable identifier, e.g. "missing-index"
    title   one line; detail: why it matters and what to do
    node    the plan_tree id of the operator it is about, or None
    fix     None, or {"kind": "run", "sql", "label"}: a statement to run first
            (CREATE INDEX, ANALYZE), or {"kind": "rewrite", "sql", "original",
            "label"}: the statement rewritten
    estimate  for a suggested index: {"before", "after", "path"}, the optimizer's own
            cost for the table now and with the index, which is only suggested when
            the optimizer would choose it (a hypothetical index, as PostgreSQL's
            hypopg extension does)
"""

import re
from dataclasses import fields, is_dataclass, replace

from ..sql import nodes as n
from . import plan as p
from .catalog import IndexSchema
from .expr import AGGREGATES, Col, Const, Op, SubqueryExpr, column_indexes, conjuncts, format_expr, walk
from .optimizer import Optimizer

MIN_SCAN_PAGES = 8  # a full scan of fewer leaf pages than this is cheap; not worth a hint
ROWS_PER_PAGE = 100  # rows in a typical 4 KB leaf, when nodes are not capped by max_keys
MIN_PAIRS = 200  # a join producing fewer pairs is cheap even without a condition
SELECTIVE = 0.10  # a filter keeping at most this share of a table's rows is worth an index
MISESTIMATE = 10  # estimate and actual this many times apart
MIN_MISESTIMATE_ROWS = 50
MIN_LOOKUPS = 10  # rows fetched through an index before a covering index pays off
MAX_COVERING_EXTRA = 2  # columns a covering index may add to an existing one
LARGE_OFFSET = 1000
STALE_STATS = 2  # table rows changed by this factor since ANALYZE

_LEVELS = ("warn", "tip", "good")
_TESTS = {"=", "<", "<=", ">", ">=", "IN"}
_NULL_TEST = re.compile(r"(?i)(!=|<>|(?<![<>!])=)\s*NULL\b")
_BARE_UNION = re.compile(r"(?i)\bUNION\b(?!\s+ALL\b)")


def advise(statement, text, catalog, plan=None, stats=None, ids=None, limits=None):
    """Hints for one statement. plan is the optimized plan (if any), stats the
    executor's {id(node): [rows, seconds]} trace, ids maps id(node) to plan_tree ids."""
    advisor = _Advisor(catalog, stats, ids or {}, text, limits)
    query = statement.statement if isinstance(statement, n.Explain) else statement
    advisor.statement(query)
    if plan is not None:
        advisor.plan(plan, _selects_star(query))
    return sorted(advisor.hints, key=lambda h: _LEVELS.index(h["level"]))


class _Advisor:
    def __init__(self, catalog, stats, ids, text, limits):
        self.catalog, self.stats, self.ids, self.text, self.limits = catalog, stats, ids, text, limits
        self.hints, self.seen = [], set()

    def add(self, level, code, title, detail, node=None, fix=None, estimate=None):
        if (code, title) in self.seen:
            return
        self.seen.add((code, title))
        self.hints.append({"level": level, "code": code, "title": title, "detail": detail,
                           "node": self.ids.get(id(node)) if node is not None else None, "fix": fix,
                           "estimate": estimate})

    def rows(self, node):
        if self.stats is not None:
            return self.stats.get(id(node), (0, 0.0))[0]
        return round(getattr(node, "est_rows", None) or 0)

    def rewrite(self, sql, label):
        return {"kind": "rewrite", "sql": sql, "original": self.text, "label": label}

    # ---- the statement as written ------------------------------------------------------

    def statement(self, statement):
        if isinstance(statement, (n.Update, n.Delete)) and statement.where is None:
            verb = "UPDATE" if isinstance(statement, n.Update) else "DELETE"
            self.add("warn", "no-where", f"{verb} without WHERE changes every row of {statement.table}",
                     "That is sometimes the point, but more often a WHERE clause went missing. Inside BEGIN ... "
                     "ROLLBACK you can try it safely and undo it.")
        for node in _ast_nodes(statement):
            if isinstance(node, n.Binary) and node.op in ("=", "!=") and _is_null(node.left, node.right):
                fixed = _NULL_TEST.sub(lambda m: "IS NOT NULL" if m.group(1) in ("!=", "<>") else "IS NULL", self.text)
                self.add("warn", "null-compare", f"{'= NULL' if node.op == '=' else '!= NULL'} is never true",
                         "Comparing with NULL gives NULL (unknown), so no row passes. Test for missing values "
                         "with IS NULL or IS NOT NULL.",
                         fix=self.rewrite(fixed, "Use IS NULL") if fixed != self.text else None)
            elif isinstance(node, n.InSubquery) and node.negated:
                self.add("tip", "not-in-null", "NOT IN (subquery) returns nothing if the subquery yields a NULL",
                         "x NOT IN (1, NULL) is unknown for every x, so one NULL in the subquery hides every row. "
                         "NOT EXISTS (SELECT 1 FROM ... WHERE inner.col = outer.col) has no such trap.")
            elif isinstance(node, n.Select) and node.having is not None and not _has_aggregate(node.having):
                self.add("tip", "having-without-aggregate", "This HAVING condition could be a WHERE condition",
                         "HAVING filters groups after they are built. A condition on grouping columns alone can "
                         "go in WHERE, so the rows are dropped before any grouping work.")
            elif isinstance(node, n.Query) and isinstance(node.offset, n.Literal) and isinstance(node.offset.value, int) \
                    and node.offset.value >= LARGE_OFFSET:
                self.add("tip", "large-offset", f"OFFSET {node.offset.value:,} still produces every skipped row",
                         "The engine builds all the skipped rows and throws them away. For paging, remember the "
                         "last key you showed and ask for WHERE id > last_id ORDER BY id LIMIT n.")
            elif isinstance(node, n.SetOp) and node.op == "UNION" and not node.all:
                fixed = _BARE_UNION.sub("UNION ALL", self.text)
                self.add("tip", "union-distinct", "UNION removes duplicates, which costs a pass over every row",
                         "If the two sides cannot overlap, or duplicates are fine, UNION ALL returns the same rows "
                         "without hashing them all.", fix=self.rewrite(fixed, "Use UNION ALL") if fixed != self.text else None)

    # ---- the plan ------------------------------------------------------------------------

    def plan(self, plan, star):
        misestimated = False
        for node in _plan_nodes(plan):
            if isinstance(node, p.Filter) and isinstance(node.input, p.TableScan):
                self.filtered_scan(node, node.input)
            elif isinstance(node, p.TableScan):
                self.stale_stats(node)
            elif isinstance(node, p.IndexScan):
                self.index_scan(node, star)
            elif isinstance(node, p.Join):
                self.join(node)
            elif isinstance(node, p.Sort) and self.limits is not None and self.rows(node) > self.limits.sort_memory_rows:
                self.add("tip", "sort-spill", f"Sorting {self.rows(node):,} rows spilled to disk",
                         f"More than {self.limits.sort_memory_rows:,} rows do not fit in the sort's memory, so it "
                         "wrote sorted runs to disk and merged them. A LIMIT, or filtering first, keeps it in memory.",
                         node)
            self.correlated(node)
            if not misestimated:
                misestimated = self.misestimate(node)

    def filtered_scan(self, node, scan):
        table = scan.table
        self.stale_stats(scan)
        total, kept = self.rows(scan), self.rows(node)
        if total < MIN_SCAN_PAGES * (self.catalog.max_keys or ROWS_PER_PAGE):
            return
        equal, ranged = [], []
        for c in conjuncts(node.predicate):
            column, op = _column_test(c)
            if column is not None:
                (equal if op in ("=", "IN") else ranged).append(column)
            elif isinstance(c, Op) and c.op == "LIKE" and isinstance(c.args[0], Col) and _leading_wildcard(c.args[1]):
                self.add("tip", "leading-wildcard", f"{format_expr(c)} must read every row",
                         "An index is sorted by the start of each value, so a pattern that starts with % "
                         "cannot jump anywhere. Anchor the pattern ('abc%') if you can.", node)
            elif (hidden := _hidden_column(c)) is not None:
                name = table.columns[hidden].name
                self.add("tip", "hidden-column", f"{format_expr(c)} hides {name} inside an expression",
                         f"An index on {name} stores {name} itself, not an expression of it. Keep the column bare "
                         f"on one side (move the arithmetic to the constant) so an index can match.", node)
        if kept > total * SELECTIVE:
            return
        wanted = list(dict.fromkeys(equal)) + [c for c in dict.fromkeys(ranged) if c not in equal][:1]
        if not wanted:
            return
        leading = {index.columns[0] for index in table.indexes} | set(table.primary_key[:1])
        share = f"{kept:,} of {total:,} rows ({kept / total:.1%})" if total else f"{kept:,} rows"
        if any(c in leading for c in wanted):
            if table.stats is None:
                self.add("tip", "analyze", f"{table.name} has an index the optimizer did not trust",
                         f"The scan kept {share}, but without statistics the optimizer guessed far more and "
                         "chose to read everything. ANALYZE measures the table so it can choose the index.",
                         node, {"kind": "run", "sql": f"ANALYZE {table.name}", "label": f"ANALYZE {table.name}"})
            return
        # An index that also holds the other columns read answers without the table (index-only);
        # one that does not costs a table descent per match, which can lose to the scan itself.
        extra = [c for c in scan.reads or () if c not in wanted and c not in table.primary_key]
        covering = (scan.reads is not None and len(extra) <= MAX_COVERING_EXTRA
                    and len(wanted) + len(extra) + len(table.primary_key) < len(table.columns))  # not a copy of the table
        columns = wanted + (extra if covering else [])
        filters = [table.columns[c].name for c in wanted]
        names = [table.columns[c].name for c in columns]
        index = self.index_name(table.name, names)
        estimate = _what_if(scan, conjuncts(node.predicate), columns, index)
        if estimate is None:
            return  # the optimizer would keep the scan even with this index
        how = (" Because it also holds every other column the query reads (and the primary key), the answer "
               "comes from the index alone: an Index Only Scan." if covering else "")
        self.add("warn", "missing-index", f"Seq Scan read all of {table.name} to keep {share}",
                 f"No index starts with {' or '.join(filters)}, so every row was read and tested. An index on "
                 f"({', '.join(names)}) lets the engine descend the B+tree straight to the matching rows.{how}", node,
                 {"kind": "run", "sql": f"CREATE INDEX {index} ON {table.name} ({', '.join(names)})",
                  "label": f"Create index on {table.name} ({', '.join(names)})"}, estimate)

    def stale_stats(self, scan):
        stats = scan.table.stats
        if stats is None or self.stats is None:
            return
        actual = self.rows(scan)
        if max(actual, stats.rows) >= MIN_SCAN_PAGES * (self.catalog.max_keys or ROWS_PER_PAGE) and max(actual, stats.rows) > STALE_STATS * max(1, min(actual, stats.rows)):
            name = scan.table.name
            self.add("tip", "stale-stats", f"Statistics for {name} are out of date",
                     f"ANALYZE counted {stats.rows:,} rows; the table now has {actual:,}. The optimizer plans "
                     "with the old numbers until you ANALYZE again.", scan,
                     {"kind": "run", "sql": f"ANALYZE {name}", "label": f"ANALYZE {name}"})

    def index_scan(self, scan, star):
        if scan.index is None:
            return
        name = f"{scan.index.name} ({', '.join(scan.table.columns[c].name for c in scan.index.columns)})"
        if scan.index_only:
            self.add("good", "index-only", f"Index Only Scan: answered from {scan.index.name} alone",
                     "Every column the query reads is inside the index entries, so the table itself was never "
                     "opened: one B+tree walk instead of a second descent per row.", scan)
            return
        fetched = self.rows(scan)
        if fetched < MIN_LOOKUPS:
            return
        if star:
            self.add("tip", "select-star", f"SELECT * fetched {fetched:,} rows from the table one by one",
                     f"The index {name} finds the rows, but * needs every column, so each match costs a second "
                     "lookup in the table. List only the columns you use; if they are all in the index, this "
                     "becomes an Index Only Scan.", scan)
        elif 0 < len(scan.uncovered) <= MAX_COVERING_EXTRA:
            table = scan.table
            columns = [table.columns[c].name for c in [*scan.index.columns, *scan.uncovered]]
            index = self.index_name(table.name, columns)
            extra = ", ".join(table.columns[c].name for c in scan.uncovered)
            self.add("tip", "covering-index", f"{fetched:,} table lookups only to read {extra}",
                     f"{name} finds the rows but lacks {extra}, so each match descends the table's tree too. "
                     f"An index on ({', '.join(columns)}) holds everything this query reads: an Index Only Scan.",
                     scan, {"kind": "run", "sql": f"CREATE INDEX {index} ON {table.name} ({', '.join(columns)})",
                            "label": f"Create covering index ({', '.join(columns)})"})

    def join(self, node):
        if node.left_keys or node.kind not in ("INNER", "CROSS", "LEFT"):
            return
        left, right = self.rows(node.left), self.rows(node.right)
        if left <= 1 or right <= 1 or left * right < MIN_PAIRS:
            return
        if node.residual is None:
            self.add("warn", "cartesian", f"Cartesian product: {left:,} × {right:,} = {left * right:,} pairs",
                     "Nothing links the two sides, so every row is paired with every row. If you meant to match "
                     "rows, add a join condition (JOIN b ON a.key = b.key).", node)
        else:
            self.add("tip", "nested-loop", f"Nested Loop compared {left * right:,} pairs",
                     f"The join condition {format_expr(node.residual)} has no equality between the two sides, so "
                     "no hash table can pair rows up. An equality condition, where one exists, enables a Hash Join.",
                     node)

    def correlated(self, node):
        for e in p.expressions(node):
            for x in walk(e):
                if isinstance(x, SubqueryExpr) and x.correlated:
                    runs = self.rows(p.inputs(node)[0]) if p.inputs(node) else 1
                    if runs > 1:
                        self.add("tip", "correlated", f"A correlated subquery ran {runs:,} times",
                                 "It reads columns of the outer row, so it re-runs for each one. The optimizer turns "
                                 "EXISTS, IN and aggregate subqueries correlated by equality into joins; written as a "
                                 "JOIN (with GROUP BY if needed) this one would read its table once.", node)

    def misestimate(self, node):
        estimate = getattr(node, "est_rows", None)
        if self.stats is None or estimate is None:
            return False
        actual = self.rows(node)
        low, high = min(actual, estimate), max(actual, estimate)
        if high < MIN_MISESTIMATE_ROWS or high < MISESTIMATE * max(1, low):
            return False
        unmeasured = sorted({s.table.name for s in _plan_nodes(node)
                             if isinstance(s, (p.TableScan, p.IndexScan)) and s.table.stats is None})
        if not unmeasured:
            return False
        self.add("tip", "misestimate", f"The optimizer expected {round(estimate):,} rows and got {actual:,}",
                 f"{', '.join(unmeasured)} {'has' if len(unmeasured) == 1 else 'have'} never been analyzed, so "
                 "estimates are fixed guesses and join orders and index choices may be wrong.", node,
                 {"kind": "run", "sql": "; ".join(f"ANALYZE {t}" for t in unmeasured), "label": "ANALYZE"})
        return True

    def index_name(self, table, columns):
        base = "_".join([table, *columns])
        name, i = base, 2
        while name in self.catalog.indexes:
            name, i = f"{base}_{i}", i + 1
        return name


# ---- helpers ---------------------------------------------------------------------------------


def _what_if(scan, preds, columns, name):
    """Ask the optimizer to plan the scan as if an index on `columns` existed. Returns the estimated
    costs without and with it, or None if it would not choose the index."""
    table = scan.table
    hypothetical = IndexSchema(name, table.name, list(columns), False, -1, "")
    candidate = p.TableScan(replace(table, indexes=[*table.indexes, hypothetical]), scan.columns, scan.reads)
    decisions = []
    Optimizer(decisions).access_path(candidate, list(preds))
    options = decisions[-1]["candidates"] if decisions else []
    chosen = next((c for c in options if c["chosen"]), None)
    if chosen is None or not chosen["path"].endswith(f"using {name}"):
        return None
    return {"before": options[0]["cost"], "after": chosen["cost"], "path": chosen["path"]}


def _plan_nodes(plan):
    """Every operator of the plan, including subquery plans, each once."""
    stack, seen = [plan], set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        exprs = p.expressions(node)
        if isinstance(node, p.IndexScan):
            exprs = node.eq + (node.points or []) + [b[0] for b in (node.lo, node.hi) if b is not None]
        stack.extend(x.plan for e in exprs for x in walk(e) if isinstance(x, SubqueryExpr))
        stack.extend(reversed(p.inputs(node)))


def _ast_nodes(node):
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, (list, tuple)):
            stack.extend(item)
        elif is_dataclass(item):
            yield item
            stack.extend(getattr(item, f.name) for f in fields(item))


def _selects_star(statement):
    return isinstance(statement, n.Query) and any(isinstance(x, n.Star) for x in _ast_nodes(statement.body))


def _is_null(a, b):
    return any(isinstance(x, n.Literal) and x.value is None for x in (a, b))


def _has_aggregate(e):
    return any(isinstance(x, n.Call) and x.name in AGGREGATES and x.over is None for x in _ast_nodes(e))


def _column_test(c):
    """(column, op) when c compares a bare column with values that read no column of the row."""
    if not isinstance(c, Op) or c.op not in _TESTS:
        return None, None
    first, rest = c.args[0], c.args[1:]
    if any(isinstance(a, Const) and a.value is None for a in c.args):
        return None, None  # = NULL matches nothing; the null-compare hint covers it
    if isinstance(first, Col) and not any(column_indexes(a) for a in rest):
        return first.index, c.op
    if c.op != "IN" and isinstance(c.args[1], Col) and not column_indexes(first):
        return c.args[1].index, c.op
    return None, None


def _hidden_column(c):
    """The column of a comparison like lower(name) = 'x' or a + 1 > 5, where it sits inside an expression."""
    if not isinstance(c, Op) or c.op not in _TESTS - {"IN"}:
        return None
    for side, other in ((c.args[0], c.args[1]), (c.args[1], c.args[0])):
        columns = column_indexes(side)
        if isinstance(side, Op) and len(columns) == 1 and not column_indexes(other):
            return next(iter(columns))
    return None


def _leading_wildcard(pattern):
    return isinstance(pattern, Const) and isinstance(pattern.value, str) and pattern.value[:1] in ("%", "_")
