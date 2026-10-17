"""Query plan nodes. Each node produces rows; `columns` describes them.

The binder builds this tree, the optimizer rewrites it and the executor runs
it. explain() renders it for EXPLAIN. The optimizer records its row estimate
on each node it plans as `est_rows`.
"""

from dataclasses import dataclass, field

from .expr import SubqueryExpr, format_expr, walk


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str


@dataclass(eq=False)
class OneRow:
    """A single empty row: the input of SELECT without FROM."""
    columns: list = field(default_factory=list)


@dataclass(eq=False)
class TableScan:
    table: object  # catalog.TableSchema
    columns: list


@dataclass(eq=False)
class IndexScan:
    """Rows of `table` found through an index (None = the primary key).

    The first len(eq) index columns equal the eq values; the next column, if
    lo or hi is set, lies in that range, or if points is set, equals one of
    those values (an IN list: one lookup per distinct value). lo and hi are
    (expr, inclusive). All bound expressions are constants or outer
    references, evaluated once per scan.
    """
    table: object
    index: object  # catalog.IndexSchema, or None for the table's primary key
    eq: list
    lo: tuple | None
    hi: tuple | None
    columns: list
    points: list | None = None

    @property
    def key_columns(self):
        return self.index.columns if self.index is not None else self.table.primary_key


@dataclass(eq=False)
class WorkTableScan:
    """The rows of the previous iteration of a recursive CTE."""
    cte_id: int
    name: str
    columns: list


@dataclass(eq=False)
class Values:
    rows: list  # [[expr]]
    columns: list


@dataclass(eq=False)
class Filter:
    input: object
    predicate: object
    columns: list


@dataclass(eq=False)
class Project:
    input: object
    exprs: list
    columns: list


@dataclass(eq=False)
class Join:
    """kind: INNER, LEFT, RIGHT, FULL, CROSS, SEMI or ANTI. Rows are left + right,
    except SEMI (left rows with a match) and ANTI (left rows without one).

    left_keys[i] (over left rows) = right_keys[i] (over right rows) are the
    equality conditions a hash join can use; residual is any remaining
    condition, evaluated on the combined row.
    """
    kind: str
    left: object
    right: object
    left_keys: list
    right_keys: list
    residual: object
    columns: list


@dataclass(eq=False)
class Aggregate:
    """Output rows are group values followed by aggregate results."""
    input: object
    groups: list
    aggs: list  # [AggCall]
    columns: list


@dataclass(eq=False)
class Window:
    """Output rows are input rows followed by one value per window call."""
    input: object
    calls: list  # [WindowCall]
    columns: list


@dataclass(eq=False)
class Sort:
    input: object
    keys: list  # [(expr, desc, nulls_first)]
    columns: list


@dataclass(eq=False)
class Limit:
    input: object
    limit: int | None
    offset: int
    columns: list


@dataclass(eq=False)
class Distinct:
    input: object
    columns: list


@dataclass(eq=False)
class SetOp:
    op: str  # UNION, INTERSECT, EXCEPT
    all: bool
    left: object
    right: object
    columns: list


@dataclass(eq=False)
class RecursiveCTE:
    anchor: object
    recursive: object  # reads the previous iteration through WorkTableScan(cte_id)
    all: bool
    cte_id: int
    name: str
    columns: list


def inputs(node):
    if isinstance(node, (Join, SetOp)):
        return [node.left, node.right]
    if isinstance(node, RecursiveCTE):
        return [node.anchor, node.recursive]
    child = getattr(node, "input", None)
    return [child] if child is not None else []


def expressions(node):
    if isinstance(node, Filter):
        return [node.predicate]
    if isinstance(node, Project):
        return list(node.exprs)
    if isinstance(node, Join):
        return node.left_keys + node.right_keys + ([node.residual] if node.residual is not None else [])
    if isinstance(node, Aggregate):
        return node.groups + node.aggs
    if isinstance(node, Window):
        return list(node.calls)
    if isinstance(node, Sort):
        return [key for key, _, _ in node.keys]
    if isinstance(node, Values):
        return [e for row in node.rows for e in row]
    return []


def describe(node):
    if isinstance(node, OneRow):
        return "OneRow"
    if isinstance(node, TableScan):
        return f"TableScan {node.table.name}"
    if isinstance(node, IndexScan):
        names = [node.table.columns[i].name for i in node.key_columns]
        conditions = [f"{names[i]} = {format_expr(v)}" for i, v in enumerate(node.eq)]
        column = names[len(node.eq)] if len(node.eq) < len(names) else None
        if node.lo is not None:
            conditions.append(f"{column} {'>=' if node.lo[1] else '>'} {format_expr(node.lo[0])}")
        if node.hi is not None:
            conditions.append(f"{column} {'<=' if node.hi[1] else '<'} {format_expr(node.hi[0])}")
        if node.points is not None:
            conditions.append(f"{column} IN ({', '.join(map(format_expr, node.points))})")
        using = node.index.name if node.index is not None else "primary key"
        return f"IndexScan {node.table.name} using {using} ({' AND '.join(conditions)})"
    if isinstance(node, WorkTableScan):
        return f"WorkTableScan {node.name}"
    if isinstance(node, Values):
        return f"Values ({len(node.rows)} rows)"
    if isinstance(node, Filter):
        return f"Filter {format_expr(node.predicate)}"
    if isinstance(node, Project):
        return "Project " + ", ".join(
            format_expr(e) if format_expr(e) == c.name else f"{format_expr(e)} AS {c.name}"
            for e, c in zip(node.exprs, node.columns))
    if isinstance(node, Join):
        if node.left_keys:
            on = ", ".join(f"{format_expr(a)} = {format_expr(b)}" for a, b in zip(node.left_keys, node.right_keys))
            text = f"HashJoin {node.kind} on {on}"
        else:
            text = f"NestedLoopJoin {node.kind}"
        return text + (f" filter {format_expr(node.residual)}" if node.residual is not None else "")
    if isinstance(node, Aggregate):
        groups = ", ".join(map(format_expr, node.groups))
        aggs = ", ".join(map(format_expr, node.aggs))
        return "HashAggregate" + (f" group by {groups}" if groups else "") + (f" compute {aggs}" if aggs else "")
    if isinstance(node, Window):
        return "Window " + ", ".join(map(format_expr, node.calls))
    if isinstance(node, Sort):
        return "Sort " + ", ".join(format_expr(k) + (" DESC" if d else "") for k, d, _ in node.keys)
    if isinstance(node, Limit):
        return f"Limit {node.limit if node.limit is not None else 'ALL'}" + (f" offset {node.offset}" if node.offset else "")
    if isinstance(node, Distinct):
        return "Distinct"
    if isinstance(node, SetOp):
        return f"{node.op}{' ALL' if node.all else ''}"
    if isinstance(node, RecursiveCTE):
        return f"RecursiveCTE {node.name}{' (UNION ALL)' if node.all else ' (UNION)'}"
    return type(node).__name__


def plan_tree(plan, stats=None):
    """The plan as nested dicts for a UI: label, operator, estimate, and actual rows and time if traced."""
    counter = iter(range(1_000_000))

    def visit(node):
        entry = {"id": next(counter), "operator": type(node).__name__, "label": describe(node),
                 "est_rows": getattr(node, "est_rows", None), "children": [], "subplans": []}
        if stats is not None:
            rows, seconds = stats.get(id(node), (0, 0.0))
            entry["rows"], entry["ms"] = rows, round(seconds * 1000, 3)
        for e in expressions(node):
            for sub in walk(e):
                if isinstance(sub, SubqueryExpr):
                    entry["subplans"].append({"kind": sub.kind.lower(), "correlated": sub.correlated,
                                              "plan": visit(sub.plan)})
        entry["children"] = [visit(child) for child in inputs(node)]
        return entry

    return visit(plan)


def explain(plan, stats=None):
    """Render the plan as indented lines. stats maps id(node) -> (rows, seconds) for EXPLAIN ANALYZE."""
    lines = []

    def visit(node, depth, prefix):
        text = describe(node)
        details = []
        if getattr(node, "est_rows", None) is not None:
            details.append(f"est={node.est_rows:,.0f}")
        if stats is not None:
            rows, seconds = stats.get(id(node), (0, 0.0))
            details.append(f"rows={rows} time={seconds * 1000:.3f}ms")
        if details:
            text += f"  ({' '.join(details)})"
        lines.append("  " * depth + prefix + text)
        for e in expressions(node):
            for sub in walk(e):
                if isinstance(sub, SubqueryExpr):
                    lines.append("  " * (depth + 1) + f"SubPlan ({sub.kind.lower()}"
                                 + (", correlated)" if sub.correlated else ")"))
                    visit(sub.plan, depth + 2, "-> ")
        for child in inputs(node):
            visit(child, depth + 1, "-> ")

    visit(plan, 0, "")
    return lines
