"""Dates, timestamps and intervals, with PostgreSQL's formats and arithmetic.

DATE values are datetime.date and TIMESTAMP values are naive datetime.datetime,
taken as UTC (there is no session time zone). INTERVAL keeps months, days and
microseconds apart, as PostgreSQL does, because a month has no fixed length:
'2024-01-31' + interval '1 month' is '2024-02-29'.
"""

import calendar
import datetime
import re
from dataclasses import dataclass

from ..errors import ExecutionError

_MICROS_PER_DAY = 86_400_000_000
_UNITS = {  # interval units: (months, days, microseconds) per unit
    "millennium": (12000, 0, 0), "century": (1200, 0, 0), "decade": (120, 0, 0), "year": (12, 0, 0),
    "month": (1, 0, 0), "week": (0, 7, 0), "day": (0, 1, 0), "hour": (0, 0, 3_600_000_000),
    "minute": (0, 0, 60_000_000), "second": (0, 0, 1_000_000), "millisecond": (0, 0, 1000),
    "microsecond": (0, 0, 1),
}
_ALIASES = {
    "y": "year", "yr": "year", "yrs": "year", "years": "year", "mon": "month", "mons": "month", "months": "month",
    "w": "week", "weeks": "week", "d": "day", "days": "day", "h": "hour", "hr": "hour", "hrs": "hour",
    "hours": "hour", "m": "minute", "min": "minute", "mins": "minute", "minutes": "minute", "s": "second",
    "sec": "second", "secs": "second", "seconds": "second", "ms": "millisecond", "msec": "millisecond",
    "milliseconds": "millisecond", "us": "microsecond", "usec": "microsecond", "microseconds": "microsecond",
    "decades": "decade", "centuries": "century", "millennia": "millennium",
}


@dataclass(frozen=True, slots=True)
class Interval:
    months: int = 0
    days: int = 0
    micros: int = 0

    def _key(self):
        # How PostgreSQL compares intervals: a month counts as 30 days.
        return (self.months * 30 + self.days) * _MICROS_PER_DAY + self.micros

    def __lt__(self, other):
        return self._key() < other._key()

    def __le__(self, other):
        return self._key() <= other._key()

    def __gt__(self, other):
        return self._key() > other._key()

    def __ge__(self, other):
        return self._key() >= other._key()

    def __add__(self, other):
        return Interval(self.months + other.months, self.days + other.days, self.micros + other.micros)

    def __sub__(self, other):
        return Interval(self.months - other.months, self.days - other.days, self.micros - other.micros)

    def __neg__(self):
        return Interval(-self.months, -self.days, -self.micros)

    def scale(self, factor):
        """Multiplied by a number; fractions of months and days carry down, as in PostgreSQL."""
        months = self.months * factor
        whole_months = int(months)
        days = self.days * factor + (months - whole_months) * 30
        whole_days = int(days)
        micros = round(self.micros * factor + (days - whole_days) * _MICROS_PER_DAY)
        return Interval(whole_months, whole_days, micros)

    def __str__(self):
        parts = []
        years, months = divmod(abs(self.months), 12)
        sign = "-" if self.months < 0 else ""
        if years:
            parts.append(f"{sign}{years} year{'s' if years != 1 else ''}")
        if months:
            parts.append(f"{sign}{months} mon{'s' if months != 1 else ''}")
        if self.days:
            parts.append(f"{self.days} day{'s' if abs(self.days) != 1 else ''}")
        if self.micros or not parts:
            micros = abs(self.micros)
            hours, rest = divmod(micros, 3_600_000_000)
            minutes, rest = divmod(rest, 60_000_000)
            seconds, fraction = divmod(rest, 1_000_000)
            clock = f"{'-' if self.micros < 0 else ''}{hours:02d}:{minutes:02d}:{seconds:02d}"
            parts.append(clock + (f".{fraction:06d}".rstrip("0") if fraction else ""))
        return " ".join(parts)


def _bad(text, kind):
    return ExecutionError(f"invalid input syntax for type {kind}: {text!r}")


def now():
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def parse_date(text):
    t = text.strip().lower()
    special = {"today": 0, "yesterday": -1, "tomorrow": 1}
    if t in special or t == "now":
        return now().date() + datetime.timedelta(days=special.get(t, 0))
    try:
        return datetime.date.fromisoformat(t[:10]) if len(t) >= 10 else datetime.date.fromisoformat(t)
    except ValueError:
        raise _bad(text, "date") from None


def parse_timestamp(text):
    t = text.strip()
    lowered = t.lower()
    if lowered == "now":
        return now()
    if lowered in ("today", "yesterday", "tomorrow"):
        return datetime.datetime.combine(parse_date(lowered), datetime.time())
    t = re.sub(r"(?i)\s*(z|utc|[+-]00(:?00)?)$", "", t)  # UTC offsets are accepted; others are not
    try:
        value = datetime.datetime.fromisoformat(t.replace(" ", "T", 1) if " " in t else t)
    except ValueError:
        raise _bad(text, "timestamp") from None
    if value.tzinfo is not None:
        raise _bad(text, "timestamp")
    return value


_PART = re.compile(r"(-?\d+(?:\.\d+)?)\s*([a-z]+)")
_CLOCK = re.compile(r"(-)?(\d+):(\d{1,2})(?::(\d{1,2}(?:\.\d+)?))?")


def parse_interval(text):
    t = text.strip().lower().replace(",", " ")
    if t.startswith("@"):
        t = t[1:]
    ago = t.endswith(" ago")
    if ago:
        t = t[:-4]
    months = days = micros = 0
    rest = t
    clock = _CLOCK.search(rest)
    if clock:
        negative, hours, minutes, seconds = clock.groups()
        value = (int(hours) * 3600 + int(minutes) * 60) * 1_000_000 + round(float(seconds or 0) * 1_000_000)
        micros += -value if negative else value
        rest = rest[:clock.start()] + rest[clock.end():]
    for amount, unit in _PART.findall(rest):
        unit = _ALIASES.get(unit, unit.rstrip("s") if unit.rstrip("s") in _UNITS else unit)
        if unit not in _UNITS:
            raise _bad(text, "interval")
        m, d, u = _UNITS[unit]
        n = float(amount)
        scaled = Interval(m, d, u).scale(n) if n != int(n) else Interval(m * int(n), d * int(n), u * int(n))
        months, days, micros = months + scaled.months, days + scaled.days, micros + scaled.micros
    leftover = _PART.sub("", rest).strip()
    if leftover or (not clock and not _PART.search(rest)):
        raise _bad(text, "interval")
    result = Interval(months, days, micros)
    return -result if ago else result


def add_months(value, months):
    if not months:
        return value
    total = value.year * 12 + value.month - 1 + months
    year, month = divmod(total, 12)
    if not 1 <= year <= 9999:
        raise ExecutionError("date out of range")
    day = min(value.day, calendar.monthrange(year, month + 1)[1])
    return value.replace(year=year, month=month + 1, day=day)


def plus(value, interval):
    """A date or timestamp plus an interval: a timestamp."""
    if not isinstance(value, datetime.datetime):
        value = datetime.datetime.combine(value, datetime.time())
    try:
        value = add_months(value, interval.months)
        return value + datetime.timedelta(days=interval.days, microseconds=interval.micros)
    except OverflowError:
        raise ExecutionError("timestamp out of range") from None


def date_plus_days(value, days):
    try:
        return value + datetime.timedelta(days=days)
    except OverflowError:
        raise ExecutionError("date out of range") from None


def difference(a, b):
    """Timestamp minus timestamp: an interval in days and time, as PostgreSQL gives it."""
    delta = a - b
    micros = delta.seconds * 1_000_000 + delta.microseconds
    days = delta.days
    if days < 0 and micros:  # keep both parts the same sign: -1 day +23:00 becomes -01:00
        days, micros = days + 1, micros - _MICROS_PER_DAY
    return Interval(0, days, micros)


def age(a, b):
    """Symbolic difference in years, months and days (PostgreSQL's age())."""
    a = a if isinstance(a, datetime.datetime) else datetime.datetime.combine(a, datetime.time())
    b = b if isinstance(b, datetime.datetime) else datetime.datetime.combine(b, datetime.time())
    sign = 1
    if a < b:
        a, b, sign = b, a, -1
    months = (a.year - b.year) * 12 + a.month - b.month
    if add_months(b, months) > a:
        months -= 1
    rest = difference(a, add_months(b, months))
    return Interval(sign * months, sign * rest.days, sign * rest.micros)


_TRUNC = ("microseconds", "milliseconds", "second", "minute", "hour", "day", "week", "month", "quarter", "year",
          "decade", "century", "millennium")


def date_trunc(field, value):
    field = field.lower()
    if not isinstance(value, datetime.datetime):
        value = datetime.datetime.combine(value, datetime.time())
    if field in ("microseconds", "microsecond"):
        return value
    if field in ("milliseconds", "millisecond"):
        return value.replace(microsecond=value.microsecond // 1000 * 1000)
    if field == "second":
        return value.replace(microsecond=0)
    if field == "minute":
        return value.replace(second=0, microsecond=0)
    if field == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    day = value.replace(hour=0, minute=0, second=0, microsecond=0)
    if field == "day":
        return day
    if field == "week":
        return day - datetime.timedelta(days=day.weekday())
    if field == "month":
        return day.replace(day=1)
    if field == "quarter":
        return day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1)
    span = {"year": 1, "decade": 10, "century": 100, "millennium": 1000}.get(field)
    if span is None:
        raise ExecutionError(f"date_trunc unit {field!r} is not recognized; use one of {', '.join(_TRUNC)}")
    year = day.year if span == 1 else ((day.year - 1) // span * span + 1 if span >= 100 else day.year // span * span)
    return day.replace(year=max(year, 1), month=1, day=1)


INTEGRAL_FIELDS = {"year", "month", "day", "hour", "minute", "dow", "isodow", "doy", "week", "quarter", "decade",
                   "century", "millennium", "isoyear"}
FIELDS = INTEGRAL_FIELDS | {"second", "epoch", "milliseconds", "microseconds"}


def date_part(field, value):
    field = field.lower()
    if field not in FIELDS:
        raise ExecutionError(f"unit {field!r} is not recognized; use one of {', '.join(sorted(FIELDS))}")
    if isinstance(value, Interval):
        return _interval_part(field, value)
    stamp = value if isinstance(value, datetime.datetime) else datetime.datetime.combine(value, datetime.time())
    if field == "epoch":
        return (stamp - datetime.datetime(1970, 1, 1)).total_seconds()
    if field == "second":
        return stamp.second + stamp.microsecond / 1_000_000
    if field == "milliseconds":
        return stamp.second * 1000 + stamp.microsecond / 1000
    if field == "microseconds":
        return stamp.second * 1_000_000 + stamp.microsecond
    iso = stamp.isocalendar()
    return {
        "year": stamp.year, "month": stamp.month, "day": stamp.day, "hour": stamp.hour, "minute": stamp.minute,
        "dow": (stamp.weekday() + 1) % 7, "isodow": iso[2], "doy": stamp.timetuple().tm_yday, "week": iso[1],
        "isoyear": iso[0], "quarter": (stamp.month - 1) // 3 + 1, "decade": stamp.year // 10,
        "century": (stamp.year - 1) // 100 + 1, "millennium": (stamp.year - 1) // 1000 + 1,
    }[field]


def _interval_part(field, v):
    if field == "epoch":
        return (v.months * 30 + v.days) * 86400 + v.micros / 1_000_000
    seconds = v.micros / 1_000_000
    parts = {"year": int(v.months / 12), "month": v.months % 12 if v.months >= 0 else -(-v.months % 12),
             "day": v.days, "hour": int(seconds // 3600), "minute": int(seconds % 3600 // 60),
             "second": seconds % 60, "milliseconds": seconds % 60 * 1000, "microseconds": round(seconds % 60 * 1e6),
             "decade": int(v.months / 120), "century": int(v.months / 1200), "millennium": int(v.months / 12000),
             "quarter": v.months % 12 // 3 + 1}
    if field not in parts:
        raise ExecutionError(f"unit {field!r} is not supported for interval values")
    return parts[field]


# to_char / to_date / to_timestamp patterns, longest first so HH24 wins over HH.
_PATTERNS = [
    ("YYYY", "%Y", lambda d: f"{d.year:04d}"), ("YY", "%y", lambda d: f"{d.year % 100:02d}"),
    ("MONTH", None, lambda d: d.strftime("%B").upper().ljust(9)), ("Month", "%B", lambda d: d.strftime("%B").ljust(9)),
    ("month", None, lambda d: d.strftime("%B").lower().ljust(9)), ("MON", None, lambda d: d.strftime("%b").upper()),
    ("Mon", "%b", lambda d: d.strftime("%b")), ("mon", None, lambda d: d.strftime("%b").lower()),
    ("MM", "%m", lambda d: f"{d.month:02d}"), ("DDD", "%j", lambda d: f"{d.timetuple().tm_yday:03d}"),
    ("DD", "%d", lambda d: f"{d.day:02d}"), ("DAY", None, lambda d: d.strftime("%A").upper().ljust(9)),
    ("Day", "%A", lambda d: d.strftime("%A").ljust(9)), ("day", None, lambda d: d.strftime("%A").lower().ljust(9)),
    ("DY", None, lambda d: d.strftime("%a").upper()), ("Dy", "%a", lambda d: d.strftime("%a")),
    ("dy", None, lambda d: d.strftime("%a").lower()), ("HH24", "%H", lambda d: f"{_t(d).hour:02d}"),
    ("HH12", "%I", lambda d: f"{(_t(d).hour % 12) or 12:02d}"), ("HH", "%I", lambda d: f"{(_t(d).hour % 12) or 12:02d}"),
    ("MI", "%M", lambda d: f"{_t(d).minute:02d}"), ("SS", "%S", lambda d: f"{_t(d).second:02d}"),
    ("MS", None, lambda d: f"{_t(d).microsecond // 1000:03d}"), ("US", "%f", lambda d: f"{_t(d).microsecond:06d}"),
    ("AM", "%p", lambda d: "AM" if _t(d).hour < 12 else "PM"), ("PM", "%p", lambda d: "AM" if _t(d).hour < 12 else "PM"),
    ("am", None, lambda d: "am" if _t(d).hour < 12 else "pm"), ("pm", None, lambda d: "am" if _t(d).hour < 12 else "pm"),
    ("Q", None, lambda d: str((d.month - 1) // 3 + 1)), ("D", None, lambda d: str((d.weekday() + 1) % 7 + 1)),
]


def _t(d):
    return d if isinstance(d, datetime.datetime) else datetime.datetime.combine(d, datetime.time())


def _tokens(pattern):
    """Split a format pattern into (pattern entry or None, literal text) pieces; "quoted" text is literal."""
    i, out = 0, []
    while i < len(pattern):
        if pattern[i] == '"':
            end = pattern.find('"', i + 1)
            end = len(pattern) if end < 0 else end
            out.append((None, pattern[i + 1:end]))
            i = end + 1
            continue
        if pattern.startswith("FM", i):  # fill mode: accepted, padding is already minimal for numbers
            i += 2
            continue
        entry = next((e for e in _PATTERNS if pattern.startswith(e[0], i)), None)
        if entry is None:
            out.append((None, pattern[i]))
            i += 1
        else:
            out.append((entry, entry[0]))
            i += len(entry[0])
    return out


def to_char(value, pattern):
    fill = "FM" in pattern
    pieces = []
    for entry, text in _tokens(pattern):
        if entry is None:
            pieces.append(text)
        else:
            piece = entry[2](value)
            pieces.append(piece.rstrip() if fill and entry[0].lower() in ("month", "day") else piece)
    return "".join(pieces)


def to_timestamp(text, pattern):
    fmt = []
    for entry, piece in _tokens(pattern):
        if entry is None:
            fmt.append(piece.replace("%", "%%"))
        elif entry[1] is None:
            raise ExecutionError(f"to_date / to_timestamp do not support the pattern {entry[0]}")
        else:
            fmt.append(entry[1])
    try:
        return datetime.datetime.strptime(text.strip(), "".join(fmt))
    except ValueError:
        raise ExecutionError(f"{text!r} does not match the format {pattern!r}") from None


def make_date(year, month, day):
    try:
        return datetime.date(year, month, day)
    except ValueError:
        raise ExecutionError(f"date field value out of range: {year}-{month:02d}-{day:02d}") from None


def format_timestamp(v):
    text = v.strftime("%Y-%m-%d %H:%M:%S")
    return text + (f".{v.microsecond:06d}".rstrip("0") if v.microsecond else "")
