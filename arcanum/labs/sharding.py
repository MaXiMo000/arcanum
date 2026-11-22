"""Sharding: one table split across several databases, by hash or by range.

The same orders are spread across `shards` Arcanum databases three ways:
hashing the order id, ranges of order ids, and hashing the customer id. A
coordinator answers each question by sending SQL only to the shards that can
hold the answer and combining what comes back, the way a sharded database's
router does. The lab counts, per strategy and question, how many shards were
asked, the pages they read in total, and the pages the busiest one read (the
one the answer waits for). It also shows how evenly the rows spread, and where
a batch of new orders lands: ids keep growing, so with ranges they all land on
the last shard, a hot spot.

The lesson is that a shard key serves the questions that name it: hashing ids
finds one order on one shard but must ask every shard for a customer's orders
or a range of ids; ranges keep neighbouring ids together; hashing customers
puts each customer's orders on one shard. Questions about everything ask every
shard whatever the key, and only gain from the work being split.
"""

import random
import zlib

from ..engine import Database
from ..storage.files import CountingMemFileSystem

STRATEGIES = {
    "hash-id": ("Hash of order id", "Each order on the shard its id hashes to."),
    "range-id": ("Ranges of order id", "Each shard holds one range of ids, in order."),
    "hash-customer": ("Hash of customer", "All of a customer's orders on the shard the customer hashes to."),
}
LOOKUPS = 40
INSERTS = 1000
RANGE_ROWS = 100


def _hash(value, shards):
    return zlib.crc32(str(value).encode()) % shards


class _Shard:
    def __init__(self):
        self.db = Database("shard", fs=CountingMemFileSystem(), pool_size=1_000_000)
        self.db.execute_script("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, "
                               "region TEXT); CREATE INDEX orders_customer ON orders (customer_id)")
        self.pages = 0
        self.db.events.subscribe(self._count)

    def _count(self, kind, data):
        if kind == "page_read":
            self.pages += 1

    def run(self, sql):
        """Rows of sql, and the pages reading them took."""
        before = self.pages
        rows = self.db.execute(sql).rows
        return rows, self.pages - before

    def insert(self, rows):
        for start in range(0, len(rows), 500):
            values = ", ".join(f"({i}, {c}, {a}, '{r}')" for i, c, a, r in rows[start:start + 500])
            self.db.execute(f"INSERT INTO orders VALUES {values}")


def compare(shards=4, rows=4000, seed=5):
    rng = random.Random(seed)
    customers = max(10, rows // 10)
    regions = ("north", "south", "east", "west")

    def order(i):
        return (i, (i * 7919) % customers + 1, round((i * 37) % 500 + 0.5, 2), regions[i % 4])

    data = [order(i) for i in range(1, rows + 1)]
    bounds = [rows * k // shards for k in range(1, shards)]  # range sharding: shard k holds ids up to bounds[k]

    def place(strategy, row):
        if strategy == "hash-id":
            return _hash(row[0], shards)
        if strategy == "range-id":
            return sum(row[0] > b for b in bounds)
        return _hash(row[1], shards)

    systems = {}
    for strategy in STRATEGIES:
        nodes = [_Shard() for _ in range(shards)]
        groups = [[] for _ in range(shards)]
        for row in data:
            groups[place(strategy, row)].append(row)
        for node, group in zip(nodes, groups, strict=True):
            node.insert(group)
        systems[strategy] = nodes

    def targets(strategy, question, value):
        everyone = list(range(shards))
        if question == "point":
            return [place(strategy, (value, 0))] if strategy != "hash-customer" else everyone
        if question == "customer":
            return [_hash(value, shards)] if strategy == "hash-customer" else everyone
        if question == "range":
            if strategy != "range-id":
                return everyone
            lo, hi = value, value + RANGE_ROWS - 1
            lower, upper = [0, *bounds], [*bounds, float("inf")]  # shard k holds ids in (lower, upper]
            return [k for k in everyone if lo <= upper[k] and hi > lower[k]]
        return everyone

    ids = [rng.randrange(1, rows + 1) for _ in range(LOOKUPS)]
    buyers = [rng.randrange(1, customers + 1) for _ in range(LOOKUPS)]
    starts = [rng.randrange(1, rows - RANGE_ROWS) for _ in range(LOOKUPS)]
    questions = [
        ("point", "One order by id", "SELECT amount FROM orders WHERE id = {}", ids),
        ("customer", "A customer's orders", "SELECT id, amount FROM orders WHERE customer_id = {}", buyers),
        ("range", f"{RANGE_ROWS} orders by id range", f"SELECT id FROM orders WHERE id BETWEEN {{}} AND {{}} + {RANGE_ROWS - 1}",
         starts),
        ("total", "Total of every order", "SELECT sum(amount), count(*) FROM orders", [None]),
    ]
    results = []
    for key, title, template, values in questions:
        per = {}
        for strategy, nodes in systems.items():
            asked = pages = busiest = 0
            answers = []
            for value in values:
                hit = targets(strategy, key, value)
                sql = template.format(value, value) if value is not None else template
                read = [nodes[k].run(sql) for k in hit]
                asked += len(hit)
                pages += sum(p for _, p in read)
                busiest += max(p for _, p in read)
                answers.append(sorted(r for rows_, _ in read for r in rows_))
            if key == "total":  # each shard sums its part; the coordinator adds the parts
                answers = [[round(sum(r[0] for r in answers[0]), 2), sum(r[1] for r in answers[0])]]
            n = len(values)
            per[strategy] = {"shards": round(asked / n, 1), "pages": round(pages / n, 1), "busiest": round(busiest / n, 1),
                             "answers": answers}
        same = len({repr(p.pop("answers")) for p in per.values()}) == 1
        results.append({"key": key, "title": title, "sql": template.replace("{}", "…"), "per": per, "agree": same})

    new = [order(i) for i in range(rows + 1, rows + INSERTS + 1)]
    landed = {s: [sum(place(s, r) == k for r in new) for k in range(shards)] for s in STRATEGIES}
    balance = {s: [n.run("SELECT count(*) FROM orders")[0][0][0] for n in nodes] for s, nodes in systems.items()}
    return {"shards": shards, "rows": rows, "customers": customers,
            "strategies": [{"key": k, "label": v[0], "about": v[1]} for k, v in STRATEGIES.items()],
            "questions": results, "balance": balance, "inserts": landed}
