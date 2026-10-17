"""Query optimizer: rewrites a bound plan into a cheaper plan with the same result.

Passes, in order:

1. Decorrelation. A correlated EXISTS / NOT EXISTS / IN in a WHERE clause runs
   its subquery once per row. When the correlation is through equalities, it
   becomes a SEMI or ANTI hash join that reads the subquery's tables once. A
   correlated scalar aggregate, `(SELECT count(*) FROM t WHERE t.k = outer.k)`,
   becomes a LEFT JOIN to `t` grouped by k, when that beats an index lookup per
   outer row; rows without a match get the aggregate's value over no rows
   (0 for COUNT), not NULL.
2. Predicate pushdown. WHERE and ON conditions move down through joins,
   projections, aggregates (when they only test grouping keys), sorts and set
   operations, so rows are discarded as early as possible. Equalities between
   the two sides of an inner join become hash-join keys.
3. Access paths. A filter directly on a table becomes an index scan when an
   index (or the primary key) matches a prefix of equalities, optionally
   followed by a range, and the cost model says that is cheaper.
4. Join ordering. A cluster of inner joins is re-planned bottom-up by dynamic
   programming over subsets of tables (the System R approach), choosing the
   order and the hash-build side with the lowest estimated cost. A projection
   restores the original column order.

Estimates use ANALYZE statistics when present (row counts, distinct counts,
NULL counts, equi-depth histograms) and fixed guesses otherwise.
"""

import math
from dataclasses import replace

from . import plan as p
from .expr import (
    BOOLEAN, INTEGER, NULL, REAL, Col, Const, Op, Outer, SubqueryExpr, column_indexes, format_expr, map_children,
    shift, walk,
)

DEFAULT_ROWS = 1000
DEFAULT_EQUALITY = 0.05
DEFAULT_RANGE = 1 / 3
DEFAULT_OTHER = 1 / 3
SECONDARY_LOOKUP_COST = 4  # a secondary index hit costs a second B+tree descent
MAX_REORDER_RELATIONS = 10

_FLIP = {"=": "=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
_RANGE_OPS = ("<", "<=", ">", ">=")


def optimize(plan):
    return Optimizer().optimize(plan)


class Optimizer:
    def __init__(self):
        self._done = {}  # id(original plan) -> (original, optimized); keeps shared CTE plans consistent
        self._estimates = {}

    def optimize(self, plan):
        entry = self._done.get(id(plan))
        if entry is not None:
            return entry[1]
        result = self.reorder(self.push(plan, []))
        self.annotate(result)
        self._done[id(plan)] = (plan, result)
        self.optimize_subqueries(result)
        return result

    def optimize_subqueries(self, node):
        for e in p.expressions(node):
            self.optimize_expr(e)
        if isinstance(node, p.IndexScan):
            for e in node.eq + [b[0] for b in (node.lo, node.hi) if b is not None]:
                self.optimize_expr(e)
        for child in p.inputs(node):
            self.optimize_subqueries(child)

    def optimize_expr(self, e):
        for sub in walk(e):
            if isinstance(sub, SubqueryExpr):
                sub.plan = self.optimize(sub.plan)

    # ---- 1. decorrelation ----------------------------------------------------------

    def semi_join(self, left, condition):
        """Turn `[NOT] EXISTS (...)` or `x IN (...)` on top of `left` into a SEMI/ANTI join, if possible."""
        negated = isinstance(condition, Op) and condition.op == "NOT"
        sub = condition.args[0] if negated else condition
        if not isinstance(sub, SubqueryExpr) or (negated and sub.kind != "EXISTS") or sub.kind == "SCALAR":
            return None
        if sub.kind == "EXISTS" and not sub.correlated:
            return None  # evaluated once and cached already
        plan, inner_value = sub.plan, None
        if isinstance(plan, p.Distinct):
            plan = plan.input  # duplicates never matter to a semi join
        if isinstance(plan, p.Project):
            inner_value = plan.exprs[0] if sub.kind == "IN" else None
            plan = plan.input
        elif sub.kind == "IN":
            return None
        conditions = []
        if isinstance(plan, p.Filter):
            conditions = _conjuncts(plan.predicate)
            plan = plan.input
        if _reaches_outside(plan) or (inner_value is not None and _has_outer_or_subquery(inner_value)):
            return None
        width = len(left.columns)
        left_keys, right_keys, local, residual = [], [], [], []
        for c in conditions:
            if any(isinstance(x, SubqueryExpr) for x in walk(c)):
                return None
            outers = [x for x in walk(c) if isinstance(x, Outer)]
            if not outers:
                local.append(c)
                continue
            if any(o.depth != 1 for o in outers):
                return None
            key = _correlated_key(c)
            if key is not None:
                left_keys.append(_outer_to_col(key[0]))
                right_keys.append(key[1])
            else:
                residual.append(_to_combined(c, width))
        if sub.kind == "IN":
            if any(isinstance(x, SubqueryExpr) for x in walk(sub.arg)):
                return None
            left_keys.append(sub.arg)
            right_keys.append(inner_value)
        if not left_keys:
            return None
        right = p.Filter(plan, _and_all(local), plan.columns) if local else plan
        return p.Join("ANTI" if negated else "SEMI", left, right, left_keys, right_keys, _and_all(residual),
                      left.columns)

    def attach_scalars(self, source, e):
        """Replace correlated scalar aggregate subqueries in e by LEFT JOINs onto source.

        Returns (new source, new expression). The new source keeps source's
        columns first, so expressions over source stay valid.
        """
        for sub in [x for x in walk(e) if isinstance(x, SubqueryExpr) and x.kind == "SCALAR" and x.correlated]:
            rewrite = self.scalar_join(source, sub)
            if rewrite is not None:
                source, replacement = rewrite
                e = _replace_node(e, sub, replacement)
        return source, e

    def scalar_join(self, source, sub):
        plan = sub.plan
        if not (isinstance(plan, p.Project) and len(plan.exprs) == 1 and isinstance(plan.input, p.Aggregate)
                and not plan.input.groups):
            return None
        value, aggregate = plan.exprs[0], plan.input
        inner, conditions = aggregate.input, []
        if isinstance(inner, p.Filter):
            conditions, inner = _conjuncts(inner.predicate), inner.input
        if (_reaches_outside(inner) or _has_outer_or_subquery(value)
                or any(_has_outer_or_subquery(a) for a in aggregate.aggs)):
            return None
        left_keys, right_keys, local = [], [], []
        for c in conditions:
            outers = [x for x in walk(c) if isinstance(x, Outer)]
            if _has_subquery(c) or any(o.depth != 1 for o in outers):
                return None
            if not outers:
                local.append(c)
                continue
            key = _correlated_key(c)
            if key is None:
                return None
            left_keys.append(_outer_to_col(key[0]))
            right_keys.append(key[1])
        if not left_keys or not self.join_beats_lookups(source, inner, right_keys[0]):
            return None
        grouped_input = p.Filter(inner, _and_all(local), inner.columns) if local else inner
        k, width = len(right_keys), len(source.columns)
        grouped = p.Aggregate(grouped_input, right_keys, list(aggregate.aggs),
                              [p.Column(format_expr(g), g.type) for g in right_keys] + list(aggregate.columns))
        names = [format_expr(g) for g in right_keys]
        join = p.Join("LEFT", source, grouped, left_keys,
                      [Col(i, g.type, names[i]) for i, g in enumerate(right_keys)], None,
                      source.columns + grouped.columns)
        matched = Op("NOTNULL", (Col(width, right_keys[0].type, names[0]),), BOOLEAN)
        present = _substitute(value, [Col(width + k + i, a.type, format_expr(a)) for i, a in enumerate(aggregate.aggs)])
        empty = _substitute(value, [Const(0 if a.name == "COUNT" else None, a.type) for a in aggregate.aggs])
        return join, Op("CASE", (matched, present, empty), sub.type)

    def join_beats_lookups(self, source, inner, key):
        """Whether grouping the whole inner table once is cheaper than a lookup per outer row."""
        inner_rows = self.estimate(inner)
        per_row = inner_rows
        if isinstance(inner, p.TableScan) and isinstance(key, Col):
            table = inner.table
            leading = [table.primary_key[:1]] + [index.columns[:1] for index in table.indexes]
            if [key.index] in leading:
                distinct = self.distinct_values(inner, key.index) or max(inner_rows / 10, 1.0)
                per_row = math.log2(inner_rows + 2) + inner_rows / distinct
        return self.estimate(source) * per_row > inner_rows + self.estimate(source)

    # ---- 2. predicate pushdown ------------------------------------------------------

    def push(self, node, preds):
        """Return node with the conjuncts `preds` applied as deep inside it as is correct."""
        if isinstance(node, p.Filter):
            source, remaining = node.input, []
            for c in _conjuncts(node.predicate):
                joined = self.semi_join(source, c)
                if joined is not None:
                    source = joined
                    continue
                source, c = self.attach_scalars(source, c)
                remaining.append(c)
            result = self.push(source, preds + remaining)
            if len(source.columns) != len(node.columns):  # joined aggregates widened the row: trim it back
                result = p.Project(result, [Col(i, c.type, c.name) for i, c in enumerate(node.columns)], node.columns)
            return result
        if isinstance(node, p.Join):
            return self.push_join(node, preds)
        if isinstance(node, p.Project):
            source, exprs = node.input, []
            for e in node.exprs:
                source, e = self.attach_scalars(source, e)
                exprs.append(e)
            node = replace(node, input=source, exprs=exprs)
            down, keep = [], []
            for c in preds:
                if _movable(c) and not any(_has_subquery(node.exprs[i]) for i in column_indexes(c)):
                    down.append(_substitute(c, node.exprs))
                else:
                    keep.append(c)
            return self.wrap(replace(node, input=self.push(node.input, down)), keep)
        if isinstance(node, p.Aggregate):
            down, keep = [], []
            for c in preds:
                cols = column_indexes(c)
                if _movable(c) and all(i < len(node.groups) for i in cols):
                    down.append(_substitute(c, node.groups))
                else:
                    keep.append(c)
            return self.wrap(replace(node, input=self.push(node.input, down)), keep)
        if isinstance(node, (p.Sort, p.Distinct)):
            return replace(node, input=self.push(node.input, preds))
        if isinstance(node, p.SetOp):
            down = [c for c in preds if _movable(c)]
            keep = [c for c in preds if not _movable(c)]
            return self.wrap(replace(node, left=self.push(node.left, down), right=self.push(node.right, down)), keep)
        node = _with_inputs(node, [self.push(child, []) for child in p.inputs(node)])
        return self.wrap(node, preds)

    def push_join(self, node, preds):
        kind, width = node.kind, len(node.left.columns)
        inner = kind in ("INNER", "CROSS")
        left_preds, right_preds, keep, join_conditions = [], [], [], []
        for c in preds:  # conditions from above the join (WHERE)
            side = _side(c, width)
            if not _movable(c):
                keep.append(c)
            elif side in "LN" and kind in ("INNER", "CROSS", "LEFT", "SEMI", "ANTI"):
                left_preds.append(c)
            elif side == "R" and kind in ("INNER", "CROSS", "RIGHT"):
                right_preds.append(shift(c, -width))
            elif side == "B" and inner:
                join_conditions.append(c)
            else:
                keep.append(c)
        for c in _conjuncts(node.residual) if node.residual is not None else []:  # the join's own ON conditions
            side = _side(c, width)
            if not _movable(c):
                join_conditions.append(c)
            elif inner and side in "LN":
                left_preds.append(c)
            elif side == "R" and kind in ("INNER", "CROSS", "LEFT", "SEMI", "ANTI"):
                right_preds.append(shift(c, -width))
            elif side == "L" and kind == "RIGHT":
                left_preds.append(c)
            else:
                join_conditions.append(c)
        left_keys, right_keys, residual = list(node.left_keys), list(node.right_keys), []
        for c in join_conditions:
            key = _equi_key(c, width)
            if key is not None:
                left_keys.append(key[0])
                right_keys.append(key[1])
            else:
                residual.append(c)
        if kind == "CROSS" and (left_keys or residual):
            kind = "INNER"
        join = replace(node, kind=kind, left=self.push(node.left, left_preds), right=self.push(node.right, right_preds),
                       left_keys=left_keys, right_keys=right_keys, residual=_and_all(residual))
        return self.wrap(join, keep)

    def wrap(self, node, preds):
        if not preds:
            return node
        if isinstance(node, p.TableScan):
            node, preds = self.access_path(node, preds)
            if not preds:
                return node
        return p.Filter(node, _and_all(preds), node.columns)

    # ---- 3. access paths -------------------------------------------------------------

    def access_path(self, scan, preds):
        """Choose between a full scan and index scans for `preds` on one table.

        Returns (scan node, conditions still to check)."""
        table = scan.table
        rows = self.table_rows(table)
        best_cost, best = rows, (scan, preds)
        options = [(None, table.primary_key)] if table.primary_key else []
        options += [(index, index.columns) for index in table.indexes]
        for index, key_columns in options:
            used, eq, lo, hi, points, fraction = set(), [], None, None, None, 1.0
            for column in key_columns:
                match = self.find_bound(table, preds, column, ("=",), used)
                if match is not None:
                    i, _, value = match
                    used.add(i)
                    eq.append(value)
                    fraction *= self.selectivity(preds[i], scan)
                    continue
                for i, op, value in self.find_bounds(table, preds, column, used):
                    if op in (">", ">=") and lo is None:
                        lo = (value, op == ">=")
                    elif op in ("<", "<=") and hi is None:
                        hi = (value, op == "<=")
                    else:
                        continue
                    used.add(i)
                    fraction *= self.selectivity(preds[i], scan)
                if lo is None and hi is None:
                    match = self.find_in_list(table, preds, column, used)
                    if match is not None:
                        i, points = match
                        used.add(i)
                        fraction *= self.selectivity(preds[i], scan)
                break
            if not used:
                continue
            estimate = rows * fraction
            if len(eq) == len(key_columns) and (index is None or index.unique):
                estimate = min(estimate, 1.0)
            cost = math.log2(rows + 2) + estimate * (1 if index is None else SECONDARY_LOOKUP_COST)
            if cost < best_cost:
                node = p.IndexScan(table, index, eq, lo, hi, scan.columns, points)
                node.est_rows = estimate
                best_cost, best = cost, (node, [c for i, c in enumerate(preds) if i not in used])
        return best

    def find_in_list(self, table, preds, column, used):
        """(position, values) of a `column IN (values)` condition whose values do not depend on the row."""
        column_type = table.columns[column].type
        for i, c in enumerate(preds):
            if (i not in used and isinstance(c, Op) and c.op == "IN" and isinstance(c.args[0], Col)
                    and c.args[0].index == column
                    and all(_is_bound_value(v) and (v.type in (column_type, NULL)
                                                    or (column_type == REAL and v.type == INTEGER))
                            for v in c.args[1:])):
                return i, list(c.args[1:])
        return None

    def find_bound(self, table, preds, column, ops, used):
        for i, op, value in self.find_bounds(table, preds, column, used):
            if op in ops:
                return i, op, value
        return None

    def find_bounds(self, table, preds, column, used):
        """Conditions `column <op> value` where value does not depend on the table's own row."""
        column_type = table.columns[column].type
        for i, c in enumerate(preds):
            if i in used or not isinstance(c, Op) or c.op not in _FLIP:
                continue
            a, b = c.args
            for col, value, op in ((a, b, c.op), (b, a, _FLIP[c.op])):
                if (isinstance(col, Col) and col.index == column and _is_bound_value(value)
                        and (value.type in (column_type, NULL) or (column_type == REAL and value.type == INTEGER))):
                    yield i, op, value
                    break

    # ---- 4. join ordering --------------------------------------------------------------

    def reorder(self, node):
        if isinstance(node, p.Join) and node.kind == "INNER":
            relations, conditions = [], []
            if _flatten(node, 0, relations, conditions) and 2 <= len(relations) <= MAX_REORDER_RELATIONS:
                relations = [(self.reorder(plan), offset) for plan, offset in relations]
                return self.join_order(relations, conditions, node.columns)
        return _with_inputs(node, [self.reorder(child) for child in p.inputs(node)])

    def join_order(self, relations, conditions, columns):
        n = len(relations)
        owner = {}
        for r, (plan, offset) in enumerate(relations):
            for k in range(len(plan.columns)):
                owner[offset + k] = r
        masks = [_mask(c, owner) for c in conditions]
        leftover = [c for c, m in zip(conditions, masks) if bin(m).count("1") <= 1]
        selectivity = [self.join_selectivity(c, relations, owner) for c in conditions]
        rows, cost, choice = {}, {}, {}
        for r, (plan, _) in enumerate(relations):
            rows[1 << r] = cost[1 << r] = self.estimate(plan)
        full = (1 << n) - 1
        for subset in sorted(range(1, full + 1), key=lambda m: bin(m).count("1")):
            if subset in rows:
                continue
            product = 1.0
            for r in range(n):
                if subset >> r & 1:
                    product *= rows[1 << r]
            for m, s in zip(masks, selectivity):
                if m and m & subset == m and bin(m).count("1") > 1:
                    product *= s
            rows[subset] = max(product, 1.0)
            best = None
            for connected_only in (True, False):
                part = (subset - 1) & subset
                while part:
                    other = subset ^ part
                    if part in cost and other in cost:
                        applicable = [ci for ci, m in enumerate(masks)
                                      if m and m & subset == m and m & part != m and m & other != m]
                        if applicable or not connected_only:
                            hashed = any(_equi_between(conditions[ci], owner, part, other) for ci in applicable)
                            work = (rows[part] + 2 * rows[other]) if hashed else rows[part] * rows[other]
                            total = cost[part] + cost[other] + work + rows[subset]
                            if best is None or total < best[0]:
                                best = (total, part, other, applicable)
                    part = (part - 1) & subset
                if best is not None:
                    break
            cost[subset], choice[subset] = best[0], best[1:]

        def build(subset):
            if subset not in choice:
                plan, offset = relations[subset.bit_length() - 1]
                return plan, list(range(offset, offset + len(plan.columns)))
            part, other, applicable = choice[subset]
            left, left_layout = build(part)
            right, right_layout = build(other)
            left_pos = {g: k for k, g in enumerate(left_layout)}
            right_pos = {g: k for k, g in enumerate(right_layout)}
            both_pos = {g: k for k, g in enumerate(left_layout + right_layout)}
            left_keys, right_keys, residual = [], [], []
            for ci in applicable:
                c = conditions[ci]
                sides = _equi_sides(c, owner, part, other)
                if sides is not None:
                    left_keys.append(_remap(sides[0], left_pos))
                    right_keys.append(_remap(sides[1], right_pos))
                else:
                    residual.append(_remap(c, both_pos))
            join = p.Join("INNER", left, right, left_keys, right_keys, _and_all(residual), left.columns + right.columns)
            join.est_rows = rows[subset]
            return join, left_layout + right_layout

        plan, layout = build(full)
        if layout != list(range(len(columns))):
            position = {g: k for k, g in enumerate(layout)}
            plan = p.Project(plan, [Col(position[g], c.type, c.name) for g, c in enumerate(columns)], columns)
        if leftover:
            plan = p.Filter(plan, _and_all(leftover), columns)
        return plan

    def join_selectivity(self, c, relations, owner):
        if isinstance(c, Op) and c.op == "=" and all(isinstance(a, Col) for a in c.args):
            distinct = []
            for col in c.args:
                plan, offset = relations[owner[col.index]]
                distinct.append(self.distinct_values(plan, col.index - offset))
            known = [d for d in distinct if d]
            if known:
                return 1 / max(known)
            return 1 / max(1.0, min(self.estimate(relations[owner[a.index]][0]) for a in c.args))
        return DEFAULT_OTHER

    # ---- estimates -----------------------------------------------------------------------

    def table_rows(self, table):
        return float(table.stats.rows) if table.stats is not None else DEFAULT_ROWS

    def column_source(self, node, i):
        """The (table, column position) a plan output column reads, if it is a plain column."""
        if isinstance(node, (p.TableScan, p.IndexScan)):
            return node.table, i
        if isinstance(node, (p.Filter, p.Sort, p.Limit, p.Distinct)):
            return self.column_source(node.input, i)
        if isinstance(node, p.Project):
            e = node.exprs[i]
            return self.column_source(node.input, e.index) if isinstance(e, Col) else None
        if isinstance(node, p.Join):
            width = len(node.left.columns)
            return self.column_source(node.left, i) if i < width else self.column_source(node.right, i - width)
        if isinstance(node, p.Aggregate) and i < len(node.groups) and isinstance(node.groups[i], Col):
            return self.column_source(node.input, node.groups[i].index)
        return None

    def column_stats(self, node, i):
        source = self.column_source(node, i)
        if source is None or source[0].stats is None:
            return None, None
        table, column = source
        return table.stats.columns[column], table.stats.rows

    def distinct_values(self, node, i):
        stats, _ = self.column_stats(node, i)
        return max(stats.distinct, 1) if stats is not None else None

    def selectivity(self, e, node):
        """Estimated fraction of `node`'s rows for which e is TRUE."""
        if isinstance(e, Const):
            return 1.0 if e.value is True else 0.0
        if not isinstance(e, Op):
            return DEFAULT_OTHER
        if e.op == "AND":
            return self.selectivity(e.args[0], node) * self.selectivity(e.args[1], node)
        if e.op == "OR":
            a, b = (self.selectivity(x, node) for x in e.args)
            return a + b - a * b
        if e.op == "NOT":
            return 1.0 - self.selectivity(e.args[0], node)
        if e.op in ("ISNULL", "NOTNULL") and isinstance(e.args[0], Col):
            stats, rows = self.column_stats(node, e.args[0].index)
            if stats is None or not rows:
                return 0.1 if e.op == "ISNULL" else 0.9
            nulls = stats.nulls / rows
            return nulls if e.op == "ISNULL" else 1 - nulls
        if e.op == "IN" and isinstance(e.args[0], Col):
            distinct = self.distinct_values(node, e.args[0].index)
            return min(1.0, (len(e.args) - 1) * (1 / distinct if distinct else DEFAULT_EQUALITY))
        if e.op in _FLIP:
            a, b = e.args
            col, value, op = (a, b, e.op) if isinstance(a, Col) else (b, a, _FLIP[e.op])
            if not isinstance(col, Col):
                return DEFAULT_EQUALITY if e.op == "=" else DEFAULT_RANGE
            stats, rows = self.column_stats(node, col.index)
            if op == "=":
                if isinstance(value, Col):
                    other = self.distinct_values(node, value.index)
                    known = [d for d in (self.distinct_values(node, col.index), other) if d]
                    return 1 / max(known) if known else DEFAULT_EQUALITY
                if stats is None or not rows:
                    return DEFAULT_EQUALITY
                if isinstance(value, Const) and value.value is not None:
                    return stats.fraction_equal(value.value, rows)
                return (1 - stats.nulls / rows) / max(stats.distinct, 1)
            if op == "!=":
                if stats is None or not rows:
                    return 1 - DEFAULT_EQUALITY
                equal = stats.fraction_equal(value.value, rows) if isinstance(value, Const) else 1 / max(stats.distinct, 1)
                return max(1 - stats.nulls / rows - equal, 0.0)
            if stats is None or not rows or not isinstance(value, Const) or value.value is None:
                return DEFAULT_RANGE
            non_null = 1 - stats.nulls / rows
            below = stats.fraction_below(value.value, rows)
            equal = stats.fraction_equal(value.value, rows)
            fraction = {"<": below, "<=": below + equal, ">": non_null - below - equal, ">=": non_null - below}[op]
            return min(max(fraction, 0.0), 1.0)
        if e.op == "LIKE":
            return 0.1
        return DEFAULT_OTHER

    def estimate(self, node):
        known = getattr(node, "est_rows", None)
        if known is not None:
            return known
        cached = self._estimates.get(id(node))
        if cached is not None:
            return cached[1]
        rows = self._estimate(node)
        self._estimates[id(node)] = (node, rows)
        return rows

    def _estimate(self, node):
        if isinstance(node, p.TableScan):
            return self.table_rows(node.table)
        if isinstance(node, p.OneRow):
            return 1.0
        if isinstance(node, p.Values):
            return float(len(node.rows))
        if isinstance(node, p.Filter):
            return max(self.estimate(node.input) * self.selectivity(node.predicate, node.input), 1.0)
        if isinstance(node, (p.Project, p.Sort, p.Window)):
            return self.estimate(node.input)
        if isinstance(node, p.Distinct):
            return self.estimate(node.input)
        if isinstance(node, p.Limit):
            rows = max(self.estimate(node.input) - node.offset, 0.0)
            return min(rows, node.limit) if node.limit is not None else rows
        if isinstance(node, p.Aggregate):
            source = self.estimate(node.input)
            if not node.groups:
                return 1.0
            groups = 1.0
            for g in node.groups:
                distinct = self.distinct_values(node.input, g.index) if isinstance(g, Col) else None
                groups *= distinct or max(source / 10, 1.0)
            return min(groups, source)
        if isinstance(node, p.Join):
            left, right = self.estimate(node.left), self.estimate(node.right)
            if node.kind in ("SEMI", "ANTI"):
                return max(left * 0.5, 1.0)
            fraction = 1.0
            width = len(node.left.columns)
            for lk, rk in zip(node.left_keys, node.right_keys):
                distinct = [d for d in (self.distinct_values(node.left, lk.index) if isinstance(lk, Col) else None,
                                        self.distinct_values(node.right, rk.index) if isinstance(rk, Col) else None) if d]
                fraction /= max(distinct) if distinct else max(min(left, right), 1.0)
            if node.residual is not None:
                fraction *= self.selectivity(node.residual, node) if width else DEFAULT_OTHER
            matched = max(left * right * fraction, 1.0)
            return {"LEFT": max(matched, left), "RIGHT": max(matched, right),
                    "FULL": max(matched, left, right)}.get(node.kind, matched)
        if isinstance(node, p.SetOp):
            left, right = self.estimate(node.left), self.estimate(node.right)
            return {"UNION": left + right, "INTERSECT": min(left, right), "EXCEPT": left}[node.op]
        if isinstance(node, p.RecursiveCTE):
            return self.estimate(node.anchor) * 10
        return 100.0

    def annotate(self, node):
        node.est_rows = self.estimate(node)
        for child in p.inputs(node):
            self.annotate(child)


# ---- helpers ---------------------------------------------------------------------------------


def _conjuncts(e):
    if isinstance(e, Op) and e.op == "AND":
        return _conjuncts(e.args[0]) + _conjuncts(e.args[1])
    return [e]


def _and_all(conditions):
    result = None
    for c in conditions:
        result = c if result is None else Op("AND", (result, c), BOOLEAN)
    return result


def _has_subquery(e):
    return any(isinstance(x, SubqueryExpr) for x in walk(e))


def _has_outer_or_subquery(e):
    return any(isinstance(x, (Outer, SubqueryExpr)) for x in walk(e))


def _movable(c):
    """A condition can move to another row layout unless a correlated subquery reads its row positions."""
    return not any(isinstance(x, SubqueryExpr) and x.correlated for x in walk(c))


def _side(c, width):
    cols = column_indexes(c)
    if not cols:
        return "N"
    if max(cols) < width:
        return "L"
    if min(cols) >= width:
        return "R"
    return "B"


def _equi_key(c, width):
    """(left key, right key) if c is `left-only expr = right-only expr`."""
    if not isinstance(c, Op) or c.op != "=" or _has_subquery(c):
        return None
    a, b = c.args
    sa, sb = _side(a, width), _side(b, width)
    if sa == "L" and sb == "R":
        return a, shift(b, -width)
    if sa == "R" and sb == "L":
        return b, shift(a, -width)
    return None


def _is_bound_value(e):
    """An expression whose value is fixed for a whole scan: constants and outer references."""
    return not any(isinstance(x, (Col, SubqueryExpr)) for x in walk(e))


def _substitute(e, exprs):
    if isinstance(e, Col):
        return exprs[e.index]
    return map_children(e, lambda c: _substitute(c, exprs))


def _replace_node(e, target, replacement):
    if e is target:
        return replacement
    return map_children(e, lambda c: _replace_node(c, target, replacement))


def _remap(e, positions):
    if isinstance(e, Col):
        return Col(positions[e.index], e.type, e.name)
    return map_children(e, lambda c: _remap(c, positions))


def _outer_to_col(e):
    if isinstance(e, Outer):
        return Col(e.index, e.type, e.name)
    return map_children(e, _outer_to_col)


def _to_combined(e, width):
    """Rewrite a correlated condition over the semi join's combined row: outer row, then inner row."""
    if isinstance(e, Outer):
        return Col(e.index, e.type, e.name)
    if isinstance(e, Col):
        return Col(width + e.index, e.type, e.name)
    return map_children(e, lambda c: _to_combined(c, width))


def _correlated_key(c):
    """(outer side, inner side) if c is an equality between an outer-only and an inner-only expression."""
    if not isinstance(c, Op) or c.op != "=":
        return None
    a, b = c.args

    def outer_only(x):
        nodes = list(walk(x))
        return any(isinstance(n, Outer) for n in nodes) and not any(isinstance(n, Col) for n in nodes)

    def inner_only(x):
        return not any(isinstance(n, Outer) for n in walk(x)) and bool(column_indexes(x))

    if outer_only(a) and inner_only(b):
        return a, b
    if outer_only(b) and inner_only(a):
        return b, a
    return None


def _reaches_outside(plan):
    """Whether any expression in the plan reads an enclosing query or runs a subquery."""
    nodes = [plan]
    while nodes:
        node = nodes.pop()
        if any(_has_outer_or_subquery(e) for e in p.expressions(node)):
            return True
        if isinstance(node, p.RecursiveCTE):
            return True
        nodes.extend(p.inputs(node))
    return False


def _flatten(node, offset, relations, conditions):
    """Collect the tables and conditions of a cluster of inner joins, in one global column numbering."""
    if isinstance(node, p.Join) and node.kind == "INNER":
        width = len(node.left.columns)
        for lk, rk in zip(node.left_keys, node.right_keys):
            conditions.append(Op("=", (shift(lk, offset), shift(rk, offset + width)), BOOLEAN))
        if node.residual is not None:
            if not _movable(node.residual):
                return False
            conditions.extend(shift(c, offset) for c in _conjuncts(node.residual))
        return _flatten(node.left, offset, relations, conditions) and \
            _flatten(node.right, offset + width, relations, conditions)
    relations.append((node, offset))
    return True


def _mask(e, owner):
    """Bit set of the relations whose columns e reads."""
    mask = 0
    for i in column_indexes(e):
        mask |= 1 << owner[i]
    return mask


def _equi_sides(c, owner, part, other):
    """(side within `part`, side within `other`) if c is an equality joining the two sets."""
    if not isinstance(c, Op) or c.op != "=" or _has_subquery(c):
        return None
    a, b = c.args
    ma, mb = _mask(a, owner), _mask(b, owner)
    if ma and mb:
        if ma & part == ma and mb & other == mb:
            return a, b
        if mb & part == mb and ma & other == ma:
            return b, a
    return None


def _equi_between(c, owner, part, other):
    return _equi_sides(c, owner, part, other) is not None


def _with_inputs(node, children):
    if isinstance(node, (p.Join, p.SetOp)):
        return replace(node, left=children[0], right=children[1])
    if isinstance(node, p.RecursiveCTE):
        return replace(node, anchor=children[0], recursive=children[1])
    if children:
        return replace(node, input=children[0])
    return node
