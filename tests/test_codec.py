import random

import pytest

from arcanum.storage.codec import decode, encode

SAMPLES = [
    (), (None,), (0,), (1,), (-1,), (2**63 - 1,), (-2**63,), (0.0,), (-0.5,), (1e300,), (-1e-300,),
    ("",), ("a",), ("a\x00",), ("a\x00b",), ("ab",), ("b",), ("ünïcode",),
    (1, "x"), (1, None), (1, "x", 2.5), (None, None),
]


@pytest.mark.parametrize("values", SAMPLES)
def test_round_trip(values):
    assert decode(encode(values)) == values


def test_negative_zero_is_zero():
    assert encode((-0.0,)) == encode((0.0,))


@pytest.mark.parametrize("make", [
    lambda r: r.randint(-2**63, 2**63 - 1),
    lambda r: r.uniform(-1e9, 1e9),
    lambda r: "".join(r.choice("ab\x00z") for _ in range(r.randint(0, 5))),
])
def test_byte_order_matches_value_order(make):
    r = random.Random(7)
    values = [make(r) for _ in range(500)]
    assert sorted(values, key=lambda v: encode((v,))) == sorted(values)


def test_tuple_order_and_nulls_first():
    rows = [(2, "a"), (1, "b"), (1, None), (1,), (None, "z"), (1, "a")]
    assert sorted(rows, key=encode) == [(None, "z"), (1,), (1, None), (1, "a"), (1, "b"), (2, "a")]


def test_rejects_unsupported_values():
    with pytest.raises(OverflowError):
        encode((2**63,))
    with pytest.raises(TypeError):
        encode((b"bytes",))
