"""Per-replica connection registry + local fan-out (ADR-009).

The :class:`ConnectionManager` holds every WebSocket this api replica currently
serves and routes a frame — arriving from the Redis pub/sub subscriber — to the
matching local sockets. Cross-replica delivery is Redis pub/sub's job (see
:mod:`titan.realtime.subscriber`); this class only knows about *its own* sockets.

Each :class:`Connection` owns a bounded outbound queue drained by a dedicated
writer task. Dispatch is therefore NON-blocking: the single subscriber loop only
``enqueue``s (never awaits a socket write), so one slow/backpressured client can
never stall delivery to the others (head-of-line isolation). A client that can't
keep up overflows its queue and is shed (marked not-alive → closed by its loop).

During a reconnect replay a connection can be put in *hold* for a project: live
frames for that project are captured aside while the buffered gap is enqueued in
order, then flushed — so a frame published mid-replay is delivered after the gap,
never lost and never ahead of it.
"""

import asyncio
import contextlib
import uuid
from typing import Protocol

#: Bounded per-connection outbound queue. A client that lets this fill (a stalled
#: reader) is shed rather than allowed to consume unbounded memory.
SEND_QUEUE_MAX = 512


class Socket(Protocol):
    """The slice of a Starlette ``WebSocket`` the manager needs (also lets tests
    substitute an in-memory double)."""

    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


def _category(frame_type: str) -> str:
    """The project topic a frame type belongs to (``task.updated`` → ``tasks``)."""
    head = frame_type.split(".", 1)[0]
    return {
        "task": "tasks",
        "run": "runs",
        "message": "messages",
        "decision": "decisions",
        "sprint": "sprints",
        "sys": "sys",
    }.get(head, head)


def _normalise_topic(topic: str) -> str:
    """Reduce a Vol 4 topic string to its category (``project:42:tasks`` →
    ``tasks``; a bare ``tasks`` is returned unchanged)."""
    return topic.rsplit(":", 1)[-1]


class Connection:
    """One live WebSocket, its subscription state, and its outbound queue."""

    def __init__(self, socket: Socket, *, user_id: str, conn_id: str | None = None) -> None:
        self.socket = socket
        self.user_id = user_id
        self.conn_id = conn_id or str(uuid.uuid4())
        self.alive = True
        self._closed = False
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=SEND_QUEUE_MAX)
        self._writer: asyncio.Task[None] | None = None
        # project_id → set of topic categories (empty set = all categories).
        self.projects: dict[str, set[str]] = {}
        # project_id → frames captured during a reconnect replay hold.
        self._held: dict[str, list[str]] = {}
        # Distinct project ids this connection has ever asked to subscribe to,
        # capping the per-connection RBAC-resolver cache + DB lookups.
        self.requested: set[str] = set()

    # -- outbound -----------------------------------------------------------

    def start_writer(self) -> None:
        self._writer = asyncio.create_task(self._run_writer())

    def enqueue(self, wire: str) -> None:
        """Queue one frame for delivery (non-blocking). A full queue means the
        client is not draining — shed it rather than block the dispatch loop or
        grow memory without bound."""
        if self._closed or not self.alive:
            return
        try:
            self._queue.put_nowait(wire)
        except asyncio.QueueFull:
            self.alive = False

    async def _run_writer(self) -> None:  # pragma: no cover — drained via close() in tests
        try:
            while not self._closed:
                try:
                    wire = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                try:
                    await self.socket.send_text(wire)
                except Exception:
                    self.alive = False
                    return
        except asyncio.CancelledError:
            raise

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self.alive = False
        if self._writer is not None:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer
        # Best-effort flush of anything still queued, then close the socket.
        with contextlib.suppress(Exception):
            while not self._queue.empty():
                await self.socket.send_text(self._queue.get_nowait())
        with contextlib.suppress(Exception):
            await self.socket.close(code=code, reason=reason)

    # -- delivery + subscription state -------------------------------------

    def deliver_project(self, project_id: str, frame_type: str, wire: str) -> bool:
        """Route a project frame to this connection's queue (or hold buffer),
        honouring its topic filter. Returns True if it was queued/held."""
        topics = self.projects.get(project_id)
        if topics is None or (topics and _category(frame_type) not in topics):
            return False
        if project_id in self._held:
            self._held[project_id].append(wire)  # captured during replay
        else:
            self.enqueue(wire)
        return True

    def begin_hold(self, project_id: str) -> None:
        self._held[project_id] = []

    def end_hold(self, project_id: str) -> None:
        """Flush frames captured during a replay hold, in arrival (= seq) order,
        then resume live delivery. Synchronous — no await — so no live frame can
        slip in between the flush and clearing the hold."""
        for wire in self._held.pop(project_id, []):
            self.enqueue(wire)


class ConnectionManager:
    """Every connection this replica serves, indexed for O(1) fan-out."""

    def __init__(self) -> None:
        self._by_id: dict[str, Connection] = {}
        self._by_project: dict[str, set[Connection]] = {}
        self._by_user: dict[str, set[Connection]] = {}
        self._agents: set[Connection] = set()
        self._org: dict[str, set[Connection]] = {}

    # -- lifecycle ----------------------------------------------------------

    def register(self, conn: Connection) -> None:
        """Add a connection and auto-subscribe it to the always-on shell topics
        (own-user notifications + global agent status, Vol 4 §5.2)."""
        self._by_id[conn.conn_id] = conn
        self._by_user.setdefault(conn.user_id, set()).add(conn)
        self._agents.add(conn)

    def unregister(self, conn: Connection) -> None:
        self._by_id.pop(conn.conn_id, None)
        self._agents.discard(conn)
        for pid in list(conn.projects):
            self._drop_project(conn, pid)
        self._discard(self._by_user, conn.user_id, conn)
        for org in list(self._org):
            self._discard(self._org, org, conn)

    # -- subscription mutations --------------------------------------------

    def subscribe_project(self, conn: Connection, project_id: str, topics: list[str]) -> None:
        conn.projects[project_id] = {_normalise_topic(t) for t in topics}
        self._by_project.setdefault(project_id, set()).add(conn)

    def unsubscribe_project(self, conn: Connection, project_id: str) -> None:
        self._drop_project(conn, project_id)

    def subscribe_org(self, conn: Connection, org: str) -> None:
        self._org.setdefault(org, set()).add(conn)

    # -- fan-out (called by the subscriber; all non-blocking) ---------------

    async def dispatch_project(self, project_id: str, frame_type: str, wire: str) -> int:
        delivered = 0
        for conn in list(self._by_project.get(project_id, ())):
            if conn.deliver_project(project_id, frame_type, wire):
                delivered += 1
        return delivered

    async def dispatch_user(self, user_id: str, wire: str) -> int:
        targets = list(self._by_user.get(user_id, ()))
        for conn in targets:
            conn.enqueue(wire)
        return len(targets)

    async def dispatch_agents(self, wire: str) -> int:
        targets = list(self._agents)
        for conn in targets:
            conn.enqueue(wire)
        return len(targets)

    async def dispatch_org(self, org: str, wire: str) -> int:
        targets = list(self._org.get(org, ()))
        for conn in targets:
            conn.enqueue(wire)
        return len(targets)

    # -- introspection ------------------------------------------------------

    def count(self) -> int:
        return len(self._by_id)

    # -- internals ----------------------------------------------------------

    def _drop_project(self, conn: Connection, project_id: str) -> None:
        conn.projects.pop(project_id, None)
        self._discard(self._by_project, project_id, conn)

    @staticmethod
    def _discard(index: dict[str, set[Connection]], key: str, conn: Connection) -> None:
        bucket = index.get(key)
        if bucket is not None:
            bucket.discard(conn)
            if not bucket:
                index.pop(key, None)


__all__ = ["SEND_QUEUE_MAX", "Connection", "ConnectionManager", "Socket"]
