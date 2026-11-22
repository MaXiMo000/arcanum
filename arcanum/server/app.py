"""The HTTP and WebSocket API.

    POST   /sessions                 create a session with its own seeded database
    DELETE /sessions/{id}            close it and delete its files
    POST   /sessions/{id}/query      run SQL; returns rows plus the traced query plan
    POST   /sessions/{id}/import     load CSV into a new or existing table (or preview it)
    GET    /sessions/{id}/state      B+trees, buffer pool and log, for drawing the database
    POST   /sessions/{id}/crash      kill the engine (optionally mid-write) and recover
    POST   /sessions/{id}/labs/{lab} an experiment: "engines", "buffer-pool", "scaling", "access" or "sharding"
    POST   /sessions/{id}/cluster    the replication demo: a three-node Raft cluster, stepped and faulted
    POST   /classrooms               open a classroom: a code for students, a token for the teacher
    GET    /classrooms/{code}        its title, student count and recent posts
    POST   /classrooms/{code}/posts  the teacher posts a statement and its results (Bearer token)
    DELETE /classrooms/{code}        the teacher ends it (Bearer token)
    POST   /classrooms/{code}/lesson the teacher's lesson step, followed by students (Bearer token)
    POST   /classrooms/{code}/questions  a student asks a question; only the teacher reads them
    WS     /classrooms/{code}/live   students receive every post as it happens
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
import re
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .classroom import ClassroomManager, normalize
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
    lock_timeout: float = 60  # seconds a statement may wait for another transaction's lock
    queue_timeout: float = 5  # seconds a request may wait for a free statement slot
    max_concurrent_queries: int = field(default_factory=lambda: max(2, os.cpu_count() or 2))
    max_pages: int = 2500  # per-session database size, in 4 KB pages (10 MB)
    max_rows: int = 1000  # rows returned per result; the rest are counted, not sent
    max_sql_bytes: int = 20_000
    max_import_bytes: int = 512 * 1024  # CSV text per import
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
    labs_per_minute: int = 6  # engine comparisons per client
    lab_timeout: float = 60  # seconds an engine comparison may run
    max_websockets: int = 200
    max_websockets_per_client: int = 4
    max_websockets_per_session: int = 4
    max_classrooms: int = 50
    max_classrooms_per_client: int = 2
    max_students_per_classroom: int = 60
    classrooms_per_minute: int = 2  # classrooms a client may open
    questions_per_minute: int = 4  # questions a client may ask
    classroom_ttl: float = 7200  # seconds a classroom with nobody connected may sit unused
    allowed_origins: list = field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])
    enable_docs: bool = False
    hsts: bool = False  # send Strict-Transport-Security (only when served over HTTPS)

    @classmethod
    def from_env(cls):
        d = cls()
        int_fields = ["max_sessions", "max_sessions_per_client", "max_concurrent_queries", "max_pages", "max_rows",
                      "max_sql_bytes", "max_import_bytes", "memory_rows", "sort_memory_rows", "spill_bytes", "recursive_rows",
                      "worker_memory_mb", "max_keys", "pool_size", "event_limit", "rate_burst", "sessions_per_minute",
                      "crashes_per_minute", "labs_per_minute", "max_websockets", "max_websockets_per_client", "max_websockets_per_session",
                      "max_classrooms", "max_classrooms_per_client", "max_students_per_classroom",
                      "classrooms_per_minute", "questions_per_minute"]
        float_fields = ["session_ttl", "query_timeout", "lock_timeout", "queue_timeout", "rate_per_second",
                        "lab_timeout", "classroom_ttl"]
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
    session: Literal["A", "B"] = "A"  # which of the visitor's two database sessions runs it


class ImportRequest(BaseModel):
    """CSV text loaded into a table (engine/csvimport.py); dry_run only reads it and returns a preview."""
    model_config = ConfigDict(extra="forbid")
    table: str = Field(min_length=1, max_length=63, pattern=r"^[a-z_][a-z0-9_]*$")
    csv: str = Field(min_length=1)
    header: bool = True
    dry_run: bool = False
    session: Literal["A", "B"] = "A"


Cell = Annotated[str, Field(max_length=200)] | int | float | bool | None


class ResultSummary(BaseModel):
    """What students see of one statement's result: its status and its first rows."""
    model_config = ConfigDict(extra="forbid")
    status: str = Field("", max_length=100)
    columns: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=30)
    rows: list[Annotated[list[Cell], Field(max_length=30)]] = Field(default_factory=list, max_length=20)
    row_total: int = Field(0, ge=0)


class ClassroomOpen(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field("", max_length=80)


class ClassroomPost(BaseModel):
    """A statement the teacher ran, its results, or the error it failed with."""
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1, max_length=20_000)
    results: list[ResultSummary] = Field(default_factory=list, max_length=10)
    error: str | None = Field(None, max_length=500)


class ClassroomLesson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lesson: str | None = Field(None, pattern=r"^[a-z0-9-]{1,40}$")
    step: int = Field(0, ge=0, le=100)


class ClassroomQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=300)
    name: str = Field("", max_length=40)


class CrashRequest(BaseModel):
    during_write: bool = True


class EnginesLab(BaseModel):
    """B+tree versus LSM tree (labs/engines.py)."""
    model_config = ConfigDict(extra="forbid")
    keys: int = Field(5000, ge=500, le=10_000)
    order: Literal["sequential", "random"] = "random"
    updates: float = Field(0.5, ge=0, le=1)  # extra writes that overwrite existing keys, per key
    value_bytes: int = Field(64, ge=16, le=256)
    memory_pages: int = Field(16, ge=8, le=64)


class BufferPoolLab(BaseModel):
    """Buffer pool replacement policies on one page-access trace (labs/buffer_pool.py)."""
    model_config = ConfigDict(extra="forbid")
    workload: Literal["hot", "hot-scan", "loop", "index"] = "hot-scan"
    pool_pages: int = Field(32, ge=4, le=512)
    rows: int = Field(4000, ge=500, le=20_000)
    lookups: int = Field(1500, ge=100, le=5_000)


class AccessLab(BaseModel):
    """A B+tree, a hash index and a column store on one table (labs/access.py)."""
    model_config = ConfigDict(extra="forbid")
    rows: int = Field(5000, ge=1000, le=20_000)


class ShardingLab(BaseModel):
    """One table split across shards by hash or by range (labs/sharding.py)."""
    model_config = ConfigDict(extra="forbid")
    shards: int = Field(4, ge=2, le=8)
    rows: int = Field(4000, ge=1000, le=10_000)


class ScalingLab(BaseModel):
    """TPC-H-style queries at doubling data sizes (labs/scaling.py); nothing to choose."""
    model_config = ConfigDict(extra="forbid")


LABS = {"engines": EnginesLab, "buffer-pool": BufferPoolLab, "scaling": ScalingLab, "access": AccessLab,
        "sharding": ShardingLab}

NodeName = Literal["n1", "n2", "n3"]


class ClusterRequest(BaseModel):
    """One step of the replication demo (replication/raft.py)."""
    model_config = ConfigDict(extra="forbid")
    action: Literal["state", "tick", "write", "read", "partition", "heal", "crash", "restart", "reset"]
    ticks: int = Field(1, ge=1, le=60)
    sql: str | None = Field(None, min_length=1, max_length=2000)
    node: NodeName | None = None
    groups: list[list[NodeName]] | None = Field(None, min_length=1, max_length=3)

    @model_validator(mode="after")
    def _complete(self):
        needs = {"write": ("sql",), "read": ("sql", "node"), "partition": ("groups",), "crash": ("node",),
                 "restart": ("node",)}
        missing = [name for name in needs.get(self.action, ()) if getattr(self, name) is None]
        if missing:
            raise ValueError(f"{self.action} needs {', '.join(missing)}")
        if self.groups is not None and sorted(name for group in self.groups for name in group) != ["n1", "n2", "n3"]:
            raise ValueError("a partition puts each node in exactly one group")
        return self  # the worker runs each by the same name


TICKET = re.compile(r"^t[0-9]{1,9}$")


def _query_reply(reply):
    if not reply["ok"]:
        return JSONResponse({"error": reply["error"]}, status_code=400)
    body = {"results": reply["results"], "stats": reply["stats"]}
    if "waiting" in reply:
        return JSONResponse({**body, "waiting": reply["waiting"]}, status_code=202)
    return body


def _error(status, error_type, message, headers=None):
    return JSONResponse({"error": {"type": error_type, "message": message}}, status_code=status, headers=headers)


def _bearer(request):
    """The token of an "Authorization: Bearer <token>" header, or None."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return (token.strip() or None) if scheme.lower() == "bearer" else None


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
    max_import_body = settings.max_import_bytes * 2 + 4096  # JSON escaping can double the CSV text
    max_post_body = max_body + 256 * 1024  # a classroom post: the SQL plus the first rows of its results
    classrooms = ClassroomManager(settings)

    @asynccontextmanager
    async def lifespan(app):
        async def reap_forever():
            while True:
                await asyncio.sleep(60)
                await manager.reap()
                classrooms.reap()

        reaper = asyncio.create_task(reap_forever())
        try:
            yield
        finally:
            reaper.cancel()
            await manager.close_all()
            classrooms.close_all()

    docs = {} if settings.enable_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="Arcanum", description="A from-scratch database you can watch work.", lifespan=lifespan, **docs)
    app.state.manager = manager
    app.state.settings = settings

    @app.middleware("http")
    async def guard(request: Request, call_next):
        client = _client(request)
        if not limiter.allow(client, "request", settings.rate_per_second, settings.rate_burst):
            return _error(429, "RateLimited", "too many requests; slow down", {"Retry-After": "1"})
        origin = request.headers.get("origin")
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and origin is not None
                and origin not in settings.allowed_origins and not _same_origin(request, origin)):
            # CORS only hides the response; a foreign page could still send a simple POST (one with no
            # body opens a session). Browsers always send Origin on these, so refuse foreign ones outright.
            return _error(403, "ForeignOrigin", "requests from other sites are not accepted")
        if request.method in ("POST", "PUT", "PATCH"):
            length = request.headers.get("content-length")
            if length is None:
                return _error(411, "LengthRequired", "requests with a body must send Content-Length")
            path = request.url.path
            limit = (max_import_body if path.endswith("/import")
                     else max_post_body if path.startswith("/classrooms/") and path.endswith("/posts") else max_body)
            if not length.isdigit() or int(length) > limit:
                return _error(413, "RequestTooLarge", f"request bodies are limited to {limit} bytes")
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
              "max_pages": settings.max_pages, "max_sql_bytes": settings.max_sql_bytes,
              "max_import_bytes": settings.max_import_bytes}

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
        reply = await session.request({"op": "query", "sql": body.sql, "session": body.session,
                                       "max_rows": settings.max_rows})
        return _query_reply(reply)

    @app.post("/sessions/{session_id}/import")
    async def import_csv(session_id: str, body: ImportRequest):
        session = manager.get(session_id)
        if len(body.csv.encode()) > settings.max_import_bytes:
            return _error(413, "CsvTooLarge", f"CSV imports are limited to {settings.max_import_bytes // 1024} KB")
        reply = await session.request({"op": "import", **body.model_dump(), "max_rows": settings.max_rows})
        if reply.get("ok") and "preview" in reply:
            return reply["preview"]
        return _query_reply(reply)

    # A statement waiting for another transaction's lock answers 202 with a ticket; fetch the ticket
    # until it finishes (200 with the results, or 400 with the error), or cancel it.
    @app.get("/sessions/{session_id}/tickets/{ticket}")
    async def ticket(session_id: str, ticket: str):
        session = manager.get(session_id)
        if not TICKET.match(ticket):
            return _error(404, "UnknownTicket", "no such ticket")
        return _query_reply(await session.request({"op": "ticket", "ticket": ticket}))

    @app.post("/sessions/{session_id}/tickets/{ticket}/cancel")
    async def cancel(session_id: str, ticket: str):
        session = manager.get(session_id)
        if not TICKET.match(ticket):
            return _error(404, "UnknownTicket", "no such ticket")
        return _query_reply(await session.request({"op": "cancel", "ticket": ticket}))

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

    @app.post("/sessions/{session_id}/cluster")
    async def cluster(session_id: str, body: ClusterRequest):
        reply = await manager.get(session_id).request({"op": "cluster", **body.model_dump()})
        status = 200 if reply.pop("ok") else 400
        return JSONResponse(reply, status_code=status)

    # Classrooms. The teacher's token travels as "Authorization: Bearer <token>".
    @app.post("/classrooms", status_code=201)
    async def open_classroom(request: Request, body: ClassroomOpen | None = None):
        client = _client(request)
        if not limiter.allow(client, "classroom", settings.classrooms_per_minute / 60, settings.classrooms_per_minute):
            return _error(429, "RateLimited", "classrooms are limited; wait a minute", {"Retry-After": "30"})
        room, token = classrooms.create(client, (body or ClassroomOpen()).title.strip())
        return {"code": room.code, "token": token, "title": room.title}

    @app.get("/classrooms/{code}")
    async def classroom(code: str, request: Request):
        room = classrooms.get(code)
        return room.summary(teacher=room.is_teacher(_bearer(request)))

    @app.post("/classrooms/{code}/lesson")
    async def classroom_lesson(code: str, request: Request, body: ClassroomLesson):
        room = classrooms.teacher(code, _bearer(request))
        room.follow({"id": body.lesson, "step": body.step} if body.lesson else None)
        return {"students": len(room.students)}

    @app.post("/classrooms/{code}/questions", status_code=201)
    async def ask_classroom(code: str, request: Request, body: ClassroomQuestion):
        room = classrooms.get(code)
        if not limiter.allow(_client(request), "question", settings.questions_per_minute / 60, settings.questions_per_minute):
            return _error(429, "RateLimited", "questions are limited; wait a little", {"Retry-After": "15"})
        if not body.text.strip():
            return _error(422, "InvalidRequest", "invalid request: check text")
        question = room.ask(body.text.strip(), body.name.strip())
        return {"id": question["id"]}

    @app.delete("/classrooms/{code}/questions/{question_id}", status_code=204)
    async def dismiss_question(code: str, question_id: int, request: Request):
        classrooms.teacher(code, _bearer(request)).dismiss(question_id)
        return Response(status_code=204)

    @app.post("/classrooms/{code}/posts")
    async def post_to_classroom(code: str, request: Request, body: ClassroomPost):
        room = classrooms.teacher(code, _bearer(request))
        if len(body.sql.encode()) > settings.max_sql_bytes:
            return _error(413, "SqlTooLong", f"SQL is limited to {settings.max_sql_bytes} bytes")
        item = room.post(body.model_dump())
        return {"seq": item["seq"], "students": len(room.students)}

    @app.delete("/classrooms/{code}", status_code=204)
    async def end_classroom(code: str, request: Request):
        classrooms.end(code, _bearer(request))
        return Response(status_code=204)

    @app.websocket("/classrooms/{code}/live")
    async def classroom_live(websocket: WebSocket, code: str):
        client = _client(websocket.scope)
        origin = websocket.headers.get("origin")
        room = classrooms.rooms.get(normalize(code))
        if origin is not None and origin not in settings.allowed_origins and not _same_origin(websocket, origin):
            await websocket.close(code=4403)
            return
        if room is None:
            await websocket.close(code=4404)
            return
        if (not limiter.allow(client, "request", settings.rate_per_second, settings.rate_burst)
                or sockets["total"] >= settings.max_websockets
                or sockets["by_client"].get(client, 0) >= settings.max_websockets_per_client
                or len(room.students) >= settings.max_students_per_classroom):
            await websocket.close(code=4429)
            return
        await websocket.accept()
        student = Subscriber(client)
        room.students.add(student)
        room.last_used = time.monotonic()
        sockets["total"] += 1
        sockets["by_client"][client] = sockets["by_client"].get(client, 0) + 1
        try:
            await websocket.send_json({"type": "joined", **room.summary()})
            while True:
                batch = await student.queue.get()
                if batch is None:
                    await websocket.close(code=4410)  # the teacher ended the classroom
                    return
                for message in batch:
                    await websocket.send_json(message)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            room.students.discard(student)
            sockets["total"] -= 1
            sockets["by_client"][client] -= 1
            if not sockets["by_client"][client]:
                del sockets["by_client"][client]

    @app.post("/sessions/{session_id}/labs/{lab}")
    async def run_lab(session_id: str, lab: str, request: Request):
        session = manager.get(session_id)
        model = LABS.get(lab)
        if model is None:
            return _error(404, "UnknownLab", f"no such lab; choose one of {', '.join(LABS)}")
        try:
            body = await request.body()
            params = model.model_validate_json(body or b"{}")
        except ValidationError as e:
            fields = sorted({".".join(str(p) for p in err.get("loc", ())) or "body" for err in e.errors()})
            return _error(422, "InvalidRequest", f"invalid request: check {', '.join(fields)}")
        if not limiter.allow(_client(request), "lab", settings.labs_per_minute / 60, max(1, settings.labs_per_minute // 2)):
            return _error(429, "RateLimited", "labs are limited; wait a few seconds", {"Retry-After": "10"})
        reply = await session.request({"op": "lab", "lab": lab, "params": params.model_dump()},
                                      timeout=settings.lab_timeout)
        if not reply.pop("ok"):
            return JSONResponse({"error": reply["error"]}, status_code=400)
        return reply

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


def _same_origin(connection, origin):
    host = connection.headers.get("host")
    return host is not None and origin in (f"http://{host}", f"https://{host}")
