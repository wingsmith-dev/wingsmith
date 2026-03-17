"""The seam between a live operator session and everything that persists.

The session owns framing, resume and backpressure; it owns no storage. Every decoded inbound
frame is handed to a `Sink`, and every outbound resync stream is drawn from a `DesiredSource`.
Both are narrow on purpose: the session must be exercisable end to end with no database.

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

    async def begin_resync(self, cluster_id: str, scope: str, request_id: str) -> None:
        """Opens a snapshot for `scope`. Nothing is pruned until it is closed as complete."""
        ...

    async def end_resync(self, cluster_id: str, request_id: str, count: int, complete: bool) -> None:
        """Closes a snapshot.

        `complete` is load-bearing: a truncated snapshot must leave local state untouched rather
        than be read as an authoritative empty set (MESSAGES.md, Resync).
        """
        ...


@runtime_checkable
class DesiredSource(Protocol):
    """Where a session draws declared state from when it must stream a full resync down."""

    def iter_desired(self, cluster_id: str, scope: str) -> AsyncIterator[tuple[str, Struct]]:
        """Yields `(type, payload)` pairs covering the complete declared state for `scope`."""
        ...


class MemorySink:
    """An in-memory `Sink`, for tests and for running the hub with no database.

    `fail_types` makes the withheld-ack path reachable without a broken database: any frame whose
    type is in the set raises, which is how a receiver says "I could not apply this".
    """

    __slots__ = ("events", "fail_types", "observed", "operations", "resyncs")

    def __init__(self, fail_types: set[str] | None = None) -> None:
        self.observed: list[SinkFrame] = []
        self.events: list[SinkFrame] = []
        self.operations: list[SinkFrame] = []
        self.resyncs: list[tuple[str, str, str, int | None, bool | None]] = []
        self.fail_types: set[str] = fail_types or set()

    def _guard(self, frame: SinkFrame) -> None:
        if frame.type in self.fail_types:
            raise RuntimeError(f"sink refused {frame.type}")

    async def apply_observed(self, frame: SinkFrame) -> None:
        self._guard(frame)
        self.observed.append(frame)

    async def record_event(self, frame: SinkFrame) -> None:
        self._guard(frame)
        self.events.append(frame)

    async def record_operation(self, frame: SinkFrame) -> None:
        self._guard(frame)
        self.operations.append(frame)

    async def begin_resync(self, cluster_id: str, scope: str, request_id: str) -> None:
        self.resyncs.append(("begin", cluster_id, request_id, None, None))

    async def end_resync(self, cluster_id: str, request_id: str, count: int, complete: bool) -> None:
        self.resyncs.append(("end", cluster_id, request_id, count, complete))


class NullSink:
    """A `Sink` that accepts everything and keeps nothing.

    The hub is a correct protocol peer before the ingestion track exists; this is what it writes
    to until then. It never raises, so every frame is acknowledged.
    """

    async def apply_observed(self, frame: SinkFrame) -> None: ...

    async def record_event(self, frame: SinkFrame) -> None: ...

    async def record_operation(self, frame: SinkFrame) -> None: ...

    async def begin_resync(self, cluster_id: str, scope: str, request_id: str) -> None: ...

    async def end_resync(self, cluster_id: str, request_id: str, count: int, complete: bool) -> None: ...


class EmptyDesiredSource:
    """A `DesiredSource` with nothing to declare. The registry track replaces this."""

    async def iter_desired(self, cluster_id: str, scope: str) -> AsyncIterator[tuple[str, Struct]]:
        return
        yield  # pragma: no cover
