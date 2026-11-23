"""The HTTP/WebSocket API, with real worker processes behind it."""

import asyncio
import os
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from arcanum.server.app import Settings, _forward, create_app
from arcanum.server.sessions import Subscriber
from arcanum.server.worker import error_json
from arcanum.storage import CorruptDatabaseError

GENEROUS = {"query_timeout": 3, "max_sessions": 2, "max_sessions_per_client": 5, "max_rows": 5, "max_pages": 300,
                "max_sql_bytes": 2000, "rate_per_second": 1000, "rate_burst": 1000, "sessions_per_minute": 1000,
                "crashes_per_minute": 1000}


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def make(**overrides):
        settings = Settings(data_dir=str(tmp_path / "data"), web_dir=str(tmp_path / "no-web"), **{**GENEROUS, **overrides})
        client = TestClient(create_app(settings))
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def new_session(client):
    response = client.post("/sessions")
    assert response.status_code == 201, response.text
    return response.json()["id"]


def sql(client, session, text):
    return client.post(f"/sessions/{session}/query", json={"sql": text})


def events_until(ws, kind):
    """Read event batches until one of `kind` arrives; return every kind seen."""
    seen = set()
    while kind not in seen:
        message = ws.receive_json()
        seen |= {e["kind"] for e in message.get("events", [])}
    return seen


# ---- behaviour ------------------------------------------------------------------------------


def test_queries_return_rows_and_the_traced_plan(client):
    session = new_session(client)
    response = sql(client, session, "SELECT id, name FROM customers ORDER BY id")
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["columns"] == ["id", "name"]
    assert result["rows"] == [[i, f"Customer {i}"] for i in range(1, 6)]  # capped at max_rows
    assert result["truncated"] and result["row_total"] == 24

    plan = sql(client, session, "SELECT id FROM customers WHERE id > 20").json()["results"][0]["plan"]
    assert plan["operator"] == "Project" and plan["rows"] == 4 and plan["est_rows"] > 0
    scan = plan["children"][0]
    assert scan["label"] == "IndexScan customers using primary key (id > 20)" and scan["rows"] == 4

    reply = sql(client, session, "INSERT INTO customers VALUES (90, 'New', 'Rome'); SELECT count(*) FROM customers").json()
    results = reply["results"]
    assert [r["status"] for r in results] == ["INSERT 1", "SELECT 1"] and results[1]["rows"] == [[25]]
    # What the engine did for the whole request, counted exactly whatever the event stream drops.
    stats = reply["stats"]
    assert stats["commits"] == 1 and stats["log_frames"] >= 2 and stats["pages_written"] >= 1
    assert stats["cache_hits"] + stats["disk_reads"] >= stats["nodes_visited"] > 0 and stats["elapsed_ms"] > 0

    lookup = sql(client, session, "SELECT name FROM customers WHERE id = 7").json()["stats"]
    assert lookup["nodes_visited"] == 3 and lookup["log_frames"] == 0  # root, internal, leaf; read-only


def test_errors_are_structured(client):
    session = new_session(client)
    error = sql(client, session, "SELECT nope FROM customers").json()["error"]
    doctor = error.pop("doctor")
    assert error == {"type": "SqlError", "message": "column nope does not exist", "line": 1, "column": 8}
    assert doctor["pos"] == 7 and doctor["end"] == 11 and "no column nope" in doctor["hint"]
    typo = sql(client, session, "SELECT nme FROM customers").json()["error"]["doctor"]
    assert typo["fix"] == typo["rerun"] == "SELECT name FROM customers"
    # Statements before the failing one ran; the rerun starts at the failing statement.
    partial = sql(client, session, "UPDATE customers SET city = 'Rome' WHERE id = 3; SELECT nme FROM customers").json()
    assert partial["error"]["completed"] == 1 and partial["error"]["doctor"]["rerun"] == "SELECT name FROM customers"
    response = sql(client, session, "INSERT INTO customers VALUES (1, 'Dup', NULL)")
    assert response.status_code == 400 and response.json()["error"]["type"] == "ConstraintError"
    advice = sql(client, session, "SELECT * FROM customers WHERE name = NULL").json()["results"][0]["advice"]
    assert advice[0]["code"] == "null-compare" and "IS NULL" in advice[0]["fix"]["sql"]
    assert sql(client, session, "SELECT 1 " + " " * 3000).status_code == 413
    assert sql(client, "x" * 24, "SELECT 1").status_code == 404
    assert sql(client, "../../etc/passwd", "SELECT 1").status_code == 404


def test_events_stream_the_engine_at_work(client):
    session = new_session(client)
    with client.websocket_connect(f"/sessions/{session}/events") as ws:
        assert ws.receive_json()["type"] == "subscribed"
        sql(client, session, "INSERT INTO customers SELECT id + 100, name, city FROM customers")
        seen = events_until(ws, "commit")
        sql(client, session, "SELECT * FROM customers WHERE id = 3")
        requests = set()
        while len(requests) < 1 or "node_visit" not in seen:
            events = ws.receive_json().get("events", [])
            requests |= {e["request"] for e in events}
            seen |= {e["kind"] for e in events}
    assert {"page_read", "node_visit", "node_split", "wal_append", "wal_sync", "commit"} <= seen
    assert len(requests) == 1  # every event of one request carries its number, for replaying it


def test_pulling_the_plug_mid_write_keeps_every_acknowledged_commit(client):
    session = new_session(client)
    with client.websocket_connect(f"/sessions/{session}/events") as ws:
        ws.receive_json()
        for _ in range(3):
            summary = client.post(f"/sessions/{session}/crash", json={"during_write": True}).json()
            assert summary["consistent"], summary
            assert summary["recovered_frames"] > 0
            seen = events_until(ws, "recovered")
            assert {"crash", "recovery", "checkpoint"} <= seen
    rows = sql(client, session, "SELECT count(*) FROM crash_demo").json()["results"][0]["rows"]
    assert rows[0][0] == summary["rows_after_recovery"]
    assert sql(client, session, "SELECT count(*) FROM orders").json()["results"][0]["rows"] == [[80]]


def test_state_describes_trees_cache_and_log(client):
    session = new_session(client)
    state = client.get(f"/sessions/{session}/state").json()
    trees = {t["name"]: t for t in state["trees"]}
    assert set(trees) == {"catalog", "customers", "orders", "orders_customer"}
    customers = trees["customers"]
    assert customers["key_columns"] == ["id"] and customers["nodes"][0]["page"] == customers["root"]
    leaves = [n for n in customers["nodes"] if n["leaf"]]
    assert sorted(k[0] for leaf in leaves for k in leaf["keys"]) == list(range(1, 25))
    assert all(len(n["keys"]) <= 4 for n in customers["nodes"])  # max_keys keeps the tree visibly branchy
    assert trees["orders_customer"]["key_columns"] == ["customer_id", "id"]
    assert state["pager"]["page_count"] > 10 and state["pager"]["cached"]


# ---- resource limits ----------------------------------------------------------------------------


def test_runaway_statement_cancels_itself_and_keeps_the_session(client):
    session = new_session(client)
    sql(client, session, "BEGIN; INSERT INTO customers VALUES (77, 'Kept', 'Lima')")
    response = sql(client, session, "SELECT count(*) FROM orders a, orders b, orders c, orders d")
    error = response.json()["error"]
    assert response.status_code == 400 and error["type"] == "StatementTimeoutError"
    assert "statement timeout" in error["message"]
    # The worker was not restarted: the open transaction (and its row) are still there.
    assert sql(client, session, "SELECT name FROM customers WHERE id = 77").json()["results"][0]["rows"] == [["Kept"]]
    sql(client, session, "COMMIT")


def test_a_statement_that_cannot_be_interrupted_is_killed_without_losing_data(client):
    session = new_session(client)
    sql(client, session, "INSERT INTO customers VALUES (78, 'Kept', 'Lima')")
    # One regular expression match runs inside Python's re module and cannot stop halfway: the server
    # kills the worker at the query timeout and starts a new one from the committed data.
    response = sql(client, session, "SELECT 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!' ~ '(a+)+$'")
    assert response.status_code == 408 and response.json()["error"]["type"] == "QueryTimeout"
    assert sql(client, session, "SELECT name FROM customers WHERE id = 78").json()["results"][0]["rows"] == [["Kept"]]


def test_statements_that_need_too_much_memory_are_refused(make_client):
    client = make_client(memory_rows=1000, recursive_rows=500)
    session = new_session(client)
    for statement in ["SELECT DISTINCT a.id, b.id FROM orders a, orders b",
                      "SELECT a.id, count(*) FROM orders a, orders b GROUP BY a.id, b.id",
                      "INSERT INTO customers SELECT a.id * 1000 + b.id, 'x', 'y' FROM orders a, orders b"]:
        error = sql(client, session, statement).json()["error"]
        assert error["type"] == "ExecutionError" and "rows in memory" in error["message"]
    error = sql(client, session, "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) "
                                 "SELECT count(*) FROM r").json()["error"]
    assert "more than 500 rows" in error["message"]
    # Streaming results are not held: the first rows come back and the rest are only counted.
    result = sql(client, session, "SELECT a.id, b.id FROM orders a, orders b").json()["results"][0]
    assert result["row_total"] == 6400 and len(result["rows"]) == 5


def test_database_size_is_capped(client):
    session = new_session(client)
    sql(client, session, "CREATE TABLE blob (id INTEGER PRIMARY KEY, body TEXT)")
    error = None
    for start in range(0, 100_000, 400):
        response = sql(client, session, f"INSERT INTO blob SELECT n, substr('{'x' * 900}', 1, 900) FROM "
                                        f"(WITH RECURSIVE s(n) AS (SELECT {start + 1} UNION ALL SELECT n + 1 FROM s "
                                        f"WHERE n < {start + 400}) SELECT n FROM s) AS q")
        if response.status_code != 200:
            error = response.json()["error"]
            break
    assert error is not None and error["type"] == "ExecutionError" and "full" in error["message"]
    count = sql(client, session, "SELECT count(*) FROM blob").json()["results"][0]["rows"][0][0]
    assert count % 400 == 0  # the statement that hit the limit left nothing behind


@pytest.mark.parametrize("statement,fragment", [
    ("SELECT 1e999", "out of range"),
    ("SELECT 1e300 * 1e300", "numeric overflow"),
    ("SELECT CAST('nan' AS REAL)", "cannot convert"),
    ("SELECT " + "(" * 100 + "1" + ")" * 100, "nested too deeply"),
    ("SELECT " + "- " * 200 + "1", "nested too deeply"),
    ("SELECT " + " + ".join(["1"] * 400), "nested too deeply"),
])
def test_hostile_sql_gets_a_clean_error(client, statement, fragment):
    session = new_session(client)
    response = sql(client, session, statement)
    assert response.status_code == 400 and fragment in response.json()["error"]["message"]
    assert sql(client, session, "SELECT 1").status_code == 200  # and the session carries on


def test_sessions_are_limited_and_deletable(client, tmp_path):
    first, second = new_session(client), new_session(client)
    response = client.post("/sessions")
    assert response.status_code == 503 and response.json()["error"]["type"] == "ServerFull"
    assert client.delete(f"/sessions/{first}").status_code == 204
    assert not os.path.exists(tmp_path / "data" / f"{first}.db")
    assert sql(client, first, "SELECT 1").status_code == 404
    new_session(client)
    assert sql(client, second, "SELECT 1").status_code == 200


# ---- abuse protection ------------------------------------------------------------------------------


def test_requests_are_rate_limited_per_client(make_client):
    client = make_client(rate_per_second=1, rate_burst=5)
    statuses = [client.get("/health").status_code for _ in range(8)]
    assert statuses[:5] == [200] * 5 and statuses[-1] == 429
    response = client.get("/health")
    assert response.headers["retry-after"] == "1" and response.json()["error"]["type"] == "RateLimited"


def test_session_creation_is_rate_limited_and_capped_per_client(make_client):
    client = make_client(max_sessions=10, max_sessions_per_client=2, sessions_per_minute=3)
    new_session(client), new_session(client)
    response = client.post("/sessions")
    assert response.status_code == 429 and response.json()["error"]["type"] == "TooManySessions"
    response = client.post("/sessions")
    assert response.status_code == 429 and response.json()["error"]["type"] == "RateLimited"


def test_request_bodies_are_bounded(client):
    session = new_session(client)
    response = client.post(f"/sessions/{session}/query", content=b"{" + b" " * 50_000 + b"}",
                           headers={"content-type": "application/json"})
    assert response.status_code == 413

    def chunks():
        yield b'{"sql": "SELECT 1"}'

    response = client.post(f"/sessions/{session}/query", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 411


def test_invalid_requests_do_not_echo_their_input(client):
    session = new_session(client)
    response = client.post(f"/sessions/{session}/query", json={"sql": {"secret-looking": "payload"}})
    assert response.status_code == 422
    assert "payload" not in response.text and response.json()["error"]["type"] == "InvalidRequest"


def test_responses_carry_security_headers_and_no_docs(client):
    response = client.get("/health")
    for header in ("x-content-type-options", "referrer-policy", "x-frame-options", "cache-control"):
        assert header in response.headers
    assert response.headers["cache-control"] == "no-store" and "server" not in response.headers
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_websockets_check_origin_and_are_capped(make_client):
    client = make_client(max_websockets_per_session=1)
    session = new_session(client)
    events = f"/sessions/{session}/events"
    with pytest.raises(WebSocketDisconnect) as refused, \
            client.websocket_connect(events, headers={"origin": "https://evil.example"}):
        pass
    assert refused.value.code == 4403
    with client.websocket_connect(events, headers={"origin": "http://localhost:5173"}) as ws:
        ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as capped, client.websocket_connect(events):
            pass
        assert capped.value.code == 4429


def test_internal_errors_do_not_leak_details():
    error = error_json(CorruptDatabaseError("C:/private/data/abc.db is not an Arcanum database"))
    assert error["type"] == "InternalError" and "private" not in error["message"] and "reference" in error["message"]


def test_stale_session_files_are_removed_at_startup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    stale, unrelated = data / ("A" * 24 + ".db"), data / "notes.db"
    stale.write_bytes(b"old")
    unrelated.write_bytes(b"keep")
    create_app(Settings(data_dir=str(data)))
    assert not stale.exists() and unrelated.exists()


def test_other_sites_cannot_change_anything(client):
    foreign = {"Origin": "https://evil.example"}
    assert client.post("/sessions", headers=foreign).status_code == 403
    session = new_session(client)
    assert client.post(f"/sessions/{session}/query", json={"sql": "DELETE FROM orders"}, headers=foreign).status_code == 403
    assert client.delete(f"/sessions/{session}", headers=foreign).status_code == 403
    # The page's own origin, an allowed one and requests without Origin (scripts, curl) still work.
    assert client.post("/sessions", headers={"Origin": "http://testserver"}).status_code == 201
    assert sql(client, session, "SELECT count(*) FROM orders").json()["results"][0]["rows"] == [[80]]


def test_a_conflicting_write_waits_and_resumes_when_the_holder_commits(client):
    session = new_session(client)

    def run(text, who):
        return client.post(f"/sessions/{session}/query", json={"sql": text, "session": who})

    run("BEGIN; UPDATE customers SET city = 'Rome' WHERE id = 1", "A")
    parked = run("SELECT 1; UPDATE customers SET city = 'Lima' WHERE id = 1; SELECT city FROM customers WHERE id = 1", "B")
    assert parked.status_code == 202
    waiting = parked.json()["waiting"]
    assert waiting["session"] == "B" and waiting["holder_session"] == "A"
    assert [r["status"] for r in parked.json()["results"]] == ["SELECT 1"]  # what ran before it waited
    assert client.get(f"/sessions/{session}/state").json()["waits"][0]["holder"] == waiting["holder"]
    ticket = f"/sessions/{session}/tickets/{waiting['ticket']}"
    assert client.get(ticket).status_code == 202
    busy = run("SELECT 2", "B")
    assert busy.status_code == 400 and "still waiting" in busy.json()["error"]["message"]

    assert run("COMMIT", "A").status_code == 200  # A was never blocked by B's wait
    done = client.get(ticket)
    assert done.status_code == 200
    assert [r["status"] for r in done.json()["results"]] == ["SELECT 1", "UPDATE 1", "SELECT 1"]
    assert done.json()["results"][2]["rows"] == [["Lima"]]  # it went ahead on A's committed version
    assert client.get(ticket).status_code == 400  # a ticket is collected once
    assert client.get(f"/sessions/{session}/state").json()["waits"] == []


def test_deadlocks_roll_back_the_transaction_that_closes_the_cycle(client):
    session = new_session(client)

    def run(text, who):
        return client.post(f"/sessions/{session}/query", json={"sql": text, "session": who})

    run("BEGIN; UPDATE customers SET city = 'x' WHERE id = 1", "A")
    run("BEGIN; UPDATE customers SET city = 'y' WHERE id = 2", "B")
    waiting = run("UPDATE customers SET city = 'y' WHERE id = 1", "B").json()["waiting"]
    deadlock = run("UPDATE customers SET city = 'x' WHERE id = 2", "A")
    assert deadlock.status_code == 400 and deadlock.json()["error"]["type"] == "DeadlockError"
    state = client.get(f"/sessions/{session}/state").json()
    assert state["sessions"]["A"]["xid"] is None  # A's transaction was rolled back...
    done = client.get(f"/sessions/{session}/tickets/{waiting['ticket']}")
    assert done.status_code == 200  # ...so B's wait ended and its update went through
    run("COMMIT", "B")
    rows = run("SELECT id, city FROM customers WHERE id <= 2 ORDER BY id", "A").json()["results"][0]["rows"]
    assert rows == [[1, "y"], [2, "y"]]


def test_serializable_shows_and_refuses_read_write_dependencies(client):
    session = new_session(client)

    def run(text, who):
        return client.post(f"/sessions/{session}/query", json={"sql": text, "session": who})

    run("BEGIN ISOLATION LEVEL SERIALIZABLE; SELECT count(*) FROM customers WHERE id <= 2", "A")
    run("BEGIN ISOLATION LEVEL SERIALIZABLE; SELECT count(*) FROM customers WHERE id <= 2", "B")
    run("UPDATE customers SET city = 'x' WHERE id = 1", "A")
    state = client.get(f"/sessions/{session}/state").json()
    assert [(d["reader_session"], d["writer_session"]) for d in state["dependencies"]] == [("B", "A")]
    assert {s["session"] for s in state["serializable"]} == {"A", "B"}
    failed = run("UPDATE customers SET city = 'y' WHERE id = 2", "B")
    error = failed.json()["error"]
    assert failed.status_code == 400 and error["type"] == "SerializationError"
    assert len(error["dependencies"]) == 2
    assert client.get(f"/sessions/{session}/state").json()["sessions"]["B"]["xid"] is None  # rolled back
    assert run("COMMIT", "A").status_code == 200


def test_waits_time_out_and_can_be_cancelled(make_client):
    client = make_client(lock_timeout=0.2)
    session = new_session(client)

    def run(text, who):
        return client.post(f"/sessions/{session}/query", json={"sql": text, "session": who})

    run("BEGIN; DELETE FROM orders WHERE id = 5", "A")
    first = run("UPDATE orders SET amount = 1 WHERE id = 5", "B").json()["waiting"]["ticket"]
    time.sleep(0.3)
    timed_out = client.get(f"/sessions/{session}/tickets/{first}")
    assert timed_out.status_code == 400 and timed_out.json()["error"]["type"] == "LockTimeoutError"
    second = run("UPDATE orders SET amount = 2 WHERE id = 5", "B").json()["waiting"]["ticket"]
    cancelled = client.post(f"/sessions/{session}/tickets/{second}/cancel")
    assert cancelled.status_code == 400 and "user request" in cancelled.json()["error"]["message"]
    assert client.get(f"/sessions/{session}/tickets/t99999").status_code == 400
    assert client.get(f"/sessions/{session}/tickets/../x").status_code == 404


def test_two_sessions_see_each_other_only_after_commit(client):
    session = new_session(client)

    def run(text, who):
        return client.post(f"/sessions/{session}/query", json={"sql": text, "session": who})

    assert run("BEGIN; UPDATE customers SET city = 'Rome' WHERE id = 1", "A").status_code == 200
    seen = run("SELECT city FROM customers WHERE id = 1", "B").json()["results"][0]["rows"]
    assert seen == [["Osaka"]]
    blocked = run("UPDATE customers SET city = 'Lima' WHERE id = 1", "B")
    assert blocked.status_code == 202  # waits for A
    client.post(f"/sessions/{session}/tickets/{blocked.json()['waiting']['ticket']}/cancel")

    state = client.get(f"/sessions/{session}/state").json()
    a = state["sessions"]["A"]
    assert a["xid"] and a["isolation"] == "READ COMMITTED" and state["sessions"]["B"]["xid"] is None
    [row] = [r for r in state["versions"] if r["table"] == "customers" and r["key"] == [1]]
    old, new = row["versions"]
    assert old["xmax"] == new["xmin"] == a["xid"]
    assert old["seen_by"] == ["B"] and new["seen_by"] == ["A"]

    run("COMMIT", "A")
    assert run("SELECT city FROM customers WHERE id = 1", "B").json()["results"][0]["rows"] == [["Rome"]]
    assert client.get(f"/sessions/{session}/state").json()["versions"] == []  # settled again
    assert client.post(f"/sessions/{session}/query", json={"sql": "SELECT 1", "session": "C"}).status_code == 422


def test_lab_compares_a_btree_and_an_lsm_tree_on_one_workload(client):
    session = new_session(client)
    reply = client.post(f"/sessions/{session}/labs/engines", json={"keys": 1500, "order": "random", "updates": 0.5})
    assert reply.status_code == 200
    lab = reply.json()
    assert lab["workload"]["operations"] == 2250
    # The classic trade-off: the LSM tree writes far less for random writes.
    assert lab["lsm"]["write_amp"] < lab["btree"]["write_amp"]
    assert lab["lsm"]["flushes"] > 0 and lab["shapes"]
    assert client.post(f"/sessions/{session}/labs/engines", json={"keys": 10**6}).status_code == 422
    assert client.post(f"/sessions/{session}/labs/engines", json={"nope": 1}).status_code == 422
    assert client.post(f"/sessions/{session}/labs/nope", json={}).status_code == 404


def test_buffer_pool_lab_replays_one_trace_through_every_policy(client):
    session = new_session(client)
    reply = client.post(f"/sessions/{session}/labs/buffer-pool",
                        json={"workload": "hot-scan", "pool_pages": 16, "rows": 800, "lookups": 300})
    assert reply.status_code == 200
    lab = reply.json()
    rates = {name: p["hit_rate"] for name, p in lab["policies"].items()}
    assert set(rates) == {"lru", "clock", "2q", "optimal"}
    assert all(rates["optimal"] >= rate for rate in rates.values())  # nothing beats Belady
    assert lab["sweep"]["sizes"][-1] == lab["workload"]["distinct_pages"]
    assert all(rates[-1] == lab["sweep"]["hit_rates"]["optimal"][-1] for rates in lab["sweep"]["hit_rates"].values())


def test_rolling_back_to_a_savepoint_wakes_waiting_statements(client):
    session = new_session(client)

    def run(text, who):
        return client.post(f"/sessions/{session}/query", json={"sql": text, "session": who})

    run("BEGIN; UPDATE customers SET city = 'x' WHERE id = 1; SAVEPOINT s; UPDATE customers SET city = 'y' WHERE id = 2", "A")
    waiting = run("UPDATE customers SET city = 'b' WHERE id = 2", "B").json()["waiting"]
    state = client.get(f"/sessions/{session}/state").json()
    assert [sp["name"] for sp in state["sessions"]["A"]["savepoints"]] == ["s"]
    assert run("ROLLBACK TO s", "A").status_code == 200  # releases row 2, while A keeps row 1
    assert client.get(f"/sessions/{session}/tickets/{waiting['ticket']}").status_code == 200
    run("COMMIT", "A")
    rows = run("SELECT id, city FROM customers WHERE id <= 2 ORDER BY id", "A").json()["results"][0]["rows"]
    assert rows == [[1, "x"], [2, "b"]]


def test_long_values_are_shortened_in_responses(client):
    session = new_session(client)
    result = sql(client, session, "SELECT repeat('x', 5000) AS long, 'short' AS short").json()["results"][0]
    long, short = result["rows"][0]
    assert result["clipped"] == 1 and short == "short"
    assert long.startswith("x" * 2000) and long.endswith("(5,000 characters in all)")


def test_the_replication_cluster_elects_replicates_and_refuses_bad_steps(client):
    session = new_session(client)
    path = f"/sessions/{session}/cluster"
    frames = client.post(path, json={"action": "tick", "ticks": 40}).json()["frames"]
    assert len(frames) == 40
    leader = next(n["name"] for n in frames[-1]["nodes"] if n["role"] == "leader")
    reply = client.post(path, json={"action": "write", "sql": "UPDATE accounts SET balance = 1 WHERE id = 1"})
    assert reply.status_code == 200 and reply.json()["position"] == 2
    client.post(path, json={"action": "tick", "ticks": 20})
    follower = next(n for n in ("n1", "n2", "n3") if n != leader)
    read = client.post(path, json={"action": "read", "node": follower, "sql": "SELECT balance FROM accounts WHERE id = 1"})
    assert read.json()["read"]["rows"] == [[1]]
    refused = client.post(path, json={"action": "write", "sql": "UPDATE accounts SET balance = random()"})
    assert refused.status_code == 400 and "random" in refused.json()["error"]["message"]
    assert refused.json()["state"]["now"] == 60  # the state still comes back, to keep the view current
    for bad in ({"action": "tick", "ticks": 1000}, {"action": "read", "sql": "SELECT 1"},
                {"action": "partition", "groups": [["n1"], ["n1", "n2"]]}, {"action": "crash", "node": "n9"},
                {"action": "write", "sql": "x", "extra": 1}):
        assert client.post(path, json=bad).status_code == 422
    split = client.post(path, json={"action": "partition", "groups": [["n1"], ["n2", "n3"]]}).json()
    assert split["state"]["groups"] == [["n1"], ["n2", "n3"]]
    assert client.post(path, json={"action": "reset"}).json()["state"]["now"] == 0


def test_csv_import_previews_then_loads_a_table(client):
    session = new_session(client)
    path = f"/sessions/{session}/import"
    text = "City,Population,Founded\nLisbon,545000,1179-01-01\nOsaka,2750000,\n"
    preview = client.post(path, json={"table": "cities", "csv": text, "dry_run": True}).json()
    assert preview["create"] and preview["rows_total"] == 2
    assert [c["type"] for c in preview["columns"]] == ["TEXT", "INTEGER", "DATE"]
    assert "cities" not in [t["name"] for t in client.get(f"/sessions/{session}/state").json()["tables"]]
    reply = client.post(path, json={"table": "cities", "csv": text})
    assert reply.status_code == 200 and [r["status"] for r in reply.json()["results"]] == ["CREATE TABLE", "INSERT 2"]
    rows = client.post(f"/sessions/{session}/query", json={"sql": "SELECT city, population FROM cities ORDER BY 1"})
    assert rows.json()["results"][0]["rows"] == [["Lisbon", 545000], ["Osaka", 2750000]]
    bad = client.post(path, json={"table": "cities", "csv": "population\nmany\n"})
    assert bad.status_code == 400 and "line 2" in bad.json()["error"]["message"]
    assert client.post(path, json={"table": "Bad Name", "csv": "a\n1"}).status_code == 422
    huge = "a\n" + "1\n" * 300_000
    assert client.post(path, json={"table": "big", "csv": huge}).status_code == 413


def test_the_scaling_lab_names_how_each_query_grows(client):
    session = new_session(client)
    reply = client.post(f"/sessions/{session}/labs/scaling", json={})
    assert reply.status_code == 200
    queries = reply.json()["queries"]
    assert queries["point"]["growth"] == "logarithmic" and queries["q6"]["growth"] == "linear"
    assert all(len(q["points"]) == len(reply.json()["sizes"]) for q in queries.values())
    assert client.post(f"/sessions/{session}/labs/scaling", json={"sizes": [1]}).status_code == 422


def test_a_classroom_streams_the_teachers_posts_to_students(client):
    opened = client.post("/classrooms", json={"title": "Indexes 101"})
    assert opened.status_code == 201
    code, token = opened.json()["code"], opened.json()["token"]
    teacher = {"authorization": f"Bearer {token}"}
    post = {"sql": "SELECT 1 AS one", "results": [{"status": "SELECT 1", "columns": ["one"], "rows": [[1]], "row_total": 1}]}
    assert client.post(f"/classrooms/{code}/posts", json=post, headers=teacher).json() == {"seq": 1, "students": 0}
    with client.websocket_connect(f"/classrooms/{code.lower()}/live") as ws:  # codes are case-insensitive
        joined = ws.receive_json()
        assert joined["type"] == "joined" and joined["title"] == "Indexes 101" and len(joined["history"]) == 1
        reply = client.post(f"/classrooms/{code}/posts", json={**post, "sql": "SELECT 2"}, headers=teacher)
        assert reply.json()["students"] == 1
        assert ws.receive_json()["post"]["sql"] == "SELECT 2"
        # Students only watch: without the token nothing can be posted or ended.
        for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": token}):
            assert client.post(f"/classrooms/{code}/posts", json=post, headers=headers).status_code == 403
            assert client.delete(f"/classrooms/{code}", headers=headers).status_code == 403
        assert client.delete(f"/classrooms/{code}", headers=teacher).status_code == 204
        with pytest.raises(WebSocketDisconnect) as ended:
            ws.receive_json()
        assert ended.value.code == 4410
    assert client.get(f"/classrooms/{code}").status_code == 404
    too_many_rows = {"sql": "SELECT 1", "results": [{"status": "x", "rows": [[1]] * 21}]}
    room = client.post("/classrooms", json={}).json()
    assert client.post(f"/classrooms/{room['code']}/posts", json=too_many_rows,
                       headers={"authorization": f"Bearer {room['token']}"}).status_code == 422


def test_classrooms_are_capped_per_client(make_client):
    client = make_client(max_classrooms_per_client=1, classrooms_per_minute=10)
    assert client.post("/classrooms", json={}).status_code == 201
    assert client.post("/classrooms", json={}).status_code == 503


def test_students_follow_the_teachers_lesson_and_ask_questions_only_the_teacher_reads(client):
    room = client.post("/classrooms", json={}).json()
    code, teacher = room["code"], {"authorization": f"Bearer {room['token']}"}
    with client.websocket_connect(f"/classrooms/{code}/live") as ws:
        assert ws.receive_json()["lesson"] is None
        assert client.post(f"/classrooms/{code}/lesson", json={"lesson": "joins", "step": 2}, headers=teacher).status_code == 200
        assert ws.receive_json() == {"type": "lesson", "lesson": {"id": "joins", "step": 2}}
    assert client.post(f"/classrooms/{code}/lesson", json={"lesson": "joins"}).status_code == 403
    assert client.post(f"/classrooms/{code}/lesson", json={"lesson": "Bad Id!"}, headers=teacher).status_code == 422
    asked = client.post(f"/classrooms/{code}/questions", json={"text": "Why a merge join here?", "name": "Sam"})
    assert asked.status_code == 201
    assert "questions" not in client.get(f"/classrooms/{code}").json()  # students cannot read them
    questions = client.get(f"/classrooms/{code}", headers=teacher).json()["questions"]
    assert [(q["text"], q["name"]) for q in questions] == [("Why a merge join here?", "Sam")]
    assert client.post(f"/classrooms/{code}/questions", json={"text": "   "}).status_code == 422
    assert client.post(f"/classrooms/{code}/questions", json={"text": "x" * 301}).status_code == 422
    assert client.delete(f"/classrooms/{code}/questions/{asked.json()['id']}").status_code == 403
    assert client.delete(f"/classrooms/{code}/questions/{asked.json()['id']}", headers=teacher).status_code == 204
    assert client.get(f"/classrooms/{code}", headers=teacher).json()["questions"] == []
    assert client.get(f"/classrooms/{code}").json()["lesson"] == {"id": "joins", "step": 2}


def test_the_access_methods_lab_runs_through_the_server(client):
    session = new_session(client)
    reply = client.post(f"/sessions/{session}/labs/access", json={"rows": 1000})
    assert reply.status_code == 200 and {q["key"] for q in reply.json()["queries"]} >= {"point", "range", "sum"}
    assert client.post(f"/sessions/{session}/labs/access", json={"rows": 10**6}).status_code == 422


def test_the_sharding_lab_runs_through_the_server(client):
    session = new_session(client)
    reply = client.post(f"/sessions/{session}/labs/sharding", json={"shards": 2, "rows": 1000})
    assert reply.status_code == 200 and len(reply.json()["balance"]["hash-id"]) == 2
    assert client.post(f"/sessions/{session}/labs/sharding", json={"shards": 50}).status_code == 422



def test_forwarding_ends_when_the_client_leaves_even_with_nothing_to_send():
    # The leak this guards against: waiting only on the queue, a socket whose client left while nothing was
    # being sent was never noticed, and held its connection slot until the session ended.
    class Gone:
        async def receive(self):
            return {"type": "websocket.disconnect"}

        async def send_json(self, message):
            raise AssertionError("nothing was queued")

        async def close(self, code):
            raise AssertionError("the queue was not closed")

    async def run():
        await asyncio.wait_for(_forward(Gone(), Subscriber("client"), lambda batch: batch), timeout=2)

    asyncio.run(run())

    class Listening(Gone):
        def __init__(self):
            self.sent = []

        async def receive(self):
            await asyncio.sleep(3600)

        async def send_json(self, message):
            self.sent.append(message)

        async def close(self, code):
            self.closed = code

    async def deliver():
        socket, subscriber = Listening(), Subscriber("client")
        subscriber.offer([{"n": 1}])
        subscriber.queue.put_nowait(None)  # the session ended after one batch
        await asyncio.wait_for(_forward(socket, subscriber, lambda batch: batch), timeout=2)
        return socket

    socket = asyncio.run(deliver())
    assert socket.sent == [{"n": 1}] and socket.closed == 4410


def test_lab_sizes_follow_the_servers_lab_scale(make_client):
    client = make_client(lab_scale=0.4, labs_per_minute=100)
    session = new_session(client)
    labs = client.post("/sessions").json()["limits"]["labs"]
    assert labs["sharding"] == {"shards": 4, "rows": 4600} and labs["scaling"] == {"steps": 3}
    refused = client.post(f"/sessions/{session}/labs/sharding", json={"shards": 8, "rows": 2000})
    assert refused.status_code == 422 and "limited to 4" in refused.json()["error"]["message"]
    ok = client.post(f"/sessions/{session}/labs/sharding", json={"shards": 4, "rows": 1000})
    assert ok.status_code == 200
    scaled = client.post(f"/sessions/{session}/labs/scaling", json={}).json()
    assert len(scaled["sizes"]) == 3  # fewer doubling steps on a small machine


def test_a_running_lab_never_takes_a_statement_slot(make_client):
    client = make_client(max_concurrent_queries=1, max_concurrent_labs=1, labs_per_minute=100)
    manager = client.app.state.manager
    session = new_session(client)
    assert manager.lab_slots is not manager.slots

    async def hold_lab_slot_and_query():
        await manager.lab_slots.acquire()  # a lab is running somewhere
        try:
            return await manager.sessions[session].request({"op": "state", "node_limit": 1})
        finally:
            manager.lab_slots.release()

    assert client.portal.call(hold_lab_slot_and_query)["ok"]


def test_the_page_carries_absolute_link_preview_images(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text('<meta property="og:image" content="/og.jpg" /><div id="root"></div>')
    settings = Settings(data_dir=str(tmp_path / "data"), web_dir=str(web), public_url="https://arcanum.example.org/")
    with TestClient(create_app(settings)) as client:
        page = client.get("/")
    assert page.status_code == 200 and 'content="https://arcanum.example.org/og.jpg"' in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]  # the page keeps its policy
