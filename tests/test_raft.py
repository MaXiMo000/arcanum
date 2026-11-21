"""Raft replication: elections, replication, and its safety properties under random faults."""

import random

import pytest

from arcanum.errors import SqlError
from arcanum.replication.raft import NODES, Cluster, check_write


def run(cluster, ticks):
    for _ in range(ticks):
        cluster.tick()


def elected(seed=1):
    cluster = Cluster(seed=seed)
    run(cluster, 40)
    assert cluster.leader() is not None
    return cluster


def balances(cluster, node):
    return cluster.read(node, "SELECT id, balance FROM accounts ORDER BY id")["rows"]


def test_a_write_commits_on_every_node():
    cluster = elected()
    cluster.write("UPDATE accounts SET balance = balance - 10 WHERE id = 1")
    run(cluster, 15)
    for name in NODES:
        assert balances(cluster, name)[0] == (1, 90)
    assert cluster.entries()["2:1"]["result"] == {"ok": True, "rowcount": 1}  # entry 1 is the leader's no-op


def test_a_partitioned_leader_cannot_commit_and_its_entries_are_replaced():
    cluster = elected()
    old = cluster.leader().name
    others = [name for name in NODES if name != old]
    cluster.partition([[old], others])
    cluster.write("UPDATE accounts SET balance = 0 WHERE id = 2")  # reaches no majority
    run(cluster, 60)
    new = cluster.leader()
    assert new.name in others and new.term > cluster.nodes[old].term
    assert cluster.nodes[old].role == "leader" and cluster.nodes[old].commit < len(cluster.nodes[old].log)
    assert balances(cluster, old)[1] == (2, 100)  # never applied: it was never committed
    cluster.write("UPDATE accounts SET balance = 7 WHERE id = 3")
    run(cluster, 15)
    cluster.heal()
    run(cluster, 40)
    logs = {tuple(e["term"] for e in node.log) for node in cluster.nodes.values()}
    assert len(logs) == 1  # the old leader's stray entry was overwritten
    assert all(balances(cluster, name) == [(1, 100), (2, 100), (3, 7)] for name in NODES)


def test_a_restarted_node_rebuilds_its_database_from_the_log():
    cluster = elected()
    cluster.write("INSERT INTO accounts VALUES (4, 'Barbara', 50)")
    run(cluster, 15)
    follower = next(name for name in NODES if cluster.nodes[name].role == "follower")
    cluster.crash(follower)
    with pytest.raises(SqlError, match="down"):
        cluster.read(follower, "SELECT 1")
    cluster.write("UPDATE accounts SET balance = 60 WHERE id = 4")  # two of three still form a majority
    run(cluster, 15)
    cluster.restart(follower)
    assert len(balances(cluster, follower)) == 3  # the snapshot only, until the leader says what is committed
    run(cluster, 20)
    assert balances(cluster, follower)[-1] == (4, 60)


def test_writes_go_through_the_leader_and_must_be_deterministic():
    cluster = elected()
    follower = next(name for name in NODES if cluster.nodes[name].role == "follower")
    with pytest.raises(SqlError, match="not the leader"):
        cluster.write("DELETE FROM accounts", node=follower)
    for sql, message in [("INSERT INTO accounts VALUES (5, 'x', random())", "random"),
                         ("UPDATE accounts SET owner = now()", "now"),
                         ("BEGIN", "transaction control"),
                         ("SELECT * FROM accounts", "reads"),
                         ("DELETE FROM accounts; DELETE FROM accounts", "one statement")]:
        with pytest.raises(SqlError, match=message):
            check_write(sql)
    with pytest.raises(SqlError, match="SELECT"):
        cluster.read(follower, "DELETE FROM accounts")


def test_a_failing_statement_fails_the_same_way_everywhere():
    cluster = elected()
    position = cluster.write("INSERT INTO accounts VALUES (1, 'dup', 1)")
    run(cluster, 15)
    entry = cluster.entries()[f"{position}:{cluster.leader().term}"]
    assert entry["result"]["ok"] is False
    assert all(len(balances(cluster, name)) == 3 for name in NODES)


@pytest.mark.parametrize("seed", range(6))
def test_safety_under_random_faults(seed):
    rng = random.Random(seed)
    cluster = Cluster(seed=seed)
    leaders = {}  # term -> the one node that led it
    written = 0
    for step in range(900):
        roll = rng.random()
        if roll < 0.02:
            cluster.crash(rng.choice(NODES))
        elif roll < 0.05:
            cluster.restart(rng.choice(NODES))
        elif roll < 0.07:
            split = rng.sample(NODES, len(NODES))
            cut = rng.randint(1, 2)
            cluster.partition([split[:cut], split[cut:]])
        elif roll < 0.09:
            cluster.heal()
        elif roll < 0.2 and cluster.leader() and written < 120:
            written += 1
            cluster.write(f"UPDATE accounts SET balance = balance + {step % 7 - 3} WHERE id = {step % 3 + 1}")
        cluster.tick()
        for node in cluster.nodes.values():
            if node.up and node.role == "leader":
                assert leaders.setdefault(node.term, node.name) == node.name  # one leader per term
        # Committed prefixes never disagree, and every applied state matches the others' at that position.
        nodes = [x for x in cluster.nodes.values() if x.up]
        for a in nodes:
            for b in nodes:
                upto = min(a.commit, b.commit)
                assert a.log[:upto] == b.log[:upto]
    for name in NODES:
        cluster.restart(name)
    cluster.heal()
    run(cluster, 150)
    assert cluster.leader() is not None
    states = {tuple(balances(cluster, name)) for name in NODES}
    assert len(states) == 1  # after healing, every node has applied the same statements in the same order
    assert len({node.commit for node in cluster.nodes.values()}) == 1
    assert cluster.leader().commit > 20 and cluster.leader().term > 5  # the faults really were exercised
