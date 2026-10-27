"""The seam between a live operator session and everything that persists.

The session owns framing, resume and backpressure; it owns no storage. Every decoded inbound
frame is handed to a `Sink`, and every outbound resync stream is drawn from a `DesiredSource`.
Both are narrow: the session must be exercisable end to end with no database.

The failure contract is the protocol's, not Python's: a `Sink` method that raises means the frame
was **not** durably applied, so the session withholds the acknowledgement and the operator replays
it. There is no negative acknowledgement (PROTOCOL §4), so raising is the only way to say no.
"""

from typing import Protocol
from typing import runtime_checkable
from collections.abc import AsyncIterator

from msgspec import Struct


class SinkFrame(Struct):
    """One inbound frame, reduced to what a persistence layer needs.

    `seq` is present so a sink can make application idempotent against replay: the same frame may
    legitimately arrive twice across a resume.
    """

    cluster_id: str
    type: str
    payload: Struct
    seq: int | None = None
    id: str | None = None
    reply_to: str | None = None
    session_id: str | None = None
    """The hub session the frame arrived on. A sink that can fences its write on it (`ClaimLost`)."""


class ClaimLost(Exception):
    """The session a write was made for no longer holds its cluster's claim (ADR 0005).

    Raised by a sink that checks the claim inside the write's own transaction, so a
    displaced session cannot interleave a single row with the one that displaced it.
    The session treats it as being displaced and closes; the write did not happen.
    """

    def __init__(self, cluster_id: str) -> None:
        super().__init__(f"claim on {cluster_id} is held by another session")
        self.cluster_id = cluster_id


@runtime_checkable
class Sink(Protocol):
    """Where a session puts what an operator tells it.

    Implementations must be idempotent per `(cluster_id, type, subject)`: observed state is
    last-writer-wins, and resume replays frames that may already have been applied.
    """

    async def apply_observed(self, frame: SinkFrame) -> None:
        """Applies observed state for one object. Full object, never a patch."""
        ...

    async def record_event(self, frame: SinkFrame) -> None:
        """Records something that happened at a point in time."""
        ...

    async def record_operation(self, frame: SinkFrame) -> None:
        """Records progress or the outcome of a long-running operation."""
        ...

    async def begin_resync(self, cluster_id: str, scope: str, request_id: str, session_id: str | None = None) -> None:
        """Opens a snapshot for `scope`. Nothing is pruned until it is closed as complete."""
        ...

    async def end_resync(
        self, cluster_id: str, request_id: str, count: int, complete: bool, session_id: str | None = None
    ) -> None:
        """Closes a snapshot.

        `complete` is essential: a truncated snapshot must leave local state untouched rather
        than be read as an authoritative empty set (MESSAGES.md, Resync).
        """
        ...


@runtime_checkable
class Liveness(Protocol):
    """The optional half of a `Sink`: what a session knows and only a session can report.

    Kept separate from `Sink` so that a sink with nowhere to put connection state is still a
    complete sink. A session calls these when a peer arrives and when it goes away, which is the
    one fact about a cluster that no frame carries.
    """

    async def mark_connected(
        self, cluster_uid: str, operator_version: str, session_id: str | None = None
    ) -> str | None:
        """The peer completed a handshake. `operator_version` comes off `hello`.

        Returns the cluster's canonical uid when it can, so a session can make
        itself reachable under the identity writers announce changes with. `None`
        is allowed and costs only dispatch latency, which a reconnect repairs.
        """
        ...

    async def mark_disconnected(
        self, cluster_uid: str, error: str | None = None, session_id: str | None = None
    ) -> None:
        """The peer is gone. `error` is why, when the hub was the one that closed.

        Written only while `session_id` still holds the cluster's claim: a session that
        was displaced must not report as gone a cluster its successor is serving.
        """
        ...

    async def touch(self, cluster_uid: str, session_id: str | None = None) -> None:
        """The peer is still here. Called on every ownership poll, so `last_seen_at` stays fresh.

        Fenced on the claim like `mark_disconnected`. A cluster whose stamp goes stale is
        one no hub is serving, whatever `reachable` last said.
        """
        ...


@runtime_checkable
class DesiredSource(Protocol):
    """Where a session draws declared state from when it must stream a full resync down."""

    def iter_desired(self, cluster_id: str, scope: str) -> AsyncIterator[tuple[str, Struct]]:
        """Yields `(type, payload)` pairs covering the complete declared state for `scope`."""
        ...


@runtime_checkable
class OperationSource(Protocol):
    """The optional half of a `DesiredSource`: the operations a cluster still owes work on.

    Separate from `iter_desired` because what it yields must never join the
    resync snapshot. A snapshot is authoritative over what is absent from it, and
    an operation that vanished from one would be a drain cancelled by a
    reconnect; these frames therefore go down beside the stream, not
    inside it. Optional for the same reason `Liveness` is: a source with no
    operations table is still a complete source.
    """

    def iter_open_operations(self, cluster_id: str) -> AsyncIterator[tuple[str, Struct]]:
        """Yields `(type, payload)` for every operation of this cluster still in an open phase."""
        ...


class MemorySink:
    """An in-memory `Sink`, for tests and for running the hub with no database.

    `fail_types` makes the withheld-ack path reachable without a broken database: any frame whose
    type is in the set raises, which is how a receiver says "I could not apply this".
    """

    __slots__ = (
        "claim_lost",
        "connections",
        "disconnections",
        "events",
        "fail_types",
        "observed",
        "operations",
        "resyncs",
        "touches",
        "transient",
    )

    def __init__(self, fail_types: set[str] | None = None, transient: dict[str, int] | None = None) -> None:
        self.observed: list[SinkFrame] = []
        self.events: list[SinkFrame] = []
        self.operations: list[SinkFrame] = []
        self.resyncs: list[tuple[str, str, str, int | None, bool | None]] = []
        self.connections: list[tuple[str, str]] = []
        self.disconnections: list[tuple[str, str | None]] = []
        self.touches: list[tuple[str, str | None]] = []
        self.fail_types: set[str] = fail_types or set()
        self.transient: dict[str, int] = dict(transient or {})
        """Per type, how many more times to fail before applying: a database that blinked."""
        self.claim_lost = False

    def _guard(self, frame: SinkFrame) -> None:
        if self.claim_lost:
            raise ClaimLost(frame.cluster_id)
        if frame.type in self.fail_types:
            raise RuntimeError(f"sink refused {frame.type}")
        if self.transient.get(frame.type, 0) > 0:
            self.transient[frame.type] -= 1
            raise RuntimeError(f"sink could not reach its database for {frame.type}")

    async def apply_observed(self, frame: SinkFrame) -> None:
        self._guard(frame)
        self.observed.append(frame)

    async def record_event(self, frame: SinkFrame) -> None:
        self._guard(frame)
        self.events.append(frame)

    async def record_operation(self, frame: SinkFrame) -> None:
        self._guard(frame)
        self.operations.append(frame)

    async def begin_resync(self, cluster_id: str, scope: str, request_id: str, session_id: str | None = None) -> None:
        self.resyncs.append(("begin", cluster_id, request_id, None, None))

    async def end_resync(
        self, cluster_id: str, request_id: str, count: int, complete: bool, session_id: str | None = None
    ) -> None:
        self.resyncs.append(("end", cluster_id, request_id, count, complete))

    async def mark_connected(
        self, cluster_uid: str, operator_version: str, session_id: str | None = None
    ) -> str | None:
        self.connections.append((cluster_uid, operator_version))
        return cluster_uid

    async def mark_disconnected(
        self, cluster_uid: str, error: str | None = None, session_id: str | None = None
    ) -> None:
        self.disconnections.append((cluster_uid, error))

    async def touch(self, cluster_uid: str, session_id: str | None = None) -> None:
        self.touches.append((cluster_uid, session_id))


class NullSink:
    """A `Sink` that accepts everything and keeps nothing, for a hub run with no database.

    It never raises, so every frame is acknowledged.
    """

    async def apply_observed(self, frame: SinkFrame) -> None: ...

    async def record_event(self, frame: SinkFrame) -> None: ...

    async def record_operation(self, frame: SinkFrame) -> None: ...

    async def begin_resync(
        self, cluster_id: str, scope: str, request_id: str, session_id: str | None = None
    ) -> None: ...

    async def end_resync(
        self, cluster_id: str, request_id: str, count: int, complete: bool, session_id: str | None = None
    ) -> None: ...

    async def mark_connected(
        self, cluster_uid: str, operator_version: str, session_id: str | None = None
    ) -> str | None: ...

    async def mark_disconnected(
        self, cluster_uid: str, error: str | None = None, session_id: str | None = None
    ) -> None: ...

    async def touch(self, cluster_uid: str, session_id: str | None = None) -> None: ...


class EmptyDesiredSource:
    """A `DesiredSource` with nothing to declare, for tests and for a hub run with no database."""

    async def iter_desired(self, cluster_id: str, scope: str) -> AsyncIterator[tuple[str, Struct]]:
        return
        yield  # pragma: no cover
