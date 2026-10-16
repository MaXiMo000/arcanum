"""Executes query plans using the iterator (Volcano) model.

Every plan node becomes a generator that pulls rows from its inputs on
demand, so `LIMIT 10` stops a scan after ten rows and EXISTS stops at the
first match. Expressions are compiled once into Python closures taking
(row, outer), where `outer` holds the rows of enclosing queries that
correlated subqueries refer to.

NULL follows SQL's three-valued logic: comparisons with NULL yield NULL
(unknown), and WHERE/ON/HAVING keep only rows whose condition is TRUE.
"""

import itertools
import operator
import time
from collections import Counter

from ..errors import ExecutionError
from . import plan as p
from .expr import (
    FUNCTIONS, Col, Const, Op, Outer, SubqueryExpr, cast, check_int, divide, like, modulo,
    to_text,
)

MAX_RECURSIVE_ROWS = 1_000_000


class Context:
    """State for running one statement."""

    def __init__(self, catalog, analyze=False):
        self.catalog = catalog
        self.work_tables = {}  # recursive CTE id -> rows of the previous iteration
        self.cache = {}  # id(subquery) -> result of an uncorrelated subquery
        self.stats = {} if analyze else None  # id(plan node) -> [rows, seconds]
        self._compiled = {}

    def compile(self, e):
        entry = self._compiled.get(id(e))
        if entry is None:
            # Keeping e alive in the entry guarantees its id is not reused while cached.
            entry = self._compiled[id(e)] = (e, _compile(e, self))
        return entry[1]


def run(plan, ctx, outer=()):
    rows = _RUNNERS[type(plan)](plan, ctx, outer)
    if ctx.stats is not None:
        rows = _measured(rows, ctx.stats.setdefault(id(plan), [0, 0.0]))
    return rows


def evaluate_constant(e):
    return _compile(e, Context(None))((), ())


def _measured(rows, stat):
    while True:
        start = time.perf_counter()
        try:
            row = next(rows)
        except StopIteration:
            stat[1] += time.perf_counter() - start
            return
        stat[1] += time.perf_counter() - start
        stat[0] += 1
        yield row


# ---- expressions -------------------------------------------------------------------


def _strict(f, fns):
    """Call f on the evaluated arguments, or return NULL if any argument is NULL."""
    if len(fns) == 1:
        (fa,) = fns

        def one(row, outer):
            a = fa(row, outer)
            return None if a is None else f(a)
        return one
    if len(fns) == 2:
        fa, fb = fns

        def two(row, outer):
            a = fa(row, outer)
            if a is None:
                return None
            b = fb(row, outer)
            return None if b is None else f(a, b)
        return two

    def many(row, outer):
        args = [g(row, outer) for g in fns]
        return None if any(a is None for a in args) else f(*args)
    return many


_BINARY = {
    "=": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
    "+": lambda a, b: check_int(a + b), "-": lambda a, b: check_int(a - b), "*": lambda a, b: check_int(a * b),
    "/": divide, "%": modulo, "||": lambda a, b: to_text(a) + to_text(b), "LIKE": like,
}


def _compile(e, ctx):
    if isinstance(e, Const):
        value = e.value
        return lambda row, outer: value
    if isinstance(e, Col):
        i = e.index
        return lambda row, outer: row[i]
    if isinstance(e, Outer):
        depth, i = -e.depth, e.index
        return lambda row, outer: outer[depth][i]
    if isinstance(e, SubqueryExpr):
        return _compile_subquery(e, ctx)
    if not isinstance(e, Op):
        raise AssertionError(f"{type(e).__name__} must be planned before it can be evaluated")
    fns = [ctx.compile(a) for a in e.args]
    op = e.op
    if op == "AND":
        fa, fb = fns

        def and_(row, outer):
            a = fa(row, outer)
            if a is False:
                return False
            b = fb(row, outer)
            if b is False:
                return False
            return None if a is None or b is None else True
        return and_
    if op == "OR":
        fa, fb = fns

        def or_(row, outer):
            a = fa(row, outer)
            if a is True:
                return True
            b = fb(row, outer)
            if b is True:
                return True
            return None if a is None or b is None else False
        return or_
    if op == "NOT":
        return _strict(operator.not_, fns)
    if op == "NEG":
        return _strict(lambda v: check_int(-v), fns)
    if op in ("ISNULL", "NOTNULL"):
        (fa,) = fns
        want_null = op == "ISNULL"
        return lambda row, outer: (fa(row, outer) is None) == want_null
    if op == "IN":
        return _compile_in(e, fns)
    if op == "CASE":
        pairs = list(zip(fns[:-1:2], fns[1:-1:2]))
        default = fns[-1]

        def case(row, outer):
            for condition, result in pairs:
                if condition(row, outer) is True:
                    return result(row, outer)
            return default(row, outer)
        return case
    if op == "COALESCE":
        def coalesce(row, outer):
            for f in fns:
                v = f(row, outer)
                if v is not None:
                    return v
            return None
        return coalesce
    if op == "NULLIF":
        fa, fb = fns

        def nullif(row, outer):
            a = fa(row, outer)
            if a is None:
                return None
            b = fb(row, outer)
            return None if b is not None and a == b else a
        return nullif
    if op == "CAST":
        target = e.type
        return _strict(lambda v: cast(v, target), fns)
    if op in _BINARY:
        return _strict(_BINARY[op], fns)
    return _strict(FUNCTIONS[op][2], fns)


def _compile_in(e, fns):
    needle, items = fns[0], fns[1:]
    if all(isinstance(a, Const) for a in e.args[1:]):
        values = {a.value for a in e.args[1:] if a.value is not None}
        has_null = any(a.value is None for a in e.args[1:])

        def in_constants(row, outer):
            v = needle(row, outer)
            if v is None:
                return None
            return True if v in values else (None if has_null else False)
        return in_constants

    def in_list(row, outer):
        v = needle(row, outer)
        if v is None:
            return None
        saw_null = False
        for f in items:
            item = f(row, outer)
            if item is None:
                saw_null = True
            elif item == v:
                return True
        return None if saw_null else False
    return in_list


def _compile_subquery(e, ctx):
    plan, key = e.plan, id(e)
    needle = ctx.compile(e.arg) if e.arg is not None else None

    def rows(row, outer):
        return run(plan, ctx, outer + (row,))

    if e.kind == "EXISTS":
        if e.correlated:
            return lambda row, outer: next(rows(row, outer), None) is not None

        def exists(row, outer):
            if key not in ctx.cache:
                ctx.cache[key] = next(run(plan, ctx, ()), None) is not None
            return ctx.cache[key]
        return exists

    if e.kind == "SCALAR":
        def scalar_value(source):
            first = next(source, None)
            if first is not None and next(source, None) is not None:
                raise ExecutionError("a subquery used as a value returned more than one row")
            return None if first is None else first[0]

        if e.correlated:
            return lambda row, outer: scalar_value(rows(row, outer))

        def scalar(row, outer):
            if key not in ctx.cache:
                ctx.cache[key] = scalar_value(run(plan, ctx, ()))
            return ctx.cache[key]
        return scalar

    def membership(v, values, has_null, empty):
        if empty:
            return False
        if v is None:
            return None
        return True if v in values else (None if has_null else False)

    if e.correlated:
        def in_correlated(row, outer):
            found = [r[0] for r in rows(row, outer)]
            return membership(needle(row, outer), set(found) - {None}, None in found, not found)
        return in_correlated

    def in_uncorrelated(row, outer):
        if key not in ctx.cache:
            found = [r[0] for r in run(plan, ctx, ())]
            ctx.cache[key] = (set(found) - {None}, None in found, not found)
        return membership(needle(row, outer), *ctx.cache[key])
    return in_uncorrelated


# ---- operators -----------------------------------------------------------------------


def _one_row(plan, ctx, outer):
    yield ()


def _table_scan(plan, ctx, outer):
    for _, row in ctx.catalog.scan(plan.table):
        yield row


def _work_table_scan(plan, ctx, outer):
    yield from ctx.work_tables[plan.cte_id]


def _values(plan, ctx, outer):
    rows = [[ctx.compile(e) for e in row] for row in plan.rows]
    for fns in rows:
        yield tuple(f((), outer) for f in fns)


def _filter(plan, ctx, outer):
    predicate = ctx.compile(plan.predicate)
    for row in run(plan.input, ctx, outer):
        if predicate(row, outer) is True:
            yield row


def _project(plan, ctx, outer):
    fns = [ctx.compile(e) for e in plan.exprs]
    for row in run(plan.input, ctx, outer):
        yield tuple(f(row, outer) for f in fns)


def _join(plan, ctx, outer):
    """Hash join when there are equality keys, nested loop otherwise.

    The right input is materialised (and hashed on its keys); the left input
    streams. RIGHT and FULL joins remember which right rows found a partner.
    """
    right_rows = list(run(plan.right, ctx, outer))
    residual = ctx.compile(plan.residual) if plan.residual is not None else None
    left_keys = [ctx.compile(k) for k in plan.left_keys]
    if left_keys:
        right_keys = [ctx.compile(k) for k in plan.right_keys]
        buckets = {}
        for j, r in enumerate(right_rows):
            key = tuple(f(r, outer) for f in right_keys)
            if None not in key:  # NULL never equals anything, so it can never match
                buckets.setdefault(key, []).append(j)
    keep_left = plan.kind in ("LEFT", "FULL")
    matched = set() if plan.kind in ("RIGHT", "FULL") else None
    right_nulls = (None,) * len(plan.right.columns)
    every_right = range(len(right_rows))
    for left in run(plan.left, ctx, outer):
        if left_keys:
            key = tuple(f(left, outer) for f in left_keys)
            candidates = () if None in key else buckets.get(key, ())
        else:
            candidates = every_right
        found = False
        for j in candidates:
            row = left + right_rows[j]
            if residual is None or residual(row, outer) is True:
                found = True
                if matched is not None:
                    matched.add(j)
                yield row
        if not found and keep_left:
            yield left + right_nulls
    if matched is not None:
        left_nulls = (None,) * len(plan.left.columns)
        for j, right in enumerate(right_rows):
            if j not in matched:
                yield left_nulls + right


def _aggregate(plan, ctx, outer):
    group_fns = [ctx.compile(g) for g in plan.groups]
    arg_fns = [ctx.compile(a.args[0]) if a.args else None for a in plan.aggs]
    groups = {}
    for row in run(plan.input, ctx, outer):
        key = tuple(f(row, outer) for f in group_fns)
        accumulators = groups.get(key)
        if accumulators is None:
            accumulators = groups[key] = [make_accumulator(a.name, a.distinct) for a in plan.aggs]
        for acc, f in zip(accumulators, arg_fns):
            acc.add(f(row, outer) if f is not None else 1)  # COUNT(*) counts a non-NULL 1 per row
    if not groups and not plan.groups:  # aggregates over no rows still produce one row
        groups[()] = [make_accumulator(a.name, a.distinct) for a in plan.aggs]
    for key, accumulators in groups.items():
        yield key + tuple(acc.result() for acc in accumulators)


def _window(plan, ctx, outer):
    rows = list(run(plan.input, ctx, outer))
    extra = [[None] * len(plan.calls) for _ in rows]
    specs = {}
    for k, call in enumerate(plan.calls):
        specs.setdefault((call.partition, call.order, call.frame), []).append(k)
    for (partition, order, frame), call_indexes in specs.items():
        partition_fns = [ctx.compile(e) for e in partition]
        order_fns = [ctx.compile(e) for e, _, _ in order]
        directions = [(desc, nulls_first) for _, desc, nulls_first in order]
        order_keys = [tuple(f(r, outer) for f in order_fns) for r in rows]
        partitions = {}
        for i, r in enumerate(rows):
            partitions.setdefault(tuple(f(r, outer) for f in partition_fns), []).append(i)
        for members in partitions.values():
            members = sort_items(members, lambda i: order_keys[i], directions)
            peers = _peer_groups([order_keys[i] for i in members])
            for k in call_indexes:
                values = _window_values(plan.calls[k], members, rows, peers, bool(order), frame, ctx, outer)
                for i, v in zip(members, values):
                    extra[i][k] = v
    for row, values in zip(rows, extra):
        yield row + tuple(values)


def _sort(plan, ctx, outer):
    fns = [ctx.compile(k) for k, _, _ in plan.keys]
    directions = [(desc, nulls_first) for _, desc, nulls_first in plan.keys]
    decorated = [(tuple(f(r, outer) for f in fns), r) for r in run(plan.input, ctx, outer)]
    for _, row in sort_items(decorated, lambda d: d[0], directions):
        yield row


def _limit(plan, ctx, outer):
    stop = None if plan.limit is None else plan.offset + plan.limit
    yield from itertools.islice(run(plan.input, ctx, outer), plan.offset, stop)


def _distinct(plan, ctx, outer):
    seen = set()
    for row in run(plan.input, ctx, outer):
        if row not in seen:
            seen.add(row)
            yield row


def _set_op(plan, ctx, outer):
    if plan.op == "UNION":
        rows = itertools.chain(run(plan.left, ctx, outer), run(plan.right, ctx, outer))
        yield from rows if plan.all else _distinct_rows(rows)
        return
    right = Counter(run(plan.right, ctx, outer))
    emitted = set()
    for row in run(plan.left, ctx, outer):
        in_right = right[row] > 0
        if plan.all:
            if in_right:
                right[row] -= 1  # each right copy cancels or matches one left copy
            if in_right == (plan.op == "INTERSECT"):
                yield row
        elif in_right == (plan.op == "INTERSECT") and row not in emitted:
            emitted.add(row)
            yield row


def _distinct_rows(rows):
    seen = set()
    for row in rows:
        if row not in seen:
            seen.add(row)
            yield row


def _recursive_cte(plan, ctx, outer):
    seen = None if plan.all else set()

    def fresh(rows):
        if seen is None:
            return list(rows)
        out = []
        for row in rows:
            if row not in seen:
                seen.add(row)
                out.append(row)
        return out

    working = fresh(run(plan.anchor, ctx, outer))
    total = len(working)
    yield from working
    while working:
        ctx.work_tables[plan.cte_id] = working
        working = fresh(run(plan.recursive, ctx, outer))
        total += len(working)
        if total > MAX_RECURSIVE_ROWS:
            raise ExecutionError(f"recursive CTE {plan.name} produced more than {MAX_RECURSIVE_ROWS:,} rows; "
                                 "is it missing a stop condition?")
        yield from working


_RUNNERS = {
    p.OneRow: _one_row, p.TableScan: _table_scan, p.WorkTableScan: _work_table_scan, p.Values: _values,
    p.Filter: _filter, p.Project: _project, p.Join: _join, p.Aggregate: _aggregate, p.Window: _window,
    p.Sort: _sort, p.Limit: _limit, p.Distinct: _distinct, p.SetOp: _set_op, p.RecursiveCTE: _recursive_cte,
}


# ---- sorting -----------------------------------------------------------------------


def sort_items(items, key, directions):
    """Stable multi-key sort. NULL sorts lowest: first ascending, last descending,
    unless NULLS FIRST / NULLS LAST says otherwise."""
    items = list(items)
    for j in reversed(range(len(directions))):
        desc, nulls_first = directions[j]
        if nulls_first is None:
            nulls_first = not desc
        null_rank_high = desc == nulls_first  # NULL must sort as the largest value in this pass

        def pass_key(item, j=j, high=null_rank_high):
            v = key(item)[j]
            if v is None:
                return (high, 0)
            return (not high, v)

        items.sort(key=pass_key, reverse=desc)
    return items


# ---- window functions ----------------------------------------------------------------


def _peer_groups(keys):
    """For each position, the [start, end) range of rows with an equal ORDER BY key."""
    n = len(keys)
    starts, ends = [0] * n, [0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and keys[j + 1] == keys[i]:
            j += 1
        for t in range(i, j + 1):
            starts[t], ends[t] = i, j + 1
        i = j + 1
    return starts, ends


def _frame_bounds(t, n, starts, ends, ordered, frame):
    if frame is None:
        # Default: with ORDER BY, everything up to the current row and its peers; without, the whole partition.
        return (0, ends[t]) if ordered else (0, n)
    mode, start_kind, start_offset, end_kind, end_offset = frame
    if mode == "ROWS":
        start = {"UNBOUNDED PRECEDING": 0, "PRECEDING": t - (start_offset or 0), "CURRENT ROW": t,
                 "FOLLOWING": t + (start_offset or 0)}[start_kind]
        end = {"UNBOUNDED FOLLOWING": n, "FOLLOWING": t + (end_offset or 0) + 1, "CURRENT ROW": t + 1,
               "PRECEDING": t - (end_offset or 0) + 1}[end_kind]
    else:  # RANGE: whole peer groups
        start = 0 if start_kind == "UNBOUNDED PRECEDING" else starts[t]
        end = n if end_kind == "UNBOUNDED FOLLOWING" else ends[t]
    start, end = max(start, 0), min(end, n)
    return start, max(start, end)


def _window_values(call, members, rows, peers, ordered, frame, ctx, outer):
    n = len(members)
    starts, ends = peers
    name = call.name
    if name == "ROW_NUMBER":
        return list(range(1, n + 1))
    if name == "RANK":
        return [s + 1 for s in starts]
    if name == "DENSE_RANK":
        out, rank = [], 0
        for t in range(n):
            if starts[t] == t:
                rank += 1
            out.append(rank)
        return out
    fns = [ctx.compile(a) for a in call.args]
    values = [fns[0](rows[i], outer) for i in members] if fns else [1] * n  # COUNT(*) counts 1 per row
    if name in ("LAG", "LEAD"):
        offset = call.args[1].value if len(call.args) > 1 else 1
        default = fns[2] if len(fns) > 2 else None
        out = []
        for t in range(n):
            j = t - offset if name == "LAG" else t + offset
            if 0 <= j < n:
                out.append(values[j])
            else:
                out.append(default(rows[members[t]], outer) if default else None)
        return out
    bounds = [_frame_bounds(t, n, starts, ends, ordered, frame) for t in range(n)]
    if name == "FIRST_VALUE":
        return [values[s] if s < e else None for s, e in bounds]
    if name == "LAST_VALUE":
        return [values[e - 1] if s < e else None for s, e in bounds]
    if all(s == 0 for s, _ in bounds):
        # Every frame starts at the partition start and frame ends never move back, so one running total serves all rows.
        acc, position, out = make_accumulator(name, False), 0, []
        for _, end in bounds:
            while position < end:
                acc.add(values[position])
                position += 1
            out.append(acc.result())
        return out
    out = []
    for s, e in bounds:
        acc = make_accumulator(name, False)
        for v in values[s:e]:
            acc.add(v)
        out.append(acc.result())
    return out


# ---- aggregate accumulators -------------------------------------------------------------


class _Accumulator:
    def __init__(self, distinct):
        self.seen = set() if distinct else None
        self.reset()

    def add(self, v):
        if v is None:  # aggregates ignore NULL inputs
            return
        if self.seen is not None:
            if v in self.seen:
                return
            self.seen.add(v)
        self.step(v)


class _Count(_Accumulator):
    def reset(self):
        self.n = 0

    def step(self, v):
        self.n += 1

    def result(self):
        return self.n


class _Sum(_Accumulator):
    def reset(self):
        self.total = None

    def step(self, v):
        self.total = v if self.total is None else self.total + v

    def result(self):
        return check_int(self.total)


class _Avg(_Accumulator):
    def reset(self):
        self.total, self.n = 0, 0

    def step(self, v):
        self.total += v
        self.n += 1

    def result(self):
        return self.total / self.n if self.n else None


class _Min(_Accumulator):
    def reset(self):
        self.value = None

    def step(self, v):
        if self.value is None or v < self.value:
            self.value = v

    def result(self):
        return self.value


class _Max(_Min):
    def step(self, v):
        if self.value is None or v > self.value:
            self.value = v


_ACCUMULATORS = {"COUNT": _Count, "SUM": _Sum, "AVG": _Avg, "MIN": _Min, "MAX": _Max}


def make_accumulator(name, distinct):
    return _ACCUMULATORS[name](distinct)
