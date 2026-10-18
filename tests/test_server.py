"""The HTTP/WebSocket API, with real worker processes behind it."""

import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from arcanum.server.app import Settings, create_app
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
    assert {"page_read", "node_visit", "node_split", "wal_append", "wal_sync", "commit"} <= seen


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


def test_runaway_statement_is_stopped_without_losing_data(client):
    session = new_session(client)
    sql(client, session, "INSERT INTO customers VALUES (77, 'Kept', 'Lima')")
    response = sql(client, session, "SELECT count(*) FROM orders a, orders b, orders c, orders d")
    assert response.status_code == 408 and response.json()["error"]["type"] == "QueryTimeout"
    assert sql(client, session, "SELECT name FROM customers WHERE id = 77").json()["results"][0]["rows"] == [["Kept"]]


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
