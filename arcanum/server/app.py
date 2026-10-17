"""The HTTP and WebSocket API.

    POST   /sessions                 create a session with its own seeded database
    DELETE /sessions/{id}            close it and delete its files
    POST   /sessions/{id}/query      run SQL; returns rows plus the traced query plan
    GET    /sessions/{id}/state      B+trees, buffer pool and log, for drawing the database
    POST   /sessions/{id}/crash      kill the engine (optionally mid-write) and recover
    WS     /sessions/{id}/events     live engine events: page reads, splits, log writes, recovery
    GET    /health

Errors are JSON: {"error": {"type", "message", "line"?, "column"?}}.
"""

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .sessions import SessionError, SessionManager, Subscriber


def _env(name, default, convert=str):
    value = os.environ.get(name)
    return default if value in (None, "") else convert(value)


@dataclass
class Settings:
    data_dir: str = field(default_factory=lambda: os.path.join(tempfile.gettempdir(), "arcanum-sessions"))
    max_sessions: int = 20
    session_ttl: float = 1800  # seconds a session may sit idle before it is closed
    query_timeout: float = 10  # seconds before a statement's worker is stopped
    max_pages: int = 2500  # per-session database size, in 4 KB pages (10 MB)
    max_rows: int = 1000  # rows returned per result; the rest are counted, not sent
    max_sql_bytes: int = 20_000
    max_keys: int = 4  # keys per B+tree node: small, so splits are easy to see
    pool_size: int = 64
    event_limit: int = 20_000  # events streamed per request
    allowed_origins: list = field(default_factory=lambda: ["http://localhost:5173"])

    @classmethod
    def from_env(cls):
        defaults = cls()
        return cls(
            data_dir=_env("ARCANUM_DATA_DIR", defaults.data_dir),
            max_sessions=_env("ARCANUM_MAX_SESSIONS", defaults.max_sessions, int),
            session_ttl=_env("ARCANUM_SESSION_TTL", defaults.session_ttl, float),
            query_timeout=_env("ARCANUM_QUERY_TIMEOUT", defaults.query_timeout, float),
            max_pages=_env("ARCANUM_MAX_PAGES", defaults.max_pages, int),
            max_rows=_env("ARCANUM_MAX_ROWS", defaults.max_rows, int),
            max_sql_bytes=_env("ARCANUM_MAX_SQL_BYTES", defaults.max_sql_bytes, int),
            max_keys=_env("ARCANUM_MAX_KEYS", defaults.max_keys, int),
            pool_size=_env("ARCANUM_POOL_SIZE", defaults.pool_size, int),
            event_limit=_env("ARCANUM_EVENT_LIMIT", defaults.event_limit, int),
            allowed_origins=[o.strip() for o in _env("ARCANUM_ALLOWED_ORIGINS", ",".join(defaults.allowed_origins)).split(",")
                             if o.strip()],
        )


class QueryRequest(BaseModel):
    sql: str


class CrashRequest(BaseModel):
    during_write: bool = True


def _error(status, error_type, message, **extra):
    return JSONResponse({"error": {"type": error_type, "message": message, **extra}}, status_code=status)


def create_app(settings=None):
    settings = settings or Settings.from_env()
    manager = SessionManager(settings)

    @asynccontextmanager
    async def lifespan(app):
        async def reap_forever():
            while True:
                await asyncio.sleep(60)
                await manager.reap()

        reaper = asyncio.create_task(reap_forever())
        try:
            yield
        finally:
            reaper.cancel()
            await manager.close_all()

    app = FastAPI(title="Arcanum", description="A from-scratch database you can watch work.", lifespan=lifespan)
    app.state.manager = manager
    app.state.settings = settings
    app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["Content-Type"])

    @app.exception_handler(SessionError)
    async def session_error(request: Request, e: SessionError):
        return JSONResponse({"error": e.error}, status_code=e.status)

    @app.get("/health")
    async def health():
        return {"status": "ok", "sessions": len(manager.sessions)}

    @app.post("/sessions", status_code=201)
    async def create_session():
        session = await manager.create()
        state = await session.request({"op": "state", "node_limit": 1})
        return {"id": session.id, "recovered_frames": session.recovered_frames, "tables": state["tables"],
                "limits": {"query_timeout": settings.query_timeout, "max_rows": settings.max_rows,
                           "max_pages": settings.max_pages, "max_sql_bytes": settings.max_sql_bytes}}

    @app.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str):
        await manager.delete(session_id)
        return Response(status_code=204)

    @app.post("/sessions/{session_id}/query")
    async def query(session_id: str, body: QueryRequest):
        session = manager.get(session_id)
        if len(body.sql.encode()) > settings.max_sql_bytes:
            return _error(413, "SqlTooLong", f"SQL is limited to {settings.max_sql_bytes} bytes")
        reply = await session.request({"op": "query", "sql": body.sql, "max_rows": settings.max_rows})
        if not reply["ok"]:
            return JSONResponse({"error": reply["error"]}, status_code=400)
        return {"results": reply["results"]}

    @app.get("/sessions/{session_id}/state")
    async def state(session_id: str):
        reply = await manager.get(session_id).request({"op": "state"})
        reply.pop("ok")
        return reply

    @app.post("/sessions/{session_id}/crash")
    async def crash(session_id: str, body: CrashRequest = CrashRequest()):
        return await manager.get(session_id).crash(body.during_write)

    @app.websocket("/sessions/{session_id}/events")
    async def events(websocket: WebSocket, session_id: str):
        session = manager.sessions.get(session_id)
        if session is None:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        subscriber = Subscriber()
        session.subscribers.add(subscriber)
        try:
            await websocket.send_json({"type": "subscribed", "generation": session.generation})
            while True:
                batch = await subscriber.queue.get()
                message = {"type": "events", "events": batch}
                if subscriber.dropped:
                    message["dropped"], subscriber.dropped = subscriber.dropped, 0
                await websocket.send_json(message)
        except WebSocketDisconnect:
            pass
        finally:
            session.subscribers.discard(subscriber)

    return app
