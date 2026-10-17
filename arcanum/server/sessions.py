"""Sessions: one database file and one engine worker process per visitor.

Requests to a worker run one at a time (a per-session lock) in a thread, so
the event loop never blocks. A request that outlives the query timeout gets
its worker killed and restarted; the database recovers from its log like
after any crash, so committed data survives.
"""

import asyncio
import multiprocessing
import os
import random
import secrets
import threading
import time
from pathlib import Path

from .worker import worker_main

STARTUP_TIMEOUT = 60


class SessionError(Exception):
    def __init__(self, status, error_type, message):
        super().__init__(message)
        self.status = status
        self.error = {"type": error_type, "message": message}


class Subscriber:
    """One WebSocket listener: a bounded queue of event batches."""

    def __init__(self):
        self.queue = asyncio.Queue(maxsize=500)
        self.dropped = 0

    def offer(self, batch):
        try:
            self.queue.put_nowait(batch)
        except asyncio.QueueFull:
            self.dropped += len(batch)  # a slow client loses events rather than slowing the engine


class Session:
    def __init__(self, session_id, path, settings, loop):
        self.id = session_id
        self.path = path
        self.settings = settings
        self.loop = loop
        self.lock = asyncio.Lock()
        self.subscribers = set()
        self.last_used = time.monotonic()
        self.generation = 0
        self.recovered_frames = 0
        self.process = None

    # ---- worker lifecycle (called from threads) ------------------------------------------

    def start(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        events = context.Queue()
        options = {"max_keys": self.settings.max_keys, "max_pages": self.settings.max_pages,
                   "pool_size": self.settings.pool_size, "event_limit": self.settings.event_limit}
        self.process = context.Process(target=worker_main, args=(str(self.path), child, events, options), daemon=True)
        self.process.start()
        child.close()
        self.conn, self.events = parent, events
        self.generation += 1
        threading.Thread(target=self._pump, args=(events, self.generation), daemon=True).start()
        if not parent.poll(STARTUP_TIMEOUT):
            self.kill()
            raise SessionError(500, "StartupError", "the database worker did not start")
        ready = parent.recv()
        if not ready["ready"]:
            self.kill()
            raise SessionError(500, ready["error"]["type"], ready["error"]["message"])
        self.recovered_frames = ready["recovered_frames"]

    def kill(self):
        if self.process is not None and self.process.is_alive():
            self.process.kill()
        if self.process is not None:
            self.process.join()
            self.events.put(None)  # stop this generation's event pump

    def call(self, message, timeout):
        """Send one request and wait for its reply; restart the worker if it runs too long or dies."""
        try:
            self.conn.send(message)
            if self.conn.poll(timeout):
                return self.conn.recv()
        except (EOFError, BrokenPipeError, ConnectionResetError):
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

    def _pump(self, queue, generation):
        while True:
            batch = queue.get()
            if batch is None:
                return
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

    async def request(self, message, timeout=None):
        async with self.lock:
            self.last_used = time.monotonic()
            reply = await asyncio.to_thread(self.call, message, timeout or self.settings.query_timeout)
            self.last_used = time.monotonic()
            return reply

    async def crash(self, during_write):
        async with self.lock:
            self.last_used = time.monotonic()
            return await asyncio.to_thread(self._crash, during_write)

    def _crash(self, during_write):
        rows_per_batch, committed = 25, None
        if during_write:
            # Let a few transactions commit, then kill the worker while it is inside the next one.
            target = random.randint(2, 6)
            self.conn.send({"op": "workload", "batches": 1000, "rows_per_batch": rows_per_batch})
            while committed is None or committed["committed_batches"] < target:
                if not self.conn.poll(self.settings.query_timeout):
                    break
                committed = self.conn.recv()
            time.sleep(random.uniform(0, 0.01))
        self.broadcast([{"kind": "crash", "during_write": during_write}])
        self.kill()
        self.start()  # opening the database replays the log
        reply = self.call({"op": "query", "sql": "SELECT count(*) FROM crash_demo" if during_write else "SELECT 1",
                           "max_rows": 1}, self.settings.query_timeout)
        summary = {"recovered_frames": self.recovered_frames, "during_write": during_write}
        if during_write and committed is not None:
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
                self.conn.send({"op": "close"})  # a clean close checkpoints and removes the log
                if self.conn.poll(10):
                    self.conn.recv()
            except (EOFError, BrokenPipeError, ConnectionResetError):
                pass
        self.kill()


class SessionManager:
    def __init__(self, settings):
        self.settings = settings
        self.sessions = {}
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

    async def create(self):
        if len(self.sessions) >= self.settings.max_sessions:
            await self.reap(force_oldest=True)
        if len(self.sessions) >= self.settings.max_sessions:
            raise SessionError(503, "ServerFull", "too many active sessions; try again in a few minutes")
        session_id = secrets.token_urlsafe(18)
        session = Session(session_id, Path(self.settings.data_dir) / f"{session_id}.db", self.settings,
                          asyncio.get_running_loop())
        await asyncio.to_thread(session.start)
        self.sessions[session_id] = session
        return session

    def get(self, session_id):
        session = self.sessions.get(session_id)
        if session is None:
            raise SessionError(404, "UnknownSession", "no such session; create one with POST /sessions")
        return session

    async def delete(self, session_id):
        session = self.get(session_id)
        del self.sessions[session_id]
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
            if not session.lock.locked() and not session.subscribers:
                await self.delete(session.id)

    async def close_all(self):
        for session_id in list(self.sessions):
            await self.delete(session_id)
