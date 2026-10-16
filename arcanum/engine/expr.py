"""Bound expressions: the binder's output, with every name resolved and every type known.

A bound expression refers to columns by position in the row it is evaluated
against. Nodes are immutable and compare by value (except subqueries), so the
binder can match `a + 1` in SELECT against `a + 1` in GROUP BY with ==.
"""

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache

from ..errors import ExecutionError

INTEGER, REAL, TEXT, BOOLEAN, NULL = "INTEGER", "REAL", "TEXT", "BOOLEAN", "NULL"
NUMERIC = (INTEGER, REAL)

_INT_MIN, _INT_MAX = -(2**63), 2**63 - 1


def unify(a, b):
    """The common type of two types, or None if they cannot be mixed."""
    if a == NULL:
        return b
    if b == NULL or a == b:
        return a
    if a in NUMERIC and b in NUMERIC:
        return REAL
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
    return TEXT


def assignable(source, target):
    """Whether a value of type `source` can be stored in a column of type `target`."""
    return source in (target, NULL) or (source == INTEGER and target == REAL)


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
    if isinstance(e, (Op, AggCall)):
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
        return AggCall(e.name, tuple(f(a) for a in e.args), e.distinct, e.type)
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


# ---- formatting (EXPLAIN) --------------------------------------------------------

_INFIX = {"AND", "OR", "=", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/", "%", "||", "LIKE"}


def format_expr(e):
    if isinstance(e, Const):
        if e.value is None:
            return "NULL"
        if isinstance(e.value, bool):
            return "TRUE" if e.value else "FALSE"
        if isinstance(e.value, str):
            return "'" + e.value.replace("'", "''") + "'"
        return str(e.value)
    if isinstance(e, Col):
        return e.name or f"#{e.index}"
    if isinstance(e, Outer):
        return f"outer.{e.name or e.index}"
    if isinstance(e, SubqueryExpr):
        label = {"SCALAR": "(subquery)", "EXISTS": "EXISTS (subquery)", "IN": "IN (subquery)"}[e.kind]
        return f"{format_expr(e.arg)} {label}" if e.arg is not None else label
    if isinstance(e, AggCall):
        inner = ", ".join(map(format_expr, e.args)) or "*"
        return f"{e.name.lower()}({'DISTINCT ' if e.distinct else ''}{inner})"
    if isinstance(e, WindowCall):
        parts = []
        if e.partition:
            parts.append("PARTITION BY " + ", ".join(map(format_expr, e.partition)))
        if e.order:
            parts.append("ORDER BY " + ", ".join(format_expr(x) + (" DESC" if d else "") for x, d, _ in e.order))
        return f"{e.name.lower()}({', '.join(map(format_expr, e.args))}) OVER ({' '.join(parts)})"
    args = [format_expr(a) for a in e.args]
    if e.op in _INFIX:
        return f"({args[0]} {e.op} {args[1]})"
    if e.op == "NOT":
        return f"NOT {args[0]}"
    if e.op == "NEG":
        return f"-{args[0]}"
    if e.op in ("ISNULL", "NOTNULL"):
        return f"{args[0]} IS {'NOT ' if e.op == 'NOTNULL' else ''}NULL"
    if e.op == "IN":
        return f"{args[0]} IN ({', '.join(args[1:])})"
    if e.op == "CAST":
        return f"CAST({args[0]} AS {e.type})"
    if e.op == "CASE":
        whens = " ".join(f"WHEN {c} THEN {r}" for c, r in zip(args[:-1:2], args[1:-1:2]))
        return f"CASE {whens} ELSE {args[-1]} END"
    return f"{e.op.lower()}({', '.join(args)})"


# ---- runtime behaviour of operators and functions ---------------------------------


def check_int(v):
    if type(v) is int and not _INT_MIN <= v <= _INT_MAX:
        raise ExecutionError("integer overflow")
    return v


def divide(a, b):
    if b == 0:
        raise ExecutionError("division by zero")
    if type(a) is int and type(b) is int:
        q = abs(a) // abs(b)  # integer division truncates toward zero, as in C and SQLite
        return check_int(-q if (a < 0) != (b < 0) else q)
    return a / b


def modulo(a, b):
    if b == 0:
        raise ExecutionError("division by zero")
    if type(a) is int and type(b) is int:
        r = abs(a) % abs(b)  # the result takes the sign of the dividend
        return -r if a < 0 else r
    return math.fmod(a, b)


def to_text(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return v if isinstance(v, str) else str(v)


def cast(v, target):
    try:
        if target == TEXT:
            return to_text(v)
        if target == BOOLEAN:
            if isinstance(v, str):
                word = v.strip().lower()
                if word in ("true", "t", "yes", "1"):
                    return True
                if word in ("false", "f", "no", "0"):
                    return False
                raise ValueError
            return v != 0
        if target == REAL:
            return float(v.strip() if isinstance(v, str) else v)
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
    except ValueError:
        pass
    raise ExecutionError(f"cannot convert {to_text(v)!r} to {target}")


@lru_cache(maxsize=256)
def _like_pattern(pattern):
    regex = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
    return re.compile(regex, re.DOTALL)


def like(value, pattern):
    """SQL LIKE: % matches any run of characters, _ matches one. Case-sensitive."""
    return _like_pattern(pattern).fullmatch(value) is not None


def _substr(s, start, length=None):
    # 1-based like SQL; a negative start counts from the end; start 0 behaves like SQLite.
    if start > 0:
        begin = start - 1
    elif start < 0:
        begin = max(len(s) + start, 0)
    else:
        begin = 0
        if length is not None:
            length -= 1
    if length is None:
        return s[begin:]
    return s[begin:begin + max(length, 0)]


def _round(x, digits=0):
    scale = 10.0 ** digits
    return math.copysign(math.floor(abs(x) * scale + 0.5) / scale, x)  # half away from zero


# name -> (accepted argument signatures, result type or "same", implementation)
# Signature letters: N numeric, I integer, T text, A any.
FUNCTIONS = {
    "ABS": (("N",), "same", abs),
    "LENGTH": (("T",), INTEGER, len),
    "LOWER": (("T",), TEXT, str.lower),
    "UPPER": (("T",), TEXT, str.upper),
    "TRIM": (("T",), TEXT, lambda s: s.strip(" ")),
    "LTRIM": (("T",), TEXT, lambda s: s.lstrip(" ")),
    "RTRIM": (("T",), TEXT, lambda s: s.rstrip(" ")),
    "REPLACE": (("TTT",), TEXT, lambda s, old, new: s.replace(old, new) if old else s),
    "SUBSTR": (("TI", "TII"), TEXT, _substr),
    "SUBSTRING": (("TI", "TII"), TEXT, _substr),
    "ROUND": (("N", "NI"), REAL, _round),
}

AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}
RANKING = {"ROW_NUMBER", "RANK", "DENSE_RANK"}
WINDOW_ONLY = RANKING | {"LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE"}
