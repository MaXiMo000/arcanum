"""Order-preserving encoding of value tuples.

encode() turns a tuple of SQL values into bytes whose byte order matches the
tuple order, so the B+tree compares keys as plain bytes and never needs to
know about types. The same format stores whole rows.

Each value is a tag byte followed by its payload:
    NULL     0x01                      sorts before every other value
    INTEGER  0x02 + 8 bytes            big-endian with the sign bit flipped
    REAL     0x03 + 8 bytes            IEEE 754 bits, transformed so byte order = numeric order
    TEXT     0x04 + UTF-8 + 0x00 0x00  a 0x00 inside the text is written as 0x00 0xFF

A shorter tuple sorts before any longer tuple it is a prefix of. A column holds
one type, so INTEGER and REAL values never meet at the same position.
"""

import struct

NULL, INTEGER, REAL, TEXT = 0x01, 0x02, 0x03, 0x04

_U64 = struct.Struct(">Q")
_F64 = struct.Struct(">d")
_SIGN = 1 << 63
_MASK = (1 << 64) - 1


def encode(values) -> bytes:
    out = bytearray()
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
            out += v.encode().replace(b"\x00", b"\x00\xff")
            out += b"\x00\x00"
        else:
            raise TypeError(f"cannot store a value of type {type(v).__name__}")
    return bytes(out)


def decode(data: bytes) -> tuple:
    values, i = [], 0
    while i < len(data):
        tag = data[i]
        i += 1
        if tag == NULL:
            values.append(None)
        elif tag == INTEGER:
            values.append(_U64.unpack_from(data, i)[0] - _SIGN)
            i += 8
        elif tag == REAL:
            bits = _U64.unpack_from(data, i)[0]
            i += 8
            values.append(_F64.unpack(_U64.pack(bits ^ _SIGN if bits & _SIGN else ~bits & _MASK))[0])
        elif tag == TEXT:
            text = bytearray()
            while True:
                end = data.index(b"\x00", i)
                text += data[i:end]
                i = end + 2
                if data[end + 1] != 0xFF:
                    break
                text.append(0)
            values.append(text.decode())
        else:
            raise ValueError(f"corrupt value: unknown tag {tag:#04x} at byte {i - 1}")
    return tuple(values)
