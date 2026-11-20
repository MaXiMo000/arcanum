"""Bound expressions: the binder's output, with every name resolved and every type known.

A bound expression refers to columns by position in the row it is evaluated
against. Nodes are immutable and compare by value (except subqueries), so the
binder can match `a + 1` in SELECT against `a + 1` in GROUP BY with ==.
"""

import datetime
import functools
import math
import random
import re
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

from ..errors import ExecutionError
from . import datetimes as dt
from .datetimes import Interval

INTEGER, REAL, TEXT, BOOLEAN, NULL = "INTEGER", "REAL", "TEXT", "BOOLEAN", "NULL"
DECIMAL, DATE, TIMESTAMP, INTERVAL = "NUMERIC", "DATE", "TIMESTAMP", "INTERVAL"
NUMERIC = (INTEGER, REAL, DECIMAL)
TEMPORAL = (DATE, TIMESTAMP)

_INT_MIN, _INT_MAX = -(2**63), 2**63 - 1


def unify(a, b):
    """The common type of two types, or None if they cannot be mixed."""
    if a == NULL:
        return b
    if b in (NULL, a):
        return a
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
    return TEXT


def assignable(source, target):
    """Whether a value of type `source` can be stored in a column of type `target`. Text converts to dates,
    timestamps and numerics when it is stored (and fails then if it is not one), as PostgreSQL's untyped
    string literals do."""
    if source in (target, NULL):
        return True
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


# ---- formatting (EXPLAIN) --------------------------------------------------------

_INFIX = {"AND", "OR", "=", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/", "%", "||", "LIKE", "ILIKE", "~", "~*",
          "!~", "!~*"}
_SHOWN_AS = {"DT+": "+", "DT-": "-", "IV*": "*", "IV/": "/"}  # internal operators and the SQL they came from


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
    if e.op in _INFIX or e.op in _SHOWN_AS:
        return f"({args[0]} {_SHOWN_AS.get(e.op, e.op)} {args[1]})"
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
    try:
        if target == TEXT:
            return to_text(v)
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
    except (ValueError, InvalidOperation, OverflowError):
        pass
    raise ExecutionError(f"cannot convert {to_text(v)!r} to {target}")


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


# ---- dates, timestamps and intervals ---------------------------------------------------


def temporal_plus(a, b):
    """date + integer days, date or timestamp + interval, interval + interval (either order)."""
    if isinstance(a, Interval) and not isinstance(b, Interval):
        a, b = b, a
    if isinstance(b, Interval):
        return a + b if isinstance(a, Interval) else dt.plus(a, b)
    if isinstance(a, int):
        a, b = b, a
    return dt.date_plus_days(a, b)


def temporal_minus(a, b):
    """date - integer, date - date (days), timestamp - timestamp (interval), date or timestamp - interval."""
    if isinstance(b, Interval):
        return a - b if isinstance(a, Interval) else dt.plus(a, -b)
    if isinstance(b, int):
        return dt.date_plus_days(a, -b)
    if isinstance(a, datetime.datetime) or isinstance(b, datetime.datetime):
        return dt.difference(cast(a, TIMESTAMP), cast(b, TIMESTAMP))
    return (a - b).days


def interval_times(a, b):
    interval, factor = (a, b) if isinstance(a, Interval) else (b, a)
    return interval.scale(factor)


def interval_divide(a, b):
    if b == 0:
        raise ExecutionError("division by zero")
    return a.scale(1 / b if isinstance(b, (int, float)) else Decimal(1) / b)


# ---- pattern matching ------------------------------------------------------------------


@functools.lru_cache(maxsize=256)
def _regex(pattern, flags):
    try:
        return re.compile(pattern, flags)
    except re.error as e:
        raise ExecutionError(f"invalid regular expression {pattern!r}: {e}") from None


def regex_match(value, pattern, insensitive=False):
    return _regex(pattern, re.IGNORECASE if insensitive else 0).search(value) is not None


def regexp_replace(value, pattern, replacement, flags=""):
    """PostgreSQL's regexp_replace: the first match only, unless flags contain g; i ignores case.
    \\1 in the replacement refers to a group, as in PostgreSQL."""
    unknown = set(flags) - {"g", "i"}
    if unknown:
        raise ExecutionError(f"regexp_replace flag {sorted(unknown)[0]!r} is not supported (use g and i)")
    compiled = _regex(pattern, re.IGNORECASE if "i" in flags else 0)
    template = re.sub(r"\\(\d)", r"\\g<\1>", replacement.replace("\\&", "\\g<0>"))
    try:
        return compiled.sub(template, value, count=0 if "g" in flags else 1)
    except (re.error, IndexError) as e:
        raise ExecutionError(f"invalid replacement {replacement!r}: {e}") from None


def like(value, pattern):
    """SQL LIKE: % matches any run of characters, _ matches one. Case-sensitive.

    A two-pointer match that only ever backtracks to the latest %, so it runs in
    O(len(value) * len(pattern)) whatever the pattern; a regex translation could
    take exponential time on patterns like '%a%a%a%a%b'.
    """
    v = p = 0
    star, resume = -1, 0
    while v < len(value):
        if p < len(pattern) and pattern[p] == "%":
            star, resume = p, v
            p += 1
        elif p < len(pattern) and (pattern[p] == "_" or pattern[p] == value[v]):
            v += 1
            p += 1
        elif star != -1:
            resume += 1  # let the latest % swallow one more character
            v, p = resume, star + 1
        else:
            return False
    while p < len(pattern) and pattern[p] == "%":
        p += 1
    return p == len(pattern)


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
    if isinstance(x, Decimal):
        return x.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    scale = 10.0 ** digits
    return math.copysign(math.floor(abs(x) * scale + 0.5) / scale, x)  # half away from zero


def _trunc(x, digits=0):
    if isinstance(x, Decimal):
        return x.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_DOWN)
    scale = 10.0 ** digits
    return math.trunc(x * scale) / scale


def _integral(rounding, function):
    def apply(x):
        if isinstance(x, Decimal):
            return x.to_integral_value(rounding)
        return x if isinstance(x, int) else float(function(x))
    return apply


def _math(function, name):
    def apply(*args):
        try:
            return check_int(function(*(float(a) for a in args)))
        except (ValueError, ZeroDivisionError, OverflowError):
            raise ExecutionError(f"{name}: argument out of range") from None
    return apply


def _pad(left):
    def pad(s, length, fill=" "):
        if length <= len(s) or not fill:
            return s[:max(length, 0)]
        padding = (fill * length)[:length - len(s)]
        return padding + s if left else s + padding
    return pad


def _split_part(s, delimiter, n):
    if n == 0:
        raise ExecutionError("split_part: field position must not be zero")
    parts = s.split(delimiter) if delimiter else [s]
    if n < 0:
        n = len(parts) + n + 1
    return parts[n - 1] if 1 <= n <= len(parts) else ""


def _btrim(s, chars=" "):
    return s.strip(chars)


# name -> (accepted argument signatures, result type or "same", implementation)
# Signature letters: N numeric, I integer, T text, D date, S date or timestamp, V interval, A any.
FUNCTIONS = {
    "CEIL": (("N",), "same", _integral(ROUND_CEILING, math.ceil)),
    "CEILING": (("N",), "same", _integral(ROUND_CEILING, math.ceil)),
    "FLOOR": (("N",), "same", _integral(ROUND_FLOOR, math.floor)),
    "TRUNC": (("N", "NI"), REAL, _trunc),
    "MOD": (("NN",), "same", modulo),
    "POWER": (("NN",), REAL, _math(math.pow, "power")),
    "POW": (("NN",), REAL, _math(math.pow, "power")),
    "SQRT": (("N",), REAL, _math(math.sqrt, "sqrt")),
    "EXP": (("N",), REAL, _math(math.exp, "exp")),
    "LN": (("N",), REAL, _math(math.log, "ln")),
    "LOG": (("N",), REAL, _math(math.log10, "log")),
    "SIGN": (("N",), "same", lambda x: (x > 0) - (x < 0) if isinstance(x, int) else type(x)((x > 0) - (x < 0))),
    "PI": (("",), REAL, lambda: math.pi),
    "RANDOM": (("",), REAL, random.random),
    "LEFT": (("TI",), TEXT, lambda s, n: s[:n]),  # a negative n drops that many characters from the end
    "RIGHT": (("TI",), TEXT, lambda s, n: (s[-n:] if n else "") if n >= 0 else s[-n:]),
    "LPAD": (("TI", "TIT"), TEXT, _pad(True)),
    "RPAD": (("TI", "TIT"), TEXT, _pad(False)),
    "STRPOS": (("TT",), INTEGER, lambda s, sub: s.find(sub) + 1),
    "SPLIT_PART": (("TTI",), TEXT, _split_part),
    "INITCAP": (("T",), TEXT, lambda s: re.sub(r"[A-Za-z0-9]+", lambda m: m[0].capitalize(), s)),
    "REPEAT": (("TI",), TEXT, lambda s, n: s * max(n, 0)),
    "REVERSE": (("T",), TEXT, lambda s: s[::-1]),
    "STARTS_WITH": (("TT",), BOOLEAN, str.startswith),
    "CHR": (("I",), TEXT, chr),
    "ASCII": (("T",), INTEGER, lambda s: ord(s[0]) if s else 0),
    "BTRIM": (("T", "TT"), TEXT, _btrim),
    "TRANSLATE": (("TTT",), TEXT, lambda s, a, b: s.translate({ord(c): (b[i] if i < len(b) else None)
                                                               for i, c in reversed(list(enumerate(a)))})),
    "CHAR_LENGTH": (("T",), INTEGER, len),
    "CHARACTER_LENGTH": (("T",), INTEGER, len),
    "REGEXP_REPLACE": (("TTT", "TTTT"), TEXT, regexp_replace),
    "NOW": (("",), TIMESTAMP, dt.now),
    "CURRENT_TIMESTAMP": (("",), TIMESTAMP, dt.now),
    "LOCALTIMESTAMP": (("",), TIMESTAMP, dt.now),
    "CURRENT_DATE": (("",), DATE, lambda: dt.now().date()),
    "DATE_TRUNC": (("TS",), TIMESTAMP, dt.date_trunc),
    "DATE_PART": (("TS", "TV"), REAL, dt.date_part),
    "AGE": (("SS", "S"), INTERVAL, lambda a, b=None: dt.age(a, b) if b is not None
            else dt.age(datetime.datetime.combine(dt.now().date(), datetime.time()), a)),
    "TO_CHAR": (("ST",), TEXT, dt.to_char),
    "TO_DATE": (("TT",), DATE, lambda s, f: dt.to_timestamp(s, f).date()),
    "TO_TIMESTAMP": (("TT",), TIMESTAMP, dt.to_timestamp),
    "MAKE_DATE": (("III",), DATE, dt.make_date),
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

AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX", "STRING_AGG", "BOOL_AND", "BOOL_OR", "EVERY"}
# Functions evaluated once per statement, so every row sees the same moment (PostgreSQL fixes it per transaction).
STABLE = {"NOW", "CURRENT_TIMESTAMP", "LOCALTIMESTAMP", "CURRENT_DATE"}
RANKING = {"ROW_NUMBER", "RANK", "DENSE_RANK"}
WINDOW_ONLY = RANKING | {"LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE"}
