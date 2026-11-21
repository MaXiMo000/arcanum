"""Overflow pages for values too large to keep in a B+tree leaf: PostgreSQL's TOAST.

A row (the chain of its versions) must fit in a quarter of a page. When it does
not, the catalog moves its largest values out, one at a time, until it fits:
each one is compressed with zlib (if that helps) and written across a chain of
overflow pages, and the row keeps a small Toast pointer in its place. Reading
the row follows the pointer back.

The pointer carries a digest of the value, so a version that keeps a value
unchanged (an UPDATE of another column) shares the pointer instead of writing
the value again; pages are freed once no version of the row refers to them.

Overflow page layout (little-endian): u32 next page (0 = last), u16 bytes used, then the bytes.
"""

import hashlib
import struct
import zlib

from ..values import Toast
from .pager import PAGE_SIZE

_HEAD = struct.Struct("<IH")
CHUNK = PAGE_SIZE - _HEAD.size
COMPRESS_GAIN = 0.9  # keep the compressed form only if it saves at least a tenth


def digest(data):
    return hashlib.blake2b(data, digest_size=8).digest()


def write(pager, data):
    """Store data (an encoded value) in new overflow pages; returns the pointer to keep in the row."""
    packed = zlib.compress(data, 6)
    compressed = len(packed) <= len(data) * COMPRESS_GAIN
    body = packed if compressed else data
    pages = [pager.allocate() for _ in range(max(1, -(-len(body) // CHUNK)))]
    for i, page in enumerate(pages):
        chunk = body[i * CHUNK:(i + 1) * CHUNK]
        pager.write(page, _HEAD.pack(pages[i + 1] if i + 1 < len(pages) else 0, len(chunk)) + chunk)
    return Toast(pages[0], len(body), len(data), compressed, digest(data))


def read(pager, toast):
    """The encoded value a pointer stands for."""
    parts, page = [], toast.page
    while page:
        data = pager.read(page)
        page, used = _HEAD.unpack_from(data)
        parts.append(data[_HEAD.size:_HEAD.size + used])
    body = b"".join(parts)
    return zlib.decompress(body) if toast.compressed else body


def pages(pager, toast):
    """The overflow pages of a pointer, without touching the buffer pool (for drawing)."""
    out, page = [], toast.page
    while page:
        out.append(page)
        page = _HEAD.unpack_from(pager.peek(page))[0]
    return out


def free(pager, toast):
    for page in pages(pager, toast):
        pager.free(page)
