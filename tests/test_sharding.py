"""The sharding lab: every strategy gives the same answers, and each shard key serves its own questions."""

from arcanum.labs.sharding import compare


def test_each_shard_key_serves_the_questions_that_name_it():
    lab = compare(shards=4, rows=1200)
    per = {q["key"]: q["per"] for q in lab["questions"]}
    assert all(q["agree"] for q in lab["questions"])  # the coordinator's merged answers match everywhere
    assert per["point"]["hash-id"]["shards"] == per["point"]["range-id"]["shards"] == 1
    assert per["point"]["hash-customer"]["shards"] == 4  # the id is not the shard key: ask everyone
    assert per["customer"]["hash-customer"]["shards"] == 1 and per["customer"]["hash-id"]["shards"] == 4
    assert per["range"]["range-id"]["shards"] < 2 < per["range"]["hash-id"]["shards"]
    assert all(p["shards"] == 4 for p in per["total"].values())  # everything: every shard, whatever the key
    assert all(sum(rows) == 1200 for rows in lab["balance"].values())
    assert lab["inserts"]["range-id"][-1] == sum(lab["inserts"]["range-id"])  # growing ids: one hot shard
    assert max(lab["inserts"]["hash-id"]) < 0.4 * sum(lab["inserts"]["hash-id"])  # hashing spreads them
