"""The hash index and the column store: correct contents, and the page reads their designs promise."""

import random

import pytest

from arcanum.labs.access import compare
from arcanum.storage import Pager, encode
from arcanum.storage.columnstore import ColumnStore
from arcanum.storage.files import CountingMemFileSystem
from arcanum.storage.hashindex import HashIndex


def pager():
    return Pager("t.db", pool_size=1_000_000, fs=CountingMemFileSystem())


def reads(p, action):
    """action's result, and how many pages it read."""
    seen = []
    stop = p.events.subscribe(lambda kind, data: seen.append(1) if kind == "page_read" else None)
    try:
        return action(), len(seen)
    finally:
        stop()


def test_the_hash_index_finds_every_key_with_one_page_read():
    p = pager()
    index = HashIndex(p)
    rng = random.Random(3)
    keys = rng.sample(range(10**6), 3000)
    for k in keys:
        index.put(encode((k,)), f"value {k}".encode())
    index.put(encode((keys[0],)), b"replaced")
    assert index.splits > 10 and index.doublings > 3  # it really grew
    assert index.get(encode((keys[0],))) == b"replaced"
    assert index.get(encode((-1,))) is None
    for k in rng.sample(keys[1:], 200):
        value, count = reads(p, lambda k=k: index.get(encode((k,))))
        assert value == f"value {k}".encode() and count == 1
    assert sorted(k for k, _ in index.scan()) == sorted(encode((k,)) for k in keys)


def test_the_column_store_reads_only_the_columns_it_needs():
    p = pager()
    store = ColumnStore(p, ["id", "category", "price", "note"])
    rows = [(i, f"c{i // 500}", (i * 37) % 1000, f"note {i:05d} " + "x" * 30) for i in range(5000)]
    for row in rows:
        store.append(row)
    store.flush()
    assert all(store.row(i) == rows[i] for i in (0, 1, 499, 500, 2500, 4999))
    assert store.rows_between(498, 503) == rows[498:504] and store.rows_between(4990, 4999) == rows[4990:]
    _, one_row = reads(p, lambda: store.row(1234))
    assert one_row == 4  # one page from every column
    total, price_pages = reads(p, lambda: sum(v for _, v in store.columns["price"].scan()))
    assert total == sum(r[2] for r in rows) and price_pages == len(store.columns["price"].pages)
    assert price_pages * 5 < store.pages()  # the other columns stay on disk
    # Run-length encoding: a column of long runs fits in very few pages.
    assert len(store.columns["category"].pages) < len(store.columns["price"].pages)
    # Zone maps: ids are sorted, so a range of ids reads only the pages that can hold it.
    found, id_pages = reads(p, lambda: [r for r, v in store.columns["id"].scan(lambda lo, hi: hi >= 100 and lo <= 199)
                                        if 100 <= v <= 199])
    assert found == list(range(100, 200)) and id_pages <= 2


@pytest.mark.parametrize("values", [[None, None], [1, None, 1], ["a"] * 3000, list(range(3000))])
def test_column_pages_round_trip_whatever_the_values(values):
    store = ColumnStore(pager(), ["v"])
    for v in values:
        store.append((v,))
    assert [v for _, v in store.columns["v"].scan()] == values


def test_the_lab_shows_where_each_access_method_wins():
    lab = compare(rows=2000)
    pages = {q["key"]: q["pages"] for q in lab["queries"]}
    assert all(q["agree"] in (True, None) for q in lab["queries"])  # the same sums and counts everywhere
    assert pages["point"]["hash"] == 1 < pages["point"]["btree"] < pages["point"]["column"]
    assert pages["range"]["hash"] > 5 * pages["range"]["btree"]  # no order: every bucket
    assert pages["sum"]["column"] * 5 < pages["sum"]["btree"]  # one column of five
    assert pages["filter"]["column"] <= 2  # zone maps skip every page without the category
