"""Bound expressions: the binder's output, with every name resolved and every type known.

A bound expression refers to columns by position in the row it is evaluated
against. Nodes are immutable and compare by value (except subqueries), so the
binder can match `a + 1` in SELECT against `a + 1` in GROUP BY with ==.
"""

import datetime
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ..errors import ExecutionError
from ..values import Jsonb, array_text, parse_array
from . import containers as ct
from . import datetimes as dt
from .datetimes import Interval

INTEGER, REAL, TEXT, BOOLEAN, NULL = "INTEGER", "REAL", "TEXT", "BOOLEAN", "NULL"
DECIMAL, DATE, TIMESTAMP, INTERVAL, JSONB = "NUMERIC", "DATE", "TIMESTAMP", "INTERVAL", "JSONB"
NUMERIC = (INTEGER, REAL, DECIMAL)
TEMPORAL = (DATE, TIMESTAMP)

_INT_MIN, _INT_MAX = -(2**63), 2**63 - 1
MAX_VALUE_LENGTH = 50_000_000  # characters of a text, elements of an array (PostgreSQL stops at 1 GB)


def check_size(length, what="the result"):
    """Refuse to build a value longer than MAX_VALUE_LENGTH (before building it, where the size is known)."""
    if length > MAX_VALUE_LENGTH:
        raise ExecutionError(f"{what} would be {length:,} long; the limit is {MAX_VALUE_LENGTH:,}")


def bounded(v):
    """v, if it is within MAX_VALUE_LENGTH."""
    if isinstance(v, (str, tuple)):
        check_size(len(v))
    elif isinstance(v, Jsonb):
        check_size(len(v.text))
    return v


def is_array(t):
    return t.endswith("[]")


def element(t):
    """The element type of an array type: INTEGER for INTEGER[]."""
    return t[:-2]


def unify(a, b):
    """The common type of two types, or None if they cannot be mixed."""
    if a == NULL:
        return b
    if b in (NULL, a):
        return a
    if is_array(a) and is_array(b):
        inner = unify(element(a), element(b))
        return inner + "[]" if inner is not None else None
    if a in NUMERIC and b in NUMERIC:
        return REAL if REAL in (a, b) else DECIMAL  # as in PostgreSQL: numeric with float8 is float8
    if a in TEMPORAL and b in TEMPORAL:
        return TIMESTAMP
    return None


def literal_type(value):
    if value is None:
        return NULL
    if isinstance(value, bool):
        return BOOLEAN
    if isinstance(value, int):
        return INTEGER
    if isinstance(value, float):
        return REAL
    if isinstance(value, Decimal):
        return DECIMAL
    if isinstance(value, datetime.datetime):
        return TIMESTAMP
    if isinstance(value, datetime.date):
        return DATE
    if isinstance(value, Interval):
        return INTERVAL
    if isinstance(value, Jsonb):
        return JSONB
    if isinstance(value, tuple):
        inner = NULL
        for item in value:
            inner = unify(inner, literal_type(item)) or inner
        return (TEXT if inner == NULL else inner) + "[]"
    return TEXT


def assignable(source, target):
    """Whether a value of type `source` can be stored in a column of type `target`. Text converts to dates,
    timestamps and numerics when it is stored (and fails then if it is not one), as PostgreSQL's untyped
    string literals do."""
    if source in (target, NULL):
        return True
    if is_array(target):
        return source == TEXT or (is_array(source) and assignable(element(source), element(target)))
    if target in (JSONB, INTERVAL):
        return source == TEXT
    if target in (REAL, DECIMAL):
        return source in NUMERIC or (target == DECIMAL and source == TEXT)
    if target in TEMPORAL:
        return source in TEMPORAL or source == TEXT
    return False


# ---- nodes ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Const:
    value: object
    type: str


@dataclass(frozen=True, slots=True)
class Col:
    """A column of the row being evaluated."""
    index: int
    type: str
    name: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class Outer:
    """A column of an enclosing query's row (a correlated reference). depth 1 is the nearest."""
    depth: int
    index: int
    type: str
    name: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class Op:
    """An operator or scalar function. Argument conventions:

    AND OR = != < <= > >= + - * / % ||   two arguments
    NOT NEG ISNULL NOTNULL CAST           one argument (CAST converts to `type`)
    DISTINCT NOTDISTINCT                  IS [NOT] DISTINCT FROM: two arguments, NULL equal to NULL
    LIKE                                  value, pattern
    IN                                    value, then the list items
    CASE                                  cond1, result1, cond2, result2, ..., else
    COALESCE NULLIF and named functions   their arguments
    """
    op: str
    args: tuple
    type: str


@dataclass(frozen=True, slots=True)
class AggCall:
    name: str
    args: tuple  # empty for COUNT(*)
    distinct: bool
    type: str
    order: tuple = ()  # ((expr, desc, nulls_first), ...): the order rows are fed in, as string_agg(x, ',' ORDER BY y)
    filter: object = None  # FILTER (WHERE ...): only rows where it is true count


@dataclass(frozen=True, slots=True)
class WindowCall:
    name: str
    args: tuple
    partition: tuple
    order: tuple  # ((expr, desc, nulls_first), ...)
    frame: tuple | None  # (mode, start_kind, start_offset, end_kind, end_offset); None = default frame
    type: str


@dataclass(eq=False, slots=True)
class SubqueryExpr:
    """SCALAR, EXISTS or IN (with `arg` as the value looked up). Compared by identity."""
    kind: str
    plan: object
    type: str
    arg: object = None
    correlated: bool = False


def children(e):
    if isinstance(e, AggCall):
        return e.args + tuple(item[0] for item in e.order) + ((e.filter,) if e.filter is not None else ())
    if isinstance(e, Op):
        return e.args
    if isinstance(e, WindowCall):
        return e.args + e.partition + tuple(item[0] for item in e.order)
    if isinstance(e, SubqueryExpr) and e.arg is not None:
        return (e.arg,)
    return ()


def map_children(e, f):
    """Rebuild e with f applied to each direct child expression."""
    if isinstance(e, Op):
        return Op(e.op, tuple(f(a) for a in e.args), e.type)
    if isinstance(e, AggCall):
        return AggCall(e.name, tuple(f(a) for a in e.args), e.distinct, e.type,
                       tuple((f(x), desc, nf) for x, desc, nf in e.order),
                       f(e.filter) if e.filter is not None else None)
    if isinstance(e, WindowCall):
        return WindowCall(e.name, tuple(f(a) for a in e.args), tuple(f(p) for p in e.partition),
                          tuple((f(x), desc, nf) for x, desc, nf in e.order), e.frame, e.type)
    if isinstance(e, SubqueryExpr) and e.arg is not None:
        return SubqueryExpr(e.kind, e.plan, e.type, f(e.arg), e.correlated)
    return e


def walk(e):
    """e and every expression below it (not descending into subquery plans)."""
    stack = [e]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(children(node))


def contains(e, cls):
    return any(isinstance(node, cls) for node in walk(e))


def conjuncts(e):
    """The conditions of an AND chain, left to right."""
    if isinstance(e, Op) and e.op == "AND":
        return conjuncts(e.args[0]) + conjuncts(e.args[1])
    return [e]


def and_all(conditions):
    """Join conditions with AND; None for no conditions."""
    result = None
    for c in conditions:
        result = c if result is None else Op("AND", (result, c), BOOLEAN)
    return result


def shift(e, offset):
    """Move every Col reference by offset (used when rows are concatenated in joins)."""
    if isinstance(e, Col):
        return Col(e.index + offset, e.type, e.name)
    return map_children(e, lambda c: shift(c, offset))


def to_outer(e, depth):
    """Turn Col references into Outer references `depth` levels up."""
    if isinstance(e, Col):
        return Outer(depth, e.index, e.type, e.name)
    return map_children(e, lambda c: to_outer(c, depth))


def column_indexes(e):
    return {node.index for node in walk(e) if isinstance(node, Col)}


# Ordered-set aggregates: f(direct arguments) WITHIN GROUP (ORDER BY the aggregated value).
ORDERED_SET = frozenset({"PERCENTILE_CONT", "PERCENTILE_DISC", "MODE"})

# ---- formatting (EXPLAIN) --------------------------------------------------------

_INFIX = {"AND", "OR", "=", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/", "%", "^", "||", "LIKE", "ILIKE", "~", "~*",
          "!~", "!~*", "->", "->>", "#>", "#>>", "@>", "<@", "&&", "?", "?|", "?&"}
# internal operators and the SQL they came from
_SHOWN_AS = {"DT+": "+", "DT-": "-", "IV*": "*", "IV/": "/", "ARR||": "||", "J-": "-"}


def format_expr(e):
    if isinstance(e, Const):
        if e.value is None:
            return "NULL"
        if isinstance(e.value, bool):
            return "TRUE" if e.value else "FALSE"
        if isinstance(e.value, str):
            return "'" + e.value.replace("'", "''") + "'"
        if e.type in (DATE, TIMESTAMP, INTERVAL):
            return f"{e.type.lower()} '{to_text(e.value)}'"
        return to_text(e.value)
    if isinstance(e, Col):
        return e.name or f"#{e.index}"
    if isinstance(e, Outer):
        return f"outer.{e.name or e.index}"
    if isinstance(e, SubqueryExpr):
        label = {"SCALAR": "(subquery)", "EXISTS": "EXISTS (subquery)", "IN": "IN (subquery)"}[e.kind]
        return f"{format_expr(e.arg)} {label}" if e.arg is not None else label
    if isinstance(e, AggCall) and e.name in ORDERED_SET:
        order = " ".join([format_expr(e.order[0][0])] + ["DESC"] * bool(e.order[0][1]))
        text = f"{e.name.lower()}({', '.join(map(format_expr, e.args[1:]))}) WITHIN GROUP (ORDER BY {order})"
        return text + (f" FILTER (WHERE {format_expr(e.filter)})" if e.filter is not None else "")
    if isinstance(e, AggCall):
        inner = ", ".join(map(format_expr, e.args)) or "*"
        if e.order:
            inner += " ORDER BY " + ", ".join(format_expr(x) + (" DESC" if d else "") for x, d, _ in e.order)
        text = f"{e.name.lower()}({'DISTINCT ' if e.distinct else ''}{inner})"
        return text + (f" FILTER (WHERE {format_expr(e.filter)})" if e.filter is not None else "")
    if isinstance(e, WindowCall):
        parts = []
        if e.partition:
            parts.append("PARTITION BY " + ", ".join(map(format_expr, e.partition)))
        if e.order:
            parts.append("ORDER BY " + ", ".join(format_expr(x) + (" DESC" if d else "") for x, d, _ in e.order))
        return f"{e.name.lower()}({', '.join(map(format_expr, e.args))}) OVER ({' '.join(parts)})"
    args = [format_expr(a) for a in e.args]
    if e.op in ("LIKE", "ILIKE") and len(args) == 3:
        return f"({args[0]} {e.op} {args[1]} ESCAPE {args[2]})"
    if e.op in _INFIX or e.op in _SHOWN_AS:
        return f"({args[0]} {_SHOWN_AS.get(e.op, e.op)} {args[1]})"
    if e.op == "NOT":
        return f"NOT {args[0]}"
    if e.op == "NEG":
        return f"-{args[0]}"
    if e.op in ("ISNULL", "NOTNULL"):
        return f"{args[0]} IS {'NOT ' if e.op == 'NOTNULL' else ''}NULL"
    if e.op in ("DISTINCT", "NOTDISTINCT"):
        right = e.args[1]
        if isinstance(right, Const) and isinstance(right.value, bool):  # spelled IS [NOT] TRUE / FALSE
            return f"{args[0]} IS {'NOT ' if e.op == 'DISTINCT' else ''}{'TRUE' if right.value else 'FALSE'}"
        return f"{args[0]} IS {'NOT ' if e.op == 'NOTDISTINCT' else ''}DISTINCT FROM {args[1]}"
    if e.op == "IN":
        return f"{args[0]} IN ({', '.join(args[1:])})"
    if e.op == "CAST":
        return f"CAST({args[0]} AS {e.type})"
    if e.op == "[]":
        return f"{args[0]}[{args[1]}]"
    if e.op == "ARRAY":
        return f"ARRAY[{', '.join(args)}]"
    if e.op in ("ANY", "ALL"):
        return f"{args[0]} {e.args[2].value} {e.op} ({args[1]})"
    if e.op == "CASE":
        whens = " ".join(f"WHEN {c} THEN {r}" for c, r in zip(args[:-1:2], args[1:-1:2], strict=True))
        return f"CASE {whens} ELSE {args[-1]} END"
    return f"{e.op.lower()}({', '.join(args)})"


# ---- runtime behaviour of operators and functions ---------------------------------


def check_int(v):
    """Keep arithmetic results in range: 64-bit integers and finite floats and decimals."""
    if type(v) is int:
        if not _INT_MIN <= v <= _INT_MAX:
            raise ExecutionError("integer overflow")
    elif (type(v) is float and not math.isfinite(v)) or (type(v) is Decimal and not v.is_finite()):
        raise ExecutionError("numeric overflow")
    return v


def divide(a, b):
    if b == 0:
        raise ExecutionError("division by zero")
    if type(a) is int and type(b) is int:
        q = abs(a) // abs(b)  # integer division truncates toward zero, as in C and SQLite
        return check_int(-q if (a < 0) != (b < 0) else q)
    return check_int(a / b)


def modulo(a, b):
    if b == 0:
        raise ExecutionError("division by zero")
    if type(a) is int and type(b) is int:
        r = abs(a) % abs(b)  # the result takes the sign of the dividend
        return -r if a < 0 else r
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        return Decimal(a) % Decimal(b)  # also takes the sign of the dividend
    return math.fmod(a, b)


def to_text(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, datetime.datetime):
        return dt.format_timestamp(v)
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, tuple):
        return array_text(v, to_text)
    return str(v)


def to_decimal(v):
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError
        return Decimal(repr(v))  # the shortest text that reads back as the float: 0.1, not 0.1000000000000000055...
    if isinstance(v, str):
        result = Decimal(v.strip())
        if not result.is_finite():
            raise ValueError
        return result
    if isinstance(v, bool):
        raise ValueError
    return Decimal(v)


def cast(v, target):
    if v is None:
        return None  # NULL converts to NULL of any type
    try:
        if target == TEXT:
            return to_text(v)
        if is_array(target):
            inner = element(target)
            if isinstance(v, str):
                return parse_array(v, lambda item: cast(item, inner))
            if isinstance(v, tuple):
                return tuple(None if item is None else cast(item, inner) for item in v)
            raise ValueError
        if target == JSONB:
            if isinstance(v, str):
                return Jsonb.parse(v)
            if isinstance(v, Jsonb):
                return v
            raise ValueError
        if isinstance(v, Jsonb):
            return ct.json_scalar(v, target)
        if isinstance(v, tuple):
            raise ValueError
        if target == DECIMAL:
            return to_decimal(v)
        if target == DATE:
            if isinstance(v, str):
                return dt.parse_date(v)
            if isinstance(v, datetime.datetime):
                return v.date()
            if isinstance(v, datetime.date):
                return v
            raise ValueError
        if target == TIMESTAMP:
            if isinstance(v, str):
                return dt.parse_timestamp(v)
            if isinstance(v, datetime.datetime):
                return v
            if isinstance(v, datetime.date):
                return datetime.datetime.combine(v, datetime.time())
            raise ValueError
        if target == INTERVAL:
            if isinstance(v, str):
                return dt.parse_interval(v)
            if isinstance(v, Interval):
                return v
            raise ValueError
        if isinstance(v, (datetime.date, Interval)):
            raise ValueError  # dates and intervals only convert to text
        if target == BOOLEAN:
            if isinstance(v, str):
                word = v.strip().lower()
                if word in ("true", "t", "yes", "1"):
                    return True
                if word in ("false", "f", "no", "0"):
                    return False
                raise ValueError
            return v != 0
        if target == INTEGER and isinstance(v, Decimal):
            return check_int(int(v.to_integral_value(ROUND_HALF_UP)))  # PostgreSQL rounds numeric to integer
        if target == REAL:
            result = float(v.strip() if isinstance(v, str) else v)
            if not math.isfinite(result):
                raise ValueError  # 'inf' and 'nan' are not SQL numbers
            return result
        if target == INTEGER:
            if isinstance(v, str):
                text = v.strip()
                try:
                    return check_int(int(text))
                except ValueError:
                    v = float(text)
            if isinstance(v, float) and not math.isfinite(v):
                raise ValueError
            return check_int(int(v))
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        pass
    shown = to_text(v)
    shown = shown if len(shown) <= 60 else shown[:57] + "..."
    raise ExecutionError(f"cannot convert {shown!r} to {target}")


def scale_decimal(v, scale, precision=None):
    """A NUMERIC rounded to `scale` digits after the point; precision limits the total digits."""
    try:
        v = to_decimal(v).quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
    except (ValueError, InvalidOperation):
        raise ExecutionError(f"cannot convert {to_text(v)!r} to NUMERIC") from None
    if precision is not None and v.adjusted() >= precision - scale:
        raise ExecutionError(f"numeric field overflow: a NUMERIC({precision}, {scale}) value must be below "
                             f"10^{precision - scale} in absolute value")
    return v
