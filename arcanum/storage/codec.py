"""Order-preserving encoding of value tuples.

encode() turns a tuple of SQL values into bytes whose byte order matches the
tuple order, so the B+tree compares keys as plain bytes and never needs to
know about types. The same format stores whole rows.

Each value is a tag byte followed by its payload:
    NULL     0x01                      sorts before every other value
    INTEGER  0x02 + 8 bytes            big-endian with the sign bit flipped
    REAL     0x03 + 8 bytes            IEEE 754 bits, transformed so byte order = numeric order
    TEXT     0x04 + UTF-8 + 0x00 0x00  a 0x00 inside the text is written as 0x00 0xFF
    DATE     0x05 + 8 bytes            days since 1970-01-01, like INTEGER
    TIMESTAMP 0x06 + 8 bytes           microseconds since 1970-01-01 00:00 (UTC), like INTEGER
    NUMERIC  0x07 + class + ...        see _encode_decimal: exponent then digits, inverted when negative
    ARRAY    0x08 + elements + 0x00    each element in this format; 0x00 sorts before any element, so a
                                       prefix sorts first ({1,2} < {1,2,3}), as PostgreSQL orders arrays
    JSONB    0x09 + text as TEXT       the canonical text (see values.py)
    TOAST    0x0A + 25 bytes           a pointer to a value in overflow pages (stored rows only, never keys)

A shorter tuple sorts before any longer tuple it is a prefix of. A column holds
one type, so INTEGER and REAL values never meet at the same position.
"""

import datetime
import struct
from decimal import Decimal

from ..values import Jsonb, Toast

NULL, INTEGER, REAL, TEXT, DATE, TIMESTAMP, NUMERIC, ARRAY, JSONB, TOAST = (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
                                                                         0x08, 0x09, 0x0A)
_TOAST = struct.Struct(">III?8s")
EPOCH = datetime.datetime(1970, 1, 1)
_EPOCH_DAY = EPOCH.date().toordinal()
_MICRO = datetime.timedelta(microseconds=1)

_U64 = struct.Struct(">Q")
_F64 = struct.Struct(">d")
_SIGN = 1 << 63
_MASK = (1 << 64) - 1


def encode(values) -> bytes:
    out = bytearray()
    _encode_into(out, values)
    return bytes(out)


def _encode_into(out, values):
    for v in values:
        if v is None:
            out.append(NULL)
        elif isinstance(v, int):  # bool is an int, stored as 0 or 1
            if not -_SIGN <= v < _SIGN:
                raise OverflowError(f"integer out of 64-bit range: {v}")
            out.append(INTEGER)
            out += _U64.pack(v + _SIGN)
        elif isinstance(v, float):
            bits = _U64.unpack(_F64.pack(v + 0.0))[0]  # + 0.0 turns -0.0 into 0.0
            out.append(REAL)
            out += _U64.pack((~bits & _MASK) if bits & _SIGN else bits | _SIGN)
        elif isinstance(v, str):
            out.append(TEXT)
            _text_into(out, v)
        elif isinstance(v, tuple):
            out.append(ARRAY)
            _encode_into(out, v)
            out.append(0)
        elif isinstance(v, Jsonb):
            out.append(JSONB)
            _text_into(out, v.text)
        elif isinstance(v, Toast):
            out.append(TOAST)
            out += _TOAST.pack(v.page, v.size, v.raw, v.compressed, v.digest)
        elif isinstance(v, datetime.datetime):  # before date: a datetime is also a date
            out.append(TIMESTAMP)
            out += _U64.pack((v - EPOCH) // _MICRO + _SIGN)
        elif isinstance(v, datetime.date):
            out.append(DATE)
            out += _U64.pack(v.toordinal() - _EPOCH_DAY + _SIGN)
        elif isinstance(v, Decimal):
            out.append(NUMERIC)
            out += _encode_decimal(v)
        else:
            raise TypeError(f"cannot store a value of type {type(v).__name__}")


def _text_into(out, text):
    out += text.encode().replace(b"\x00", b"\x00\xff")
    out += b"\x00\x00"


def decode(data: bytes) -> tuple:
    values, i = [], 0
    while i < len(data):
        value, i = _decode_one(data, i)
        values.append(value)
    return tuple(values)


def _decode_text(data, i):
    text = bytearray()
    while True:
        end = data.index(b"\x00", i)
        text += data[i:end]
        i = end + 2
        if data[end + 1] != 0xFF:
            return text.decode(), i
        text.append(0)


def _decode_one(data, i):
    tag = data[i]
    i += 1
    if tag == NULL:
        return None, i
    if tag == INTEGER:
        return _U64.unpack_from(data, i)[0] - _SIGN, i + 8
    if tag == REAL:
        bits = _U64.unpack_from(data, i)[0]
        return _F64.unpack(_U64.pack(bits ^ _SIGN if bits & _SIGN else ~bits & _MASK))[0], i + 8
    if tag == TEXT:
        return _decode_text(data, i)
    if tag == TIMESTAMP:
        return EPOCH + (_U64.unpack_from(data, i)[0] - _SIGN) * _MICRO, i + 8
    if tag == DATE:
        return datetime.date.fromordinal(_U64.unpack_from(data, i)[0] - _SIGN + _EPOCH_DAY), i + 8
    if tag == NUMERIC:
        return _decode_decimal(data, i)
    if tag == ARRAY:
        items = []
        while data[i] != 0:
            value, i = _decode_one(data, i)
            items.append(value)
        return tuple(items), i + 1
    if tag == JSONB:
        text, i = _decode_text(data, i)
        return Jsonb.parse(text), i
    if tag == TOAST:
        return Toast(*_TOAST.unpack_from(data, i)), i + _TOAST.size
    raise ValueError(f"corrupt value: unknown tag {tag:#04x} at byte {i - 1}")


# NUMERIC: a non-zero value is 0.d1d2d3... x 10^exponent. Byte order must match numeric order and no
# encoding may be a prefix of another, so: a class byte (negative < zero < positive), then for positives
# the exponent (biased, big-endian) and the digits as ASCII ending in 0x00, which sorts before any digit
# (0.12 < 0.123). Negatives store every byte inverted and end in 0xFF, so larger magnitudes sort first.
_NEGATIVE, _ZERO, _POSITIVE = 0x01, 0x02, 0x03
_EXP = struct.Struct(">I")
_EXP_BIAS = 1 << 31


def _encode_decimal(v):
    if not v.is_finite():
        raise ValueError("NUMERIC values must be finite")
    if not v:
        return bytes([_ZERO])
    sign, digits, exponent = v.normalize().as_tuple()
    body = _EXP.pack(len(digits) + exponent + _EXP_BIAS) + bytes(48 + d for d in digits)
    if sign:
        return bytes([_NEGATIVE]) + bytes(255 - b for b in body) + b"\xff"
    return bytes([_POSITIVE]) + body + b"\x00"


def _decode_decimal(data, i):
    kind = data[i]
    i += 1
    if kind == _ZERO:
        return Decimal(0), i
    negative = kind == _NEGATIVE
    # The exponent's 4 bytes may contain the terminator byte, so the search starts after them.
    end = data.index(b"\xff" if negative else b"\x00", i + 4)
    body = bytes(255 - b for b in data[i:end]) if negative else data[i:end]
    point = _EXP.unpack_from(body)[0] - _EXP_BIAS
    digits = tuple(b - 48 for b in body[4:])
    return Decimal((int(negative), digits, point - len(digits))), end + 1
