"""The HTTP/WebSocket API, with real worker processes behind it."""

import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from arcanum.server.app import Settings, create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path), query_timeout=3, max_sessions=2, max_rows=5, max_pages=300,
                        max_sql_bytes=2000)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def new_session(client):
    response = client.post("/sessions")
    assert response.status_code == 201
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

    results = sql(client, session, "INSERT INTO customers VALUES (90, 'New', 'Rome'); "
                                   "SELECT count(*) FROM customers").json()["results"]
    assert [r["status"] for r in results] == ["INSERT 1", "SELECT 1"] and results[1]["rows"] == [[25]]


def test_errors_are_structured(client):
    session = new_session(client)
    error = sql(client, session, "SELECT nope FROM customers").json()["error"]
    assert error == {"type": "SqlError", "message": "column nope does not exist", "line": 1, "column": 8}
    response = sql(client, session, "INSERT INTO customers VALUES (1, 'Dup', NULL)")
    assert response.status_code == 400 and response.json()["error"]["type"] == "ConstraintError"
    assert sql(client, session, "SELECT 1 " + " " * 3000).status_code == 413
    assert sql(client, "no-such-session", "SELECT 1").status_code == 404


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
    rows = sql(client, session, "SELECT count(*), count(DISTINCT batch) FROM crash_demo").json()["results"][0]["rows"]
    assert rows[0][0] == summary["rows_after_recovery"]
    assert sql(client, session, "SELECT count(*) FROM orders").json()["results"][0]["rows"] == [[80]]


def test_runaway_query_is_stopped_without_losing_data(client):
    session = new_session(client)
    sql(client, session, "INSERT INTO customers VALUES (77, 'Kept', 'Lima')")
    response = sql(client, session, "SELECT count(*) FROM orders a, orders b, orders c, orders d")
    assert response.status_code == 408 and response.json()["error"]["type"] == "QueryTimeout"
    assert sql(client, session, "SELECT name FROM customers WHERE id = 77").json()["results"][0]["rows"] == [["Kept"]]


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


def test_sessions_are_limited_and_deletable(client, tmp_path):
    first, second = new_session(client), new_session(client)
    response = client.post("/sessions")
    assert response.status_code == 503 and response.json()["error"]["type"] == "ServerFull"
    assert client.delete(f"/sessions/{first}").status_code == 204
    assert not os.path.exists(tmp_path / f"{first}.db")
    assert sql(client, first, "SELECT 1").status_code == 404
    new_session(client)
    assert sql(client, second, "SELECT 1").status_code == 200


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
    assert state["wal"]["committed_frames"] >= 0
