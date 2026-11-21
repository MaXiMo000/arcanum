"""The built-in operators and functions at run time, and the registries the binder checks calls against.

FUNCTIONS maps a scalar function's name to its accepted argument signatures, its
result type and its implementation (see the signature letters there). Aggregate,
window, variadic and statement-stable functions are listed by name; their
implementations live with the executor (aggregates) or in containers.py (JSONB
builders). A new scalar function is one entry in FUNCTIONS; its type checking
and NULL handling come for free.
"""

import datetime
import functools
import math
import random
import re
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

from ..errors import ExecutionError
from . import containers as ct
from . import datetimes as dt
from .datetimes import Interval
from .expr import (
    BOOLEAN,
    DATE,
    INTEGER,
    INTERVAL,
    JSONB,
    REAL,
    TEXT,
    TIMESTAMP,
    cast,
    check_int,
    check_size,
    modulo,
    to_text,
)

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
def compile_regex(pattern, flags):
    try:
        return re.compile(pattern, flags)
    except re.error as e:
        raise ExecutionError(f"invalid regular expression {pattern!r}: {e}") from None


def regex_match(value, pattern, insensitive=False):
    return compile_regex(pattern, re.IGNORECASE if insensitive else 0).search(value) is not None


def regexp_replace(value, pattern, replacement, flags=""):
    """PostgreSQL's regexp_replace: the first match only, unless flags contain g; i ignores case.
    \\1 in the replacement refers to a group, as in PostgreSQL."""
    unknown = set(flags) - {"g", "i"}
    if unknown:
        raise ExecutionError(f"regexp_replace flag {sorted(unknown)[0]!r} is not supported (use g and i)")
    compiled = compile_regex(pattern, re.IGNORECASE if "i" in flags else 0)
    matches = len(value) + 1 if "g" in flags else 1  # at most one match per position
    check_size(len(value) + matches * len(replacement), "regexp_replace's result")
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


def _replace(s, old, new):
    if not old:
        return s
    check_size(len(s) + s.count(old) * (len(new) - len(old)), "replace's result")
    return s.replace(old, new)


def power(a, b):
    """a ^ b: exact for NUMERIC, a float otherwise."""
    try:
        if isinstance(a, Decimal) or isinstance(b, Decimal):
            return check_int(Decimal(a) ** Decimal(b))
        return check_int(math.pow(a, b))
    except (ValueError, ZeroDivisionError, OverflowError, InvalidOperation):
        raise ExecutionError(f"cannot raise {to_text(a)} to the power {to_text(b)}") from None


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
        check_size(length, "the padded text")
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
# Signature letters: N numeric, I integer, T text, D date, S date or timestamp, V interval, R an array,
# J jsonb, B boolean, A any. Results that depend on an argument's type are worked out by the binder.
FUNCTIONS = {
    "ARRAY_LENGTH": (("RI",), INTEGER, ct.array_length),
    "ARRAY_UPPER": (("RI",), INTEGER, ct.array_length),
    "ARRAY_LOWER": (("RI",), INTEGER, lambda a, d: 1 if d == 1 and a else None),
    "CARDINALITY": (("R",), INTEGER, len),
    "ARRAY_APPEND": (("RA",), "same", lambda a, x: (*a, x)),
    "ARRAY_PREPEND": (("AR",), "array", lambda x, a: (x, *a)),
    "ARRAY_CAT": (("RR",), "same", lambda a, b: a + b),
    "ARRAY_REMOVE": (("RA",), "same", ct.array_remove),
    "ARRAY_POSITION": (("RA",), INTEGER, ct.array_position),
    "ARRAY_TO_STRING": (("RT", "RTT"), TEXT, ct.array_to_string),
    "STRING_TO_ARRAY": (("TT", "TTT"), "TEXT[]", ct.string_to_array),
    "TO_JSONB": (("A",), JSONB, ct.to_jsonb),
    "TO_JSON": (("A",), JSONB, ct.to_jsonb),
    "JSONB_TYPEOF": (("J",), TEXT, ct.json_typeof),
    "JSON_TYPEOF": (("J",), TEXT, ct.json_typeof),
    "JSONB_ARRAY_LENGTH": (("J",), INTEGER, ct.json_array_length),
    "JSON_ARRAY_LENGTH": (("J",), INTEGER, ct.json_array_length),
    "JSONB_SET": (("JRJ", "JRJB"), JSONB, ct.json_set),
    "JSONB_PRETTY": (("J",), TEXT, ct.json_pretty),
    "JSONB_STRIP_NULLS": (("J",), JSONB, ct.json_strip_nulls),
    "JSONB_EXTRACT_PATH": (("JR",), JSONB, ct.json_path),
    "JSONB_EXTRACT_PATH_TEXT": (("JR",), TEXT, ct.json_path_text),
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
    "REPEAT": (("TI",), TEXT, lambda s, n: check_size(len(s) * max(n, 0), "repeat's result") or s * max(n, 0)),
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
    "REPLACE": (("TTT",), TEXT, _replace),
    "SUBSTR": (("TI", "TII"), TEXT, _substr),
    "SUBSTRING": (("TI", "TII"), TEXT, _substr),
    "ROUND": (("N", "NI"), REAL, _round),
}

AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX", "STRING_AGG", "BOOL_AND", "BOOL_OR", "EVERY", "ARRAY_AGG",
              "JSONB_AGG", "JSON_AGG", "JSONB_OBJECT_AGG", "JSON_OBJECT_AGG"}
# Functions that accept any number of arguments and NULLs among them.
VARIADIC = {"JSONB_BUILD_OBJECT": ct.build_object, "JSON_BUILD_OBJECT": ct.build_object,
            "JSONB_BUILD_ARRAY": ct.build_array, "JSON_BUILD_ARRAY": ct.build_array}
# Functions evaluated once per statement, so every row sees the same moment (PostgreSQL fixes it per transaction).
STABLE = {"NOW", "CURRENT_TIMESTAMP", "LOCALTIMESTAMP", "CURRENT_DATE"}
RANKING = {"ROW_NUMBER", "RANK", "DENSE_RANK"}
WINDOW_ONLY = RANKING | {"LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE"}
