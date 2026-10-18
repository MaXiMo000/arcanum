"""The HTTP and WebSocket API.

    POST   /sessions                 create a session with its own seeded database
    DELETE /sessions/{id}            close it and delete its files
    POST   /sessions/{id}/query      run SQL; returns rows plus the traced query plan
    GET    /sessions/{id}/state      B+trees, buffer pool and log, for drawing the database
    POST   /sessions/{id}/crash      kill the engine (optionally mid-write) and recover
    WS     /sessions/{id}/events     live engine events: page reads, splits, log writes, recovery
    GET    /health

Errors are JSON: {"error": {"type", "message", "line"?, "column"?}}. If a
built web UI is present it is served from /.

Abuse protection lives here and in sessions.py: per-client rate limits and
session caps, request size limits, a cap on concurrent statements, WebSocket
caps with an Origin check, and security headers. Volumetric floods must be
stopped before they reach the app (a reverse proxy or CDN); see SECURITY.md.
"""

import asyncio
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .sessions import SessionError, SessionManager, Subscriber

log = logging.getLogger("arcanum.server")
DEFAULT_WEB_DIR = Path(__file__).resolve().parents[2] / "web" / "dist"


def _env(name, default, convert=str):
    value = os.environ.get(name)
    return default if value in (None, "") else convert(value)


def _flag(value):
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: str = field(default_factory=lambda: os.path.join(tempfile.gettempdir(), "arcanum-sessions"))
    web_dir: str = str(DEFAULT_WEB_DIR)
    max_sessions: int = 20
    max_sessions_per_client: int = 2
    session_ttl: float = 1800  # seconds a session may sit idle before it is closed
    query_timeout: float = 10  # seconds before a statement's worker is stopped
    queue_timeout: float = 5  # seconds a request may wait for a free statement slot
    max_concurrent_queries: int = field(default_factory=lambda: max(2, os.cpu_count() or 2))
    max_pages: int = 2500  # per-session database size, in 4 KB pages (10 MB)
    max_rows: int = 1000  # rows returned per result; the rest are counted, not sent
    max_sql_bytes: int = 20_000
    memory_rows: int = 200_000  # rows a statement may hold in memory at once
    sort_memory_rows: int = 10_000
    spill_bytes: int = 64 * 1024 * 1024  # temporary sort space per statement
    recursive_rows: int = 100_000
    worker_memory_mb: int = 768  # address-space cap per worker, where the OS supports it
    max_keys: int = 4  # keys per B+tree node: small, so splits are easy to see
    pool_size: int = 64
    event_limit: int = 20_000  # events streamed per request
    rate_per_second: float = 10  # sustained requests per client
    rate_burst: int = 40
    sessions_per_minute: int = 4  # new sessions per client
    crashes_per_minute: int = 6  # crash demos per client
    max_websockets: int = 200
    max_websockets_per_client: int = 4
    max_websockets_per_session: int = 4
    allowed_origins: list = field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])
    enable_docs: bool = False
    hsts: bool = False  # send Strict-Transport-Security (only when served over HTTPS)

    @classmethod
    def from_env(cls):
        d = cls()
        int_fields = ["max_sessions", "max_sessions_per_client", "max_concurrent_queries", "max_pages", "max_rows",
                      "max_sql_bytes", "memory_rows", "sort_memory_rows", "spill_bytes", "recursive_rows",
                      "worker_memory_mb", "max_keys", "pool_size", "event_limit", "rate_burst", "sessions_per_minute",
                      "crashes_per_minute", "max_websockets", "max_websockets_per_client", "max_websockets_per_session"]
        float_fields = ["session_ttl", "query_timeout", "queue_timeout", "rate_per_second"]
        values = {name: _env(f"ARCANUM_{name.upper()}", getattr(d, name), int) for name in int_fields}
        values |= {name: _env(f"ARCANUM_{name.upper()}", getattr(d, name), float) for name in float_fields}
        origins = _env("ARCANUM_ALLOWED_ORIGINS", ",".join(d.allowed_origins))
        public_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")  # set by Render for every service
        if public_url:
            origins = f"{origins},{public_url}"
        return cls(data_dir=_env("ARCANUM_DATA_DIR", d.data_dir), web_dir=_env("ARCANUM_WEB_DIR", d.web_dir),
                   allowed_origins=[o.strip() for o in origins.split(",") if o.strip()],
                   enable_docs=_flag(_env("ARCANUM_ENABLE_DOCS", "0")), hsts=_flag(_env("ARCANUM_HSTS", "0")),
                   **values)


class RateLimiter:
    """Token buckets per (client, bucket). Memory stays bounded: idle buckets are dropped."""

    def __init__(self):
        self.buckets = {}  # (client, name) -> [tokens, last refill time]

    def allow(self, client, name, rate, burst):
        now = time.monotonic()
        tokens, last = self.buckets.get((client, name), (burst, now))
        tokens = min(burst, tokens + (now - last) * rate)
        if tokens < 1:
            self.buckets[(client, name)] = [tokens, now]
            return False
        self.buckets[(client, name)] = [tokens - 1, now]
        if len(self.buckets) > 50_000:
            self.buckets = {k: v for k, v in self.buckets.items() if now - v[1] < 120}
        return True


class QueryRequest(BaseModel):
    sql: str


class CrashRequest(BaseModel):
    during_write: bool = True


def _error(status, error_type, message, headers=None):
    return JSONResponse({"error": {"type": error_type, "message": message}}, status_code=status, headers=headers)


def _client(scope_or_request):
    client = scope_or_request.client if hasattr(scope_or_request, "client") else scope_or_request.get("client")
    return client[0] if client else "unknown"


API_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def create_app(settings=None):
    settings = settings or Settings.from_env()
    manager = SessionManager(settings)
    limiter = RateLimiter()
    sockets = {"total": 0, "by_client": {}}
    max_body = settings.max_sql_bytes + 4096

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

    docs = {} if settings.enable_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="Arcanum", description="A from-scratch database you can watch work.", lifespan=lifespan, **docs)
    app.state.manager = manager
    app.state.settings = settings

    @app.middleware("http")
    async def guard(request: Request, call_next):
        client = _client(request)
        if not limiter.allow(client, "request", settings.rate_per_second, settings.rate_burst):
            return _error(429, "RateLimited", "too many requests; slow down", {"Retry-After": "1"})
        if request.method in ("POST", "PUT", "PATCH"):
            length = request.headers.get("content-length")
            if length is None:
                return _error(411, "LengthRequired", "requests with a body must send Content-Length")
            if not length.isdigit() or int(length) > max_body:
                return _error(413, "RequestTooLarge", f"request bodies are limited to {max_body} bytes")
        response = await call_next(request)
        response.headers.update(API_HEADERS)
        if settings.hsts:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Content-Security-Policy"] = PAGE_CSP
            response.headers["Cache-Control"] = "no-cache"
        elif request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"  # file names carry a hash
        else:
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["Content-Type"], allow_credentials=False, max_age=600)

    @app.exception_handler(SessionError)
    async def session_error(request: Request, e: SessionError):
        return JSONResponse({"error": e.error}, status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, e: RequestValidationError):
        # The default response echoes the submitted input back; this one does not.
        fields = sorted({".".join(str(p) for p in err.get("loc", ())[1:]) or "body" for err in e.errors()})
        return _error(422, "InvalidRequest", f"invalid request: check {', '.join(fields)}")

    @app.exception_handler(Exception)
    async def internal_error(request: Request, e: Exception):
        log.exception("unhandled error")
        return _error(500, "InternalError", "internal error")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    limits = {"query_timeout": settings.query_timeout, "max_rows": settings.max_rows,
              "max_pages": settings.max_pages, "max_sql_bytes": settings.max_sql_bytes}

    @app.post("/sessions", status_code=201)
    async def create_session(request: Request):
        client = _client(request)
        if not limiter.allow(client, "session", settings.sessions_per_minute / 60, settings.sessions_per_minute):
            return _error(429, "RateLimited", "too many new sessions; wait a minute", {"Retry-After": "15"})
        session = await manager.create(client)
        state = await session.request({"op": "state", "node_limit": 1})
        return {"id": session.id, "recovered_frames": session.recovered_frames, "tables": state["tables"],
                "limits": limits}

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
        return {"results": reply["results"], "stats": reply["stats"]}

    @app.get("/sessions/{session_id}/state")
    async def state(session_id: str):
        reply = await manager.get(session_id).request({"op": "state"})
        reply.pop("ok")
        return {**reply, "limits": limits}

    @app.post("/sessions/{session_id}/crash")
    async def crash(session_id: str, request: Request, body: CrashRequest | None = None):
        session = manager.get(session_id)
        burst = max(1, settings.crashes_per_minute // 2)
        if not limiter.allow(_client(request), "crash", settings.crashes_per_minute / 60, burst):
            return _error(429, "RateLimited", "the crash demo is limited; wait a few seconds", {"Retry-After": "10"})
        return await session.crash(body.during_write if body is not None else True)

    @app.websocket("/sessions/{session_id}/events")
    async def events(websocket: WebSocket, session_id: str):
        client = _client(websocket.scope)
        origin = websocket.headers.get("origin")
        session = manager.sessions.get(session_id)
        if origin is not None and origin not in settings.allowed_origins and not _same_origin(websocket, origin):
            await websocket.close(code=4403)  # browsers always send Origin; refuse foreign pages
            return
        if session is None:
            await websocket.close(code=4404)
            return
        if (not limiter.allow(client, "request", settings.rate_per_second, settings.rate_burst)
                or sockets["total"] >= settings.max_websockets
                or sockets["by_client"].get(client, 0) >= settings.max_websockets_per_client
                or len(session.subscribers) >= settings.max_websockets_per_session):
            await websocket.close(code=4429)
            return
        await websocket.accept()
        subscriber = Subscriber(client)
        session.subscribers.add(subscriber)
        sockets["total"] += 1
        sockets["by_client"][client] = sockets["by_client"].get(client, 0) + 1
        try:
            await websocket.send_json({"type": "subscribed", "generation": session.generation})
            while True:
                batch = await subscriber.queue.get()
                if batch is None:
                    await websocket.close(code=4410)  # the session ended
                    return
                message = {"type": "events", "events": batch}
                if subscriber.dropped:
                    message["dropped"], subscriber.dropped = subscriber.dropped, 0
                await websocket.send_json(message)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            session.subscribers.discard(subscriber)
            sockets["total"] -= 1
            sockets["by_client"][client] -= 1
            if not sockets["by_client"][client]:
                del sockets["by_client"][client]

    web_dir = Path(settings.web_dir)
    if (web_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


def _same_origin(websocket, origin):
    host = websocket.headers.get("host")
    return host is not None and origin in (f"http://{host}", f"https://{host}")
