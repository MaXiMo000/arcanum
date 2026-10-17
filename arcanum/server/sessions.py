"""Sessions: one database file and one engine worker process per visitor.

Requests to a worker run one at a time (a per-session lock) in a thread, so
the event loop never blocks, and at most `max_concurrent_queries` run across
all sessions at once. A request that outlives the statement timeout gets its
worker killed and restarted; the database recovers from its log like after
any crash, so committed data survives.
"""

import asyncio
import json
import multiprocessing
import os
import random
import re
import secrets
import threading
import time
from pathlib import Path

from .worker import MAX_MESSAGE_BYTES, receive, send, worker_main

STARTUP_TIMEOUT = 60
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{24}$")  # secrets.token_urlsafe(18)
SESSION_FILE = re.compile(r"^[A-Za-z0-9_-]{24}\.db(-wal)?$")


class SessionError(Exception):
    def __init__(self, status, error_type, message):
        super().__init__(message)
        self.status = status
        self.error = {"type": error_type, "message": message}


class Subscriber:
    """One WebSocket listener: a bounded queue of event batches."""

    def __init__(self, client):
        self.client = client
        self.queue = asyncio.Queue(maxsize=500)
        self.dropped = 0

    def offer(self, batch):
        try:
            self.queue.put_nowait(batch)
        except asyncio.QueueFull:
            self.dropped += len(batch)  # a slow client loses events rather than slowing the engine

    def close(self):
        """Tell the listener its session has ended (None in the queue ends its loop)."""
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)


class Session:
    def __init__(self, session_id, path, settings, loop, slots, owner):
        self.id = session_id
        self.path = path
        self.settings = settings
        self.loop = loop
        self.slots = slots  # shared limit on statements running at once
        self.owner = owner  # client address that created the session
        self.lock = asyncio.Lock()
        self.subscribers = set()
        self.last_used = time.monotonic()
        self.generation = 0
        self.recovered_frames = 0
        self.process = None

    # ---- worker lifecycle (called from threads) ------------------------------------------

    def start(self):
        context = multiprocessing.get_context("spawn")
        conn, child_conn = context.Pipe()
        event_reader, event_writer = context.Pipe(duplex=False)
        s = self.settings
        options = {"max_keys": s.max_keys, "max_pages": s.max_pages, "pool_size": s.pool_size,
                   "event_limit": s.event_limit, "memory_rows": s.memory_rows, "spill_bytes": s.spill_bytes,
                   "sort_memory_rows": s.sort_memory_rows, "recursive_rows": s.recursive_rows,
                   "memory_mb": s.worker_memory_mb}
        self.process = context.Process(target=worker_main, args=(str(self.path), child_conn, event_writer, options),
                                       daemon=True)
        self.process.start()
        child_conn.close()
        event_writer.close()  # the pump sees end-of-file once the worker exits
        self.conn = conn
        self.generation += 1
        threading.Thread(target=self._pump, args=(event_reader, self.generation), daemon=True).start()
        if not conn.poll(STARTUP_TIMEOUT):
            self.kill()
            raise SessionError(500, "StartupError", "the database worker did not start")
        ready = receive(conn)
        if not ready.get("ready"):
            self.kill()
            raise SessionError(500, "StartupError", "the database could not be opened")
        self.recovered_frames = ready["recovered_frames"]

    def kill(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.kill()
            self.process.join(10)

    def call(self, message, timeout):
        """Send one request and wait for its reply; restart the worker if it runs too long or dies."""
        try:
            send(self.conn, message)
            if self.conn.poll(timeout):
                return receive(self.conn)
        except (EOFError, OSError, ValueError):
            self.restart("worker_lost")
            raise SessionError(500, "WorkerLost", "the database worker stopped unexpectedly and was restarted")
        self.restart("timeout")
        raise SessionError(408, "QueryTimeout",
                           f"the statement ran longer than {timeout:g} seconds and was stopped; "
                           "committed data is unaffected")

    def restart(self, reason):
        self.kill()
        self.broadcast([{"kind": "worker_restart", "reason": reason}])
        self.start()

    def _pump(self, reader, generation):
        while True:
            try:
                batch = json.loads(reader.recv_bytes(MAX_MESSAGE_BYTES))
            except (EOFError, OSError, ValueError):
                reader.close()
                return
            if not isinstance(batch, list):
                continue
            for event in batch:
                event["generation"] = generation
            self.loop.call_soon_threadsafe(self._deliver, batch)

    def broadcast(self, batch):
        """Send server-side events (crash, restart) to listeners; safe from any thread."""
        self.loop.call_soon_threadsafe(self._deliver, [dict(e, generation=self.generation) for e in batch])

    def _deliver(self, batch):
        for subscriber in list(self.subscribers):
            subscriber.offer(batch)

    # ---- requests (called from the event loop) ----------------------------------------------

    async def _slot(self):
        try:
            await asyncio.wait_for(self.slots.acquire(), self.settings.queue_timeout)
        except asyncio.TimeoutError:
            raise SessionError(503, "ServerBusy", "the server is busy; try again shortly") from None

    async def request(self, message, timeout=None):
        async with self.lock:
            await self._slot()
            try:
                self.last_used = time.monotonic()
                return await asyncio.to_thread(self.call, message, timeout or self.settings.query_timeout)
            finally:
                self.slots.release()
                self.last_used = time.monotonic()

    async def crash(self, during_write):
        async with self.lock:
            await self._slot()
            try:
                self.last_used = time.monotonic()
                return await asyncio.to_thread(self._crash, during_write)
            finally:
                self.slots.release()

    def _crash(self, during_write):
        rows_per_batch, committed = 25, None
        if during_write:
            # Let a few transactions commit, then kill the worker while it is inside the next one.
            target = random.randint(2, 6)
            send(self.conn, {"op": "workload", "batches": 1000, "rows_per_batch": rows_per_batch})
            while committed is None or committed["committed_batches"] < target:
                if not self.conn.poll(self.settings.query_timeout):
                    break
                message = receive(self.conn)
                if not message.get("progress"):
                    break  # the workload stopped (for example, the database is full)
                committed = message
            time.sleep(random.uniform(0, 0.01))
        self.broadcast([{"kind": "crash", "during_write": during_write}])
        self.kill()
        self.start()  # opening the database replays the log
        summary = {"recovered_frames": self.recovered_frames, "during_write": during_write}
        if during_write and committed is not None:
            reply = self.call({"op": "query", "sql": "SELECT count(*) FROM crash_demo", "max_rows": 1},
                              self.settings.query_timeout)
            rows = reply["results"][0]["rows"][0][0]
            # Every acknowledged commit must be back. The transaction that was running when the
            # plug was pulled is all there (it reached its commit frame) or not there at all.
            summary.update(acknowledged_rows=committed["rows"], rows_after_recovery=rows,
                           in_flight_rows=rows_per_batch, in_flight_committed=rows > committed["rows"],
                           consistent=rows in (committed["rows"], committed["rows"] + rows_per_batch))
        self.broadcast([{"kind": "recovered", **summary}])
        return summary

    async def close(self):
        async with self.lock:
            await asyncio.to_thread(self._close)

    def _close(self):
        if self.process is not None and self.process.is_alive():
            try:
                send(self.conn, {"op": "close"})  # a clean close checkpoints and removes the log
                if self.conn.poll(10):
                    receive(self.conn)
            except (EOFError, OSError, ValueError):
                pass
        self.kill()


class SessionManager:
    def __init__(self, settings):
        self.settings = settings
        self.sessions = {}
        self.slots = None  # created inside the running event loop
        data_dir = Path(settings.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(data_dir, 0o700)  # session databases are readable by this user only
        for entry in data_dir.iterdir():  # files left by a previous run belong to sessions that no longer exist
            if entry.is_file() and SESSION_FILE.match(entry.name):
                entry.unlink()

    async def create(self, owner):
        if self.slots is None:
            self.slots = asyncio.Semaphore(self.settings.max_concurrent_queries)
        owned = sum(1 for s in self.sessions.values() if s.owner == owner)
        if owned >= self.settings.max_sessions_per_client:
            raise SessionError(429, "TooManySessions",
                               f"each client may hold {self.settings.max_sessions_per_client} sessions; "
                               "delete one first")
        if len(self.sessions) >= self.settings.max_sessions:
            await self.reap(force_oldest=True)
        if len(self.sessions) >= self.settings.max_sessions:
            raise SessionError(503, "ServerFull", "too many active sessions; try again in a few minutes")
        session_id = secrets.token_urlsafe(18)
        session = Session(session_id, Path(self.settings.data_dir) / f"{session_id}.db", self.settings,
                          asyncio.get_running_loop(), self.slots, owner)
        self.sessions[session_id] = session  # reserve the slot while the worker starts
        try:
            await asyncio.to_thread(session.start)
        except BaseException:
            del self.sessions[session_id]
            raise
        return session

    def get(self, session_id):
        session = self.sessions.get(session_id) if SESSION_ID.match(session_id) else None
        if session is None:
            raise SessionError(404, "UnknownSession", "no such session; create one with POST /sessions")
        return session

    async def delete(self, session_id):
        session = self.get(session_id)
        del self.sessions[session_id]
        for subscriber in list(session.subscribers):
            subscriber.close()
        await session.close()
        for suffix in ("", "-wal"):
            try:
                os.remove(f"{session.path}{suffix}")
            except FileNotFoundError:
                pass

    async def reap(self, force_oldest=False):
        """Close sessions idle longer than the TTL; optionally also the least recently used one."""
        now = time.monotonic()
        idle = [s for s in self.sessions.values() if now - s.last_used > self.settings.session_ttl]
        if force_oldest and not idle and self.sessions:
            oldest = min(self.sessions.values(), key=lambda s: s.last_used)
            if now - oldest.last_used > self.settings.session_ttl / 10:
                idle = [oldest]
        for session in idle:
            if not session.lock.locked():
                await self.delete(session.id)

    async def close_all(self):
        for session_id in list(self.sessions):
            await self.delete(session_id)
