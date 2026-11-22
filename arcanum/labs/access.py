"""Three access methods on one table: a B+tree, a hash index and a column store.

The same rows (an order-like table: id, category, price, quantity, a note) are
loaded into Arcanum's B+tree keyed by id, an extendible hash index on id
(storage/hashindex.py) and a column store (storage/columnstore.py), each on its
own pager. Then the same questions are asked of each, and the lab counts the
pages each one reads: a point lookup, a range of ids, a sum over one column,
a count filtered on a column whose values come in runs, and a batch of new
rows (pages written). Pages read include buffer pool hits, so the counts
measure the access method, not the cache.

Each design wins somewhere: the hash index needs one page for a lookup but
cannot do ranges; the B+tree does both well; the column store reads only the
columns a question uses and skips pages by their min/max, but fetching one
whole row touches every column.
"""

import random
import time

from ..storage import PAGE_SIZE, BTree, Pager, decode, encode
from ..storage.columnstore import ColumnStore
from ..storage.files import CountingMemFileSystem
from ..storage.hashindex import HashIndex

COLUMNS = ("id", "category", "price", "qty", "note")
CATEGORIES = 12
ENGINES = ("btree", "hash", "column")
LOOKUPS = 200
RANGES = 40
RANGE_ROWS = 100
INSERTS = 500


def _row(i, rows):
    # Categories come in runs (rows are loaded by id, as a table loaded by date would group them).
    return (i, f"cat-{i * CATEGORIES // rows:02d}", (i * 7919) % 10_000, i % 50 + 1, f"order {i:06d} " + "~" * 28)


class _Counter:
    """Pages read on one pager, and the distinct pages written, from its events."""

    def __init__(self, pager):
        self.reads = 0
        self.written = set()
        pager.events.subscribe(self._on)

    def _on(self, kind, data):
        if kind == "page_read":
            self.reads += 1
        elif kind == "page_write":
            self.written.add(data["page"])


def compare(rows=5000, seed=3):
    rng = random.Random(seed)
    data = [_row(i, rows) for i in range(rows)]
    pagers = {name: Pager(f"access/{name}.db", pool_size=1_000_000, fs=CountingMemFileSystem()) for name in ENGINES}
    counters = {name: _Counter(p) for name, p in pagers.items()}
    tree, index = BTree.create(pagers["btree"]), HashIndex(pagers["hash"])
    store = ColumnStore(pagers["column"], COLUMNS)

    load = {}
    for name, put in (("btree", lambda r: tree.insert(encode(r[:1]), encode(r[1:]))),
                      ("hash", lambda r: index.put(encode(r[:1]), encode(r[1:]))),
                      ("column", store.append)):
        started = time.perf_counter()
        for row in data:
            put(row)
        if name == "column":
            store.flush()
        load[name] = round((time.perf_counter() - started) * 1000)

    def tree_rows(lo=None, hi=None):
        return [(*decode(k), *decode(v)) for k, v in tree.scan(lo, hi)]

    def hash_rows():
        return [(*decode(k), *decode(v)) for k, v in index.scan()]

    ids = [rng.randrange(rows) for _ in range(LOOKUPS)]
    starts = [rng.randrange(rows - RANGE_ROWS) for _ in range(RANGES)]
    category = f"cat-{CATEGORIES // 2:02d}"
    queries = [
        ("point", "Point lookup", "One order by its id.", LOOKUPS, {
            "btree": lambda: [tree.get(encode((i,))) for i in ids],
            "hash": lambda: [index.get(encode((i,))) for i in ids],
            "column": lambda: [store.row(i) for i in ids]}),
        ("range", "Range of ids", f"{RANGE_ROWS} consecutive orders by id.", RANGES, {
            "btree": lambda: [tree_rows(encode((s,)), encode((s + RANGE_ROWS,))) for s in starts],
            "hash": lambda: [[r for r in hash_rows() if s <= r[0] < s + RANGE_ROWS] for s in starts],
            "column": lambda: [store.rows_between(s, s + RANGE_ROWS - 1) for s in starts]}),
        ("sum", "Sum of one column", "sum(price) over every order.", 1, {
            "btree": lambda: sum(r[2] for r in tree_rows()),
            "hash": lambda: sum(r[2] for r in hash_rows()),
            "column": lambda: sum(v for _, v in store.columns["price"].scan())}),
        ("filter", "Filtered count", f"count(*) WHERE category = '{category}': the values come in runs.", 1, {
            "btree": lambda: sum(r[1] == category for r in tree_rows()),
            "hash": lambda: sum(r[1] == category for r in hash_rows()),
            "column": lambda: sum(v == category for _, v in store.columns["category"].scan(
                lambda lo, hi: lo <= category <= hi))}),
    ]
    results = []
    for key, title, about, repeats, runs in queries:
        pages, answers = {}, {}
        for name in ENGINES:
            before = counters[name].reads
            answers[name] = runs[name]()
            pages[name] = round((counters[name].reads - before) / repeats, 1)
        results.append({"key": key, "title": title, "about": about, "per": "query", "pages": pages,
                        "answer": answers["btree"] if key in ("sum", "filter") else None,
                        "agree": len(set(answers.values())) == 1 if key in ("sum", "filter") else None})

    new = [_row(i, rows) for i in range(rows, rows + INSERTS)]
    written = {}
    for name, put in (("btree", lambda r: tree.insert(encode(r[:1]), encode(r[1:]))),
                      ("hash", lambda r: index.put(encode(r[:1]), encode(r[1:]))),
                      ("column", store.append)):
        counters[name].written.clear()
        for row in new:
            put(row)
        if name == "column":
            store.flush()
        written[name] = len(counters[name].written)
    results.append({"key": "insert", "title": "Insert rows", "about": f"{INSERTS} new orders: the distinct pages written.",
                    "per": "batch", "pages": written, "answer": None, "agree": None})

    sizes = {"btree": len(tree.dump()), "hash": index.pages(), "column": store.pages()}
    return {"rows": rows, "load_ms": load, "queries": results,
            "engines": {name: {"pages": sizes[name], "bytes": sizes[name] * PAGE_SIZE} for name in ENGINES},
            "details": {"hash_splits": index.splits, "hash_doublings": index.doublings,
                        "hash_depth": index.global_depth,
                        "column_pages": {c: len(store.columns[c].pages) for c in COLUMNS}}}
