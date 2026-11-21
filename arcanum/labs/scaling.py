"""How query cost grows with the data: a TPC-H-style dataset generated at doubling sizes.

The schema follows TPC-H's customer, orders and lineitem tables (simplified),
with four line items per order and one customer per ten orders, generated in
SQL from integer arithmetic so every run is identical. At each size the same
queries run once, and the lab counts the pages each one asks for (buffer pool
hits included, so the count is the work the plan does, not luck with the
cache) and its time. The slope of log(pages) against log(rows) names the
growth: about 0 for a B+tree lookup (logarithmic, so nearly flat), about 1 for
a full scan, a hash join or a sort's input (linear).
"""

import math
import time

from ..engine import Database
from ..storage.files import CountingMemFileSystem

SCHEMA = """
CREATE TABLE customer (c_custkey INTEGER PRIMARY KEY, c_name TEXT, c_nation INTEGER, c_segment TEXT);
CREATE TABLE orders (o_orderkey INTEGER PRIMARY KEY, o_custkey INTEGER, o_orderdate DATE, o_totalprice REAL,
                     o_priority TEXT);
CREATE TABLE lineitem (l_orderkey INTEGER, l_linenumber INTEGER, l_quantity INTEGER, l_extendedprice REAL,
                       l_discount REAL, l_shipdate DATE, l_returnflag TEXT, PRIMARY KEY (l_orderkey, l_linenumber));
CREATE INDEX orders_custkey ON orders (o_custkey);
"""

# Rows added to go from `low` orders to `high` (keys low+1 .. high); every table grows in proportion.
GROW = """
INSERT INTO customer SELECT n, 'Customer#' || n, n % 25,
    CASE n % 5 WHEN 0 THEN 'BUILDING' WHEN 1 THEN 'AUTOMOBILE' WHEN 2 THEN 'MACHINERY' WHEN 3 THEN 'HOUSEHOLD'
    ELSE 'FURNITURE' END
    FROM generate_series({low} / 10 + 1, {high} / 10) AS n;
INSERT INTO orders SELECT n, (n * 7919) % ({high} / 10) + 1, DATE '1992-01-01' + (n * 37) % 2400,
    (n * 7333) % 400000 / 100.0 + 900, CASE n % 5 WHEN 0 THEN '1-URGENT' WHEN 1 THEN '2-HIGH' ELSE '3-MEDIUM' END
    FROM generate_series({low} + 1, {high}) AS n;
INSERT INTO lineitem SELECT n / 4 + 1, n % 4 + 1, n % 50 + 1, (n * 37) % 9000 + 100.0, (n % 11) / 100.0,
    DATE '1992-01-01' + (n * 13) % 2400, CASE n % 3 WHEN 0 THEN 'A' WHEN 1 THEN 'N' ELSE 'R' END
    FROM generate_series({low} * 4, {high} * 4 - 1) AS n;
ANALYZE;
"""

QUERIES = {
    "point": ("Point lookup", "One order by its key: a single descent of the orders B+tree.",
              "SELECT o_totalprice FROM orders WHERE o_orderkey = {key}"),
    "customer": ("Index lookup", "One customer's orders through the orders_custkey index: a handful of rows "
                 "at every size.", "SELECT o_orderkey, o_totalprice FROM orders WHERE o_custkey = {customer}"),
    "q6": ("TPC-H Q6", "Forecasting revenue change: a filtered sum over every line item, so a full scan.",
           "SELECT sum(l_extendedprice * l_discount) FROM lineitem WHERE l_shipdate >= DATE '1994-01-01' "
           "AND l_shipdate < DATE '1995-01-01' AND l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24"),
    "q1": ("TPC-H Q1", "Pricing summary: a scan of line items into a hash aggregate of a few groups.",
           "SELECT l_returnflag, sum(l_quantity), avg(l_extendedprice), count(*) FROM lineitem "
           "WHERE l_shipdate <= DATE '1998-06-01' GROUP BY l_returnflag ORDER BY l_returnflag"),
    "q3": ("TPC-H Q3", "Shipping priority: customers joined to orders and line items, the top ten by revenue.",
           "SELECT o.o_orderkey, sum(l.l_extendedprice * (1 - l.l_discount)) AS revenue FROM customer c "
           "JOIN orders o ON o.o_custkey = c.c_custkey JOIN lineitem l ON l.l_orderkey = o.o_orderkey "
           "WHERE c.c_segment = 'BUILDING' AND o.o_orderdate < DATE '1995-03-15' "
           "GROUP BY o.o_orderkey ORDER BY revenue DESC LIMIT 10"),
    "sort": ("Sort", "Every order sorted by price: a scan plus an O(n log n) sort.",
             "SELECT o_orderkey FROM orders ORDER BY o_totalprice DESC, o_orderkey"),
}
SIZES = (250, 500, 1000, 2000, 4000)  # orders at each step (customers / 10, line items x 4)
FLAT, LINEAR = 0.3, 0.85  # slope thresholds between "logarithmic", "between" and "linear or more"


def compare(sizes=SIZES):
    """Run every query at every size: pages asked for, time, and the fitted growth of each."""
    db = Database("scaling", fs=CountingMemFileSystem(), pool_size=1_000_000)
    db.execute_script(SCHEMA)
    pages = [0]
    db.events.subscribe(lambda kind, data: pages.__setitem__(0, pages[0] + 1) if kind == "page_read" else None)
    results = {name: {"title": title, "about": about, "sql": sql, "points": []}
               for name, (title, about, sql) in QUERIES.items()}
    low = 0
    for high in sizes:
        db.execute_script(GROW.format(low=low, high=high))
        low = high
        for query in results.values():
            sql = query["sql"].format(key=high // 2, customer=high // 20 + 1)
            pages[0] = 0
            started = time.perf_counter()
            rows = len(db.execute(sql).rows)
            query["points"].append({"rows": high * 4, "orders": high, "pages": pages[0], "rows_out": rows,
                                    "ms": round((time.perf_counter() - started) * 1000, 2)})
    for query in results.values():
        query["slope"], query["growth"] = _growth(query["points"])
        query["sql"] = query["sql"].format(key="…", customer="…")
    return {"sizes": list(sizes), "queries": results}


def _growth(points):
    """Least-squares slope of log(pages) on log(line items), and what it means."""
    xs = [math.log(p["rows"]) for p in points]
    ys = [math.log(max(p["pages"], 1)) for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    spread = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / spread if spread else 0.0
    growth = "logarithmic" if slope < FLAT else "linear" if slope >= LINEAR else "sublinear"
    return round(slope, 2), growth
