"""SQL values that are not plain Python scalars: JSONB documents, and the text form of arrays.

A JSONB value keeps the parsed document and its canonical text: keys ordered
the way PostgreSQL's jsonb orders them (shorter keys first, then by bytes),
duplicate keys resolved to the last one, numbers kept exact. Two documents are
equal exactly when their canonical texts are, which is also the order they sort
and index in.

Arrays are Python tuples at run time; array_text() and parse_array() convert
them to and from PostgreSQL's '{1,2,NULL}' text form.
"""

import json
from decimal import Decimal, InvalidOperation

from .errors import ExecutionError


class Jsonb:
    __slots__ = ("text", "value")

    def __init__(self, value):
        self.value = value
        self.text = dumps(value)

    @classmethod
    def parse(cls, text):
        try:
            return cls(json.loads(text, parse_float=Decimal, parse_constant=_no_constant))
        except (ValueError, InvalidOperation):
            raise ExecutionError(f"invalid input syntax for type jsonb: {text[:60]!r}") from None

    def __eq__(self, other):
        return isinstance(other, Jsonb) and self.text == other.text

    def __hash__(self):
        return hash(("jsonb", self.text))

    def __lt__(self, other):
        return self.text < other.text

    def __le__(self, other):
        return self.text <= other.text

    def __gt__(self, other):
        return self.text > other.text

    def __ge__(self, other):
        return self.text >= other.text

    def __str__(self):
        return self.text

    def __repr__(self):
        return f"Jsonb({self.text})"


def _no_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def dumps(value):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(dumps(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: (len(k.encode()), k.encode()))
        return "{" + ", ".join(f"{json.dumps(k, ensure_ascii=False)}: {dumps(value[k])}" for k in keys) + "}"
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def _needs_quotes(text):
    return text == "" or text.upper() == "NULL" or any(c in text for c in '{},"\\ \t\n')


def array_text(values, to_text):
    """PostgreSQL's text form of an array: {1,2,NULL,"two words"}."""
    parts = []
    for v in values:
        if v is None:
            parts.append("NULL")
            continue
        text = to_text(v)
        if _needs_quotes(text):
            text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        parts.append(text)
    return "{" + ",".join(parts) + "}"


def parse_array(text, convert):
    """The elements of a '{...}' array literal, each passed through convert (NULL stays None)."""
    t = text.strip()
    if len(t) < 2 or t[0] != "{" or t[-1] != "}":
        raise ExecutionError(f"malformed array literal: {text[:60]!r}: it must start with {{ and end with }}")
    body, out, i = t[1:-1], [], 0
    if not body.strip():
        return ()
    while i <= len(body):
        while i < len(body) and body[i].isspace():
            i += 1
        if i < len(body) and body[i] == '"':
            i += 1
            chars = []
            while i < len(body) and body[i] != '"':
                if body[i] == "\\" and i + 1 < len(body):
                    i += 1
                chars.append(body[i])
                i += 1
            if i >= len(body):
                raise ExecutionError(f"malformed array literal: {text[:60]!r}: unterminated quote")
            item, quoted = "".join(chars), True
            i += 1
        else:
            end = body.find(",", i)
            end = len(body) if end < 0 else end
            item, quoted = body[i:end].strip(), False
            if "{" in item or "}" in item:
                raise ExecutionError("Arcanum supports one-dimensional arrays only")
            i = end
        while i < len(body) and body[i].isspace():
            i += 1
        if i < len(body) and body[i] != ",":
            raise ExecutionError(f"malformed array literal: {text[:60]!r}")
        out.append(None if not quoted and item.upper() == "NULL" else convert(item))
        i += 1
    return tuple(out)
