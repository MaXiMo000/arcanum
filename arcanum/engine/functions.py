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
import hashlib
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
    ORDERED_SET,
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


_ANY, _ONE = object(), object()  # the wildcards % and _, told apart from escaped literal characters


def like_tokens(pattern, escape="\\"):
    """A LIKE pattern as characters and wildcards. The escape character (backslash unless ESCAPE says
    otherwise, none for ESCAPE '') makes the character after it literal."""
    if escape is not None and len(escape) > 1:
        raise ExecutionError("invalid escape string: ESCAPE takes a single character")
    tokens, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if escape and c == escape:
            i += 1
            if i == len(pattern):
                raise ExecutionError("LIKE pattern must not end with the escape character")
            tokens.append(pattern[i])
        else:
            tokens.append(_ANY if c == "%" else _ONE if c == "_" else c)
        i += 1
    return tokens


def like(value, pattern, escape="\\"):
    """SQL LIKE: % matches any run of characters, _ matches one. Case-sensitive.

    A two-pointer match that only ever backtracks to the latest %, so it runs in
    O(len(value) * len(pattern)) whatever the pattern; a regex translation could
    take exponential time on patterns like '%a%a%a%a%b'.
    """
    pattern = like_tokens(pattern, escape)
    v = p = 0
    star, resume = -1, 0
    while v < len(value):
        if p < len(pattern) and pattern[p] is _ANY:
            star, resume = p, v
            p += 1
        elif p < len(pattern) and (pattern[p] is _ONE or pattern[p] == value[v]):
            v += 1
            p += 1
        elif star != -1:
            resume += 1  # let the latest % swallow one more character
            v, p = resume, star + 1
        else:
            return False
    while p < len(pattern) and pattern[p] is _ANY:
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


def quote_ident(name):
    """An identifier as SQL text: bare when it can be, else in double quotes (PostgreSQL's quote_ident)."""
    from ..sql.parser import RESERVED  # noqa: PLC0415 (the parser is not needed until a query runs)

    if re.fullmatch(r"[a-z_][a-z0-9_$]*", name) and name.upper() not in RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def quote_literal(text):
    """A value as an SQL string literal; backslashes make it an E'' string, as in PostgreSQL."""
    quoted = text.replace("'", "''")
    if "\\" not in text:
        return f"'{quoted}'"
    doubled = quoted.replace("\\", "\\\\")
    return f"E'{doubled}'"


_FORMAT_SPEC = re.compile(r"%(?:(\d+)\$)?(-)?(\d+)?([sIL%])")


def format_text(fmt, args):
    """PostgreSQL's format(): %s inserts a value as text (NULL as nothing), %I as a quoted identifier and %L
    as a quoted literal (NULL as NULL); %n$ picks argument n, a width pads (- on the right); %% is a %."""
    out, i, following = [], 0, 0
    while (j := fmt.find("%", i)) != -1:
        out.append(fmt[i:j])
        spec = _FORMAT_SPEC.match(fmt, j)
        if spec is None:
            raise ExecutionError("format: unrecognized format specifier after %; use %s, %I, %L or %%")
        position, left, width, kind = spec.groups()
        i = spec.end()
        if kind == "%":
            out.append("%")
            continue
        index = int(position) - 1 if position else following
        if index < 0:
            raise ExecutionError("format: argument positions start at 1")
        if index >= len(args):
            raise ExecutionError("format: too few arguments")
        following, value = index + 1, args[index]
        if kind == "s":
            text = "" if value is None else to_text(value)
        elif kind == "I":
            if value is None:
                raise ExecutionError("format: a NULL cannot be formatted as an SQL identifier")
            text = quote_ident(to_text(value))
        else:
            text = "NULL" if value is None else quote_literal(to_text(value))
        if width:
            text = text.ljust(int(width)) if left else text.rjust(int(width))
        out.append(text)
    out.append(fmt[i:])
    return "".join(out)


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
    "MD5": (("T",), TEXT, lambda s: hashlib.md5(s.encode(), usedforsecurity=False).hexdigest()),
    "QUOTE_IDENT": (("T",), TEXT, quote_ident),
    "QUOTE_LITERAL": (("A",), TEXT, lambda v: quote_literal(to_text(v))),
    "TRIM": (("T",), TEXT, lambda s: s.strip(" ")),
    "LTRIM": (("T",), TEXT, lambda s: s.lstrip(" ")),
    "RTRIM": (("T",), TEXT, lambda s: s.rstrip(" ")),
    "REPLACE": (("TTT",), TEXT, _replace),
    "SUBSTR": (("TI", "TII"), TEXT, _substr),
    "SUBSTRING": (("TI", "TII"), TEXT, _substr),
    "ROUND": (("N", "NI"), REAL, _round),
}

AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX", "STRING_AGG", "BOOL_AND", "BOOL_OR", "EVERY", "ARRAY_AGG",
              "JSONB_AGG", "JSON_AGG", "JSONB_OBJECT_AGG", "JSON_OBJECT_AGG", *ORDERED_SET}
# Functions that accept any number of arguments and NULLs among them.
VARIADIC = {"JSONB_BUILD_OBJECT": ct.build_object, "JSON_BUILD_OBJECT": ct.build_object,
            "JSONB_BUILD_ARRAY": ct.build_array, "JSON_BUILD_ARRAY": ct.build_array}
# Functions evaluated once per statement, so every row sees the same moment (PostgreSQL fixes it per transaction).
STABLE = {"NOW", "CURRENT_TIMESTAMP", "LOCALTIMESTAMP", "CURRENT_DATE"}
RANKING = {"ROW_NUMBER", "RANK", "DENSE_RANK"}
DISTRIBUTION = {"PERCENT_RANK", "CUME_DIST"}  # a row's relative position in its partition, from 0 or above 0 to 1
WINDOW_ONLY = RANKING | DISTRIBUTION | {"LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE", "NTILE"}
