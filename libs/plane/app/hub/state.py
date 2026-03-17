"""Per-session state that outlives one connection.

A session is the unit resume counts within: sequence numbers in both directions are meaningless
without it (PROTOCOL §4). The hub therefore keeps a `SessionState` keyed by `session_id` across
reconnects, holding the inbound cursor, the outbound counter, and the outbound replay buffer.

This is deliberately in-process. The *operator's* cursor must be durable — it lives beside the
CRDs — but the hub's copy is recoverable: a hub that lost its state answers `full_resync`, which
is the correct, if expensive, answer.
"""

import time
import secrets

from typing import Any
from collections import deque


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
        "cluster_id",
        "connection_count",
        "expected_seq",
        "next_seq",
        "replay",
        "session_id",
    )

    def __init__(self, session_id: str, cluster_id: str, replay_capacity: int) -> None:
        self.session_id = session_id
        self.cluster_id = cluster_id
        self.next_seq = 1
        self.expected_seq = 1
        self.ack_cursor = 0
        self._applied: set[int] = set()
        self.replay = ReplayBuffer(replay_capacity)
        self.connection_count = 0

    def take_seq(self) -> int:
        seq = self.next_seq
        self.next_seq += 1
        return seq

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

    def __contains__(self, session_id: Any) -> bool:
        return session_id in self._sessions

    def __len__(self) -> int:
        return len(self._sessions)
