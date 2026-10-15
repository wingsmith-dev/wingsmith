"""Per-session state that outlives one connection.

A session is the unit resume counts within: sequence numbers in both directions are meaningless
without it (PROTOCOL §4). The hub therefore keeps a `SessionState` keyed by `session_id` across
reconnects, holding the inbound cursor, the outbound counter, and the outbound replay buffer.

This is deliberately in-process. The *operator's* cursor must be durable — it lives beside the
CRDs — but the hub's copy is recoverable: a hub that lost its state answers `full_resync`, which
is the correct, if expensive, answer.
"""

import time
import asyncio
import secrets

from typing import Any
from logging import getLogger
from collections import deque


logger = getLogger(__name__)


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id() -> str:
    """A 26-character Crockford-base32 ULID: 48 bits of millisecond time, 80 bits of entropy."""
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(value >> shift) & 0x1F] for shift in range(125, -5, -5))


class ReplayBuffer:
    """Frames sent but not yet released by the peer's `ack`.

    Bounded. Overflow is not survivable by dropping — hub → operator must never drop desired
    state (PROTOCOL §6) — so `add` reports the overflow and the caller closes the connection.
    """

    __slots__ = ("_capacity", "_frames", "_released")

    def __init__(self, capacity: int) -> None:
        self._frames: deque[tuple[int, bytes]] = deque()
        self._capacity = capacity
        self._released = 0

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def full(self) -> bool:
        return len(self._frames) >= self._capacity

    @property
    def base(self) -> int:
        """The highest seq known to be released: everything above it is still retained."""
        return self._frames[0][0] - 1 if self._frames else self._released

    def add(self, seq: int, frame: bytes) -> bool:
        """Retains a sent frame. Returns False when the buffer is already full."""
        if self.full:
            return False
        self._frames.append((seq, frame))
        return True

    def release(self, through: int) -> None:
        """Drops everything at or below `through`, the peer's highest contiguous processed seq."""
        self._released = max(self._released, through)
        while self._frames and self._frames[0][0] <= through:
            self._frames.popleft()

    def pending(self) -> list[bytes]:
        return [frame for _, frame in self._frames]

    def retains(self, resume_from: int) -> bool:
        """Whether every frame after `resume_from` is still available to re-send."""
        if not self._frames:
            return resume_from >= self._released
        return resume_from >= self._frames[0][0] - 1


class SessionState:
    """One logical session, spanning any number of connections."""

    __slots__ = (
        "_applied",
        "ack_cursor",
        "adopt_inbound",
        "attached",
        "cluster_id",
        "connection_count",
        "expected_seq",
        "idle_since",
        "next_seq",
        "replay",
        "session_id",
    )

    def __init__(self, session_id: str, cluster_id: str, replay_capacity: int) -> None:
        self.session_id = session_id
        self.cluster_id = cluster_id
        self.next_seq = 1
        self.expected_seq = 1
        self.adopt_inbound = False
        self.ack_cursor = 0
        self._applied: set[int] = set()
        self.replay = ReplayBuffer(replay_capacity)
        self.connection_count = 0
        self.attached = 0
        """How many connections are attached to this session right now, on this process.

        Usually one. Two for the moment a peer resumes before its previous connection has
        finished closing — and that older connection's close must not report the cluster
        gone while the newer one is serving it.
        """
        self.idle_since: float | None = None
        """`time.monotonic()` of the moment the last connection detached.

        `None` while a connection is attached — a live session is never a reap candidate,
        TTL or no TTL. Independent of `connection_count`, which counts connections
        cumulatively rather than concurrently.
        """

    def take_seq(self) -> int:
        seq = self.next_seq
        self.next_seq += 1
        return seq

    def rewind_inbound(self) -> None:
        """Re-arms the inbound cursor for a resumed connection.

        A resuming peer replays from `ack_seq + 1`, so every frame received but not applied on the
        previous connection arrives again. Carrying the old expectation across the reconnect would
        read those frames as duplicates and drop them — and since their ack was withheld precisely
        because they were not applied, the stream would never advance past the hole.

        The expectation is a guess, though, not a fact: the operator holds no durable counter,
        because it is stateless by design — it is started on a cluster, reads what is declared, and
        says what it sees. A restarted process resumes the session it checkpointed and numbers its
        own stream from one again, which under a remembered expectation is silently below it, and
        every observation it pushes is dropped as a duplicate. So the first sequenced frame of a
        resumed connection sets the expectation instead of being judged by it.
        """
        self.expected_seq = self.ack_cursor + 1
        self.adopt_inbound = True
        self._applied.clear()

    def adopt(self, seq: int) -> None:
        """Takes the peer's numbering as the expectation, for the first frame after a reconnect.

        The acknowledgement cursor moves with it. It counts the peer's stream, and a peer
        that restarted is counting a new one: left where it was, every frame the hub sends
        would acknowledge seqs the peer has not sent yet, and the peer would release from
        its own replay buffer the frames it most needs kept.
        """
        self.expected_seq = seq
        self.ack_cursor = seq - 1
        self.adopt_inbound = False
        self._applied.clear()

    def mark_applied(self, seq: int) -> None:
        """Records a durably processed inbound seq and advances the contiguous ack cursor."""
        self._applied.add(seq)
        while self.ack_cursor + 1 in self._applied:
            self.ack_cursor += 1
            self._applied.discard(self.ack_cursor)


class SessionRegistry:
    """The set of sessions the hub can resume, keyed by `session_id`."""

    __slots__ = ("_sessions", "replay_capacity")

    def __init__(self, replay_capacity: int = 4096) -> None:
        self._sessions: dict[str, SessionState] = {}
        self.replay_capacity = replay_capacity

    def get(self, session_id: str | None) -> SessionState | None:
        return self._sessions.get(session_id) if session_id else None

    def create(self, cluster_id: str) -> SessionState:
        state = SessionState(new_id(), cluster_id, self.replay_capacity)
        self._sessions[state.session_id] = state
        return state

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def connected(self, session_id: str) -> None:
        """A connection attached to `session_id`. Cancels any idle countdown.

        `connection_count` is cumulative — the total number of connections this session has
        ever had. How many are open now is `attached`, and liveness for the reaper is
        `idle_since`, which only the last of them to detach starts.
        """
        state = self._sessions.get(session_id)
        if state is not None:
            state.connection_count += 1
            state.attached += 1
            state.idle_since = None

    def disconnected(self, session_id: str) -> bool:
        """A connection detached from `session_id`. Returns whether it was the last one attached.

        Only the last one starts the idle countdown, and only the last one may report the
        cluster gone.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return True
        state.attached = max(0, state.attached - 1)
        if state.attached:
            return False
        state.idle_since = time.monotonic()
        return True

    def reap(self, ttl: float) -> list[str]:
        """Drops every session idle for at least `ttl` seconds. Never one with a live connection.

        A session is a reap candidate only once `idle_since` is set, which `connected()`
        clears and `disconnected()` sets only when no connection is left attached — so a
        session mid-reconnect, or one a second connection resumed before the first had
        finished closing, cannot be dropped out from under it.
        """
        now = time.monotonic()
        dropped = [
            session_id
            for session_id, state in self._sessions.items()
            if state.idle_since is not None and now - state.idle_since >= ttl
        ]
        for session_id in dropped:
            self._sessions.pop(session_id, None)
        return dropped

    def __contains__(self, session_id: Any) -> bool:
        return session_id in self._sessions

    def __len__(self) -> int:
        return len(self._sessions)


async def reaper_loop(registry: SessionRegistry, ttl: float, interval: float) -> None:
    """Sweeps `registry` on `interval`, dropping sessions idle past `ttl`.

    Runs for the life of the process (see `app.hub.server.serve_forever`), so a
    cluster that disconnects and never reconnects stops holding a replay buffer of
    up to `replay_capacity` frames once the fleet's reprovisioning lifetime has
    genuinely moved past it — see ADR 0005.
    """
    while True:
        await asyncio.sleep(interval)
        dropped = registry.reap(ttl)
        if dropped:
            logger.info("hub.reaper.dropped count=%s sessions=%s", len(dropped), dropped)
