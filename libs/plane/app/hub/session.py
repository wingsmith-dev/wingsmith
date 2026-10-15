"""The hub half of the plane <-> operator session protocol.

Implements `libs/packages/contract/PROTOCOL.md`: handshake (§1), envelope and dispatch (§2), correlation (§3),
sequencing and resume (§4), liveness (§5) and backpressure (§6). Nothing here touches storage —
inbound frames go to a `Sink`, declared state comes from a `DesiredSource`.

The four things this file owns are the four that ADR 0001 gave up codegen for. They are written
out longhand because nothing enforces them mechanically.
"""

import asyncio

from typing import Any
from typing import Protocol
from logging import getLogger
from datetime import UTC
from datetime import datetime
from dataclasses import dataclass

import msgspec

from msgspec import Raw
from msgspec import Struct
from msgspec import json as msgjson

from app.auth import Principal
from app.hub.sink import Sink
from app.hub.sink import Liveness
from app.hub.sink import SinkFrame
from app.hub.sink import MemorySink
from app.hub.sink import DesiredSource
from app.hub.sink import OperationSource
from app.hub.sink import EmptyDesiredSource
from app.hub.state import SessionState
from app.hub.state import SessionRegistry
from app.hub.state import new_id
from app.hub.dispatch import LiveSessions
from app.hub.identity import instance_id
from app.hub.messages import MESSAGE_TYPES
from app.hub.messages import ENVELOPE_VERSION
from app.hub.messages import UNSEQUENCED_TYPES
from app.hub.messages import Ack
from app.hub.messages import Ping
from app.hub.messages import Pong
from app.hub.messages import Error
from app.hub.messages import Hello
from app.hub.messages import Welcome
from app.hub.messages import Envelope
from app.hub.messages import StreamEnd
from app.hub.messages import RawEnvelope
from app.hub.messages import ResyncBegin
from app.hub.standing import Standing
from app.hub.standing import OpenStanding
from app.hub.ownership import ClaimStore
from app.hub.ownership import MemoryClaimStore
from app.modules.clusters.enrolment import schedule_promote_on_enrolment


logger = getLogger(__name__)


CLOSE_PROTOCOL_ERROR = 1002
CLOSE_INTERNAL_ERROR = 1011
CLOSE_TRY_AGAIN_LATER = 1013
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403

MAX_FRAME_BYTES = 8 << 20

_OBSERVED_SUFFIX = ".observed"
_EVENT_SUFFIX = ".event"


class Connection(Protocol):
    """The subset of a WebSocket the session uses. `ServerConnection` satisfies it as-is."""

    async def send(self, message: Any) -> None: ...

    async def recv(self) -> Any: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class UnknownType(Exception):
    """A `type` outside `MESSAGE_TYPES`. Logged and answered with `error`, never fatal (§2)."""

    __slots__ = ("frame_id", "type_name")

    def __init__(self, type_name: str, frame_id: str) -> None:
        super().__init__(f"unknown message type {type_name}")
        self.type_name = type_name
        self.frame_id = frame_id


class ProtocolViolation(Exception):
    """A peer did something the contract forbids. The connection does not survive it.

    `retryable` is what the `error` frame tells the operator, and the operator stops
    redialling on a refusal that says it is not: only say so when no reconnect can
    succeed until somebody changes something on the cluster's side.
    """

    __slots__ = ("close_code", "code", "retryable")

    def __init__(
        self,
        message: str,
        code: str = "invalid",
        close_code: int = CLOSE_PROTOCOL_ERROR,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.close_code = close_code
        self.retryable = retryable


class Refused(ProtocolViolation):
    """The peer is who it says it is and may not hold a session: wrong credential, or a cluster
    the registry does not admit. Closed in the 4xxx range the server's own refusal uses."""

    def __init__(self, message: str, code: str, close_code: int, retryable: bool = True) -> None:
        super().__init__(message, code=code, close_code=close_code, retryable=retryable)


class Backpressure(ProtocolViolation):
    """The outbound path could not keep up.

    Hub → operator must never drop desired state, so the only correct response is to close and let
    resume repair it (PROTOCOL §6).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="unavailable", close_code=CLOSE_TRY_AGAIN_LATER)


class Displaced(ProtocolViolation):
    """This session lost its claim on `cluster_id` — a newer `hello` won it elsewhere (ADR 0005).

    Not a protocol error in the operator's sense: the fix is a plain reconnect, most
    likely to a different pod, which is why this carries the same "try again" close code
    as `Backpressure` rather than the generic protocol-error one.
    """

    def __init__(self, cluster_id: str) -> None:
        super().__init__(
            f"claim on cluster {cluster_id!r} lost to a newer session",
            code="displaced",
            close_code=CLOSE_TRY_AGAIN_LATER,
        )


@dataclass(frozen=True)
class SessionConfig:
    """Timings and bounds. Defaults are the numbers in PROTOCOL §1, §5 and §6."""

    handshake_timeout: float = 10.0
    ping_interval: float = 10.0
    liveness_timeout: float = 30.0
    send_queue_size: int = 1024
    replay_capacity: int = 4096
    ownership_check_interval: float = 10.0
    resync_interval: float = 900.0
    """How often a live session restreams the cluster's whole declared state.

    Live dispatch is a wakeup, and a wakeup can be lost: a hub mid-restart, a
    listener reconnecting, a session that was not attached yet. A cluster that
    missed one keeps running what it last knew, and nothing else would ever tell
    it otherwise while the session stays up. This bounds that drift.
    """


def now_ts() -> str:
    """RFC 3339 UTC with exactly milliseconds, which is what the wire format specifies."""
    moment = datetime.now(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class OperatorSession:
    """One live operator connection.

    A connection is not a session: the session survives reconnects and is what `seq` counts
    within. This object is per-connection and borrows its `SessionState` from the registry.
    """

    __slots__ = (
        "_close_reason",
        "_closing",
        "_deferred",
        "_drained",
        "_full_resync",
        "_last_recv",
        "_outbound",
        "_released",
        "_resync_again",
        "_stopped",
        "_synced",
        "claims",
        "config",
        "connection",
        "credential",
        "desired",
        "instance",
        "live",
        "principal",
        "registry",
        "sink",
        "standing",
        "state",
    )

    def __init__(
        self,
        connection: Connection,
        principal: Principal,
        registry: SessionRegistry | None = None,
        sink: Sink | None = None,
        desired: DesiredSource | None = None,
        config: SessionConfig | None = None,
        claims: ClaimStore | None = None,
        instance: str | None = None,
        live: LiveSessions | None = None,
        standing: Standing | None = None,
        credential: str | None = None,
    ) -> None:
        self.connection = connection
        self.principal = principal
        self.standing: Standing = OpenStanding() if standing is None else standing
        self.credential = credential
        """The digest of the enrolment token this connection opened with, so a rotation is seen."""
        self.config = SessionConfig() if config is None else config
        self.registry = SessionRegistry(self.config.replay_capacity) if registry is None else registry
        self.sink: Sink = MemorySink() if sink is None else sink
        self.desired: DesiredSource = EmptyDesiredSource() if desired is None else desired
        self.claims: ClaimStore = MemoryClaimStore() if claims is None else claims
        self.instance = instance_id() if instance is None else instance
        self.live = LiveSessions() if live is None else live
        self.state: SessionState | None = None
        self._outbound: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self.config.send_queue_size)
        self._last_recv = 0.0
        self._closing = False
        self._close_reason: str | None = None
        self._stopped = asyncio.Event()
        self._full_resync = False
        self._synced = False
        """Whether this connection finished bringing the operator up to date.

        Until it has, the session is not resumable: a resume replays only what was
        sent, and a snapshot cut off half way would resume as a silently shorter one.
        """
        self._deferred: list[tuple[str, Struct, str | None]] | None = []
        """Live sends held back while a snapshot streams, or `None` when sends go straight out."""
        self._resync_again = False
        self._drained = asyncio.Event()
        self._released = asyncio.Event()

    # ----------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        logger.info("hub.session.open principal=%s", self.principal.name)
        self._last_recv = asyncio.get_running_loop().time()
        try:
            try:
                await self._handshake()
            except ProtocolViolation as exc:
                logger.warning("hub.session.handshake_failed reason=%s", exc)
                await self._try_send(self._safe_frame(Error(code=exc.code, message=str(exc), retryable=exc.retryable)))
                await self._close(exc.close_code, str(exc))
                return
            except TimeoutError:
                logger.warning("hub.session.handshake_timeout principal=%s", self.principal.name)
                await self._close(CLOSE_PROTOCOL_ERROR, "handshake timeout")
                return
            except Exception as exc:  # noqa: BLE001 - a broken handshake must close, never crash the hub
                logger.warning("hub.session.handshake_error error=%s", exc)
                await self._close(CLOSE_INTERNAL_ERROR, "handshake error")
                return

            await self._pump()
        finally:
            await self._close(1000, "")
            await self._mark_disconnected()
            if self.state is not None:
                self.live.unregister(self.state.cluster_id, self)
                if self._synced:
                    self.registry.disconnected(self.state.session_id)
                else:
                    # A connection that never finished its snapshot or its replay leaves a session
                    # whose replay buffer holds part of a stream. Resuming it would deliver that
                    # part and nothing after it, so the next hello starts over instead.
                    self.registry.drop(self.state.session_id)
            self._stopped.set()
            logger.info(
                "hub.session.close principal=%s session=%s",
                self.principal.name,
                self.state.session_id if self.state else "-",
            )

    async def _pump(self) -> None:
        tasks = [
            asyncio.create_task(self._read_loop(), name="hub-read"),
            asyncio.create_task(self._write_loop(), name="hub-write"),
            asyncio.create_task(self._liveness_loop(), name="hub-liveness"),
            asyncio.create_task(self._ownership_loop(), name="hub-ownership"),
            asyncio.create_task(self._sync_loop(), name="hub-sync"),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if isinstance(exc, ProtocolViolation):
                logger.warning("hub.session.violation reason=%s", exc)
                await self._close(exc.close_code, str(exc))
            elif exc is not None:
                logger.info("hub.session.ended reason=%r", exc)

    async def wait_closed(self) -> None:
        await self._stopped.wait()

    async def _close(self, code: int, reason: str) -> None:
        if self._closing:
            return
        self._closing = True
        self._close_reason = reason or None
        try:
            await self.connection.close(code, reason)
        except Exception:
            logger.debug("hub.session.close_failed", exc_info=True)

    # ----------------------------------------------------------------- handshake

    async def _handshake(self) -> None:
        async with asyncio.timeout(self.config.handshake_timeout):
            raw = await self.connection.recv()
        envelope = self._decode(self._decode_envelope(raw))
        if envelope.type != "hello":
            raise ProtocolViolation(f"expected hello, got {envelope.type}")

        hello = envelope.payload
        assert isinstance(hello, Hello)
        cluster_uid = self._admitted_cluster(hello)
        refusal = await self.standing.refusal(cluster_uid, self.credential)
        if refusal is not None:
            raise self._refusal(cluster_uid, refusal)

        state, full_resync = self._resolve_session(cluster_uid, hello)
        self.state = state
        await self.claims.claim(state.cluster_id, state.session_id, self.instance)
        self.registry.connected(state.session_id)
        self.live.register(state.cluster_id, self)
        if hello.cluster_id != state.cluster_id:
            self.live.alias(hello.cluster_id, self)

        if not full_resync:
            state.rewind_inbound()
        if not full_resync and hello.resume_from:
            state.replay.release(hello.resume_from)
        if envelope.ack and not full_resync:
            state.replay.release(envelope.ack)

        welcome = Welcome(session_id=state.session_id, ack_seq=state.ack_cursor, full_resync=full_resync)
        await self._send_now(self._frame("welcome", welcome, reply_to=envelope.id))
        logger.info(
            "hub.session.welcome cluster=%s session=%s resume_from=%s full_resync=%s",
            hello.cluster_id,
            state.session_id,
            hello.resume_from,
            full_resync,
        )

        await self._mark_connected(hello)

        # Bounded by the replay buffer and written before anything else can take a seq, so the
        # resumed stream reaches the peer in order. A full resync is not sent here: it can be any
        # size, and it streams from `_sync_loop` once the writer and the reader are running.
        self._full_resync = full_resync
        if not full_resync:
            for frame in state.replay.pending():
                await self._send_now(frame)

    async def _mark_connected(self, hello: Hello) -> None:
        """Reports the peer as reachable, and learns the cluster's canonical uid.

        A sink that cannot record it must not fail the session. The uid is what
        makes this session reachable by live dispatch: a writer announcing a
        change holds the uid, and `hello` carries whichever identity the operator
        was configured with. Missing it costs dispatch latency and nothing else.
        """
        if not isinstance(self.sink, Liveness):
            return
        cluster_id = self._state.cluster_id
        try:
            canonical = await self.sink.mark_connected(cluster_id, hello.operator_version)
        except Exception:
            logger.warning("hub.session.sync_state_failed phase=connect cluster=%s", cluster_id, exc_info=True)
            return
        if canonical and canonical != cluster_id:
            self.live.alias(canonical, self)
        if canonical:
            schedule_promote_on_enrolment(canonical)

    async def _mark_disconnected(self) -> None:
        if self.state is None or not isinstance(self.sink, Liveness):
            return
        try:
            await self.sink.mark_disconnected(self.state.cluster_id, self._close_reason)
        except Exception:
            logger.warning(
                "hub.session.sync_state_failed phase=disconnect cluster=%s", self.state.cluster_id, exc_info=True
            )

    def _admitted_cluster(self, hello: Hello) -> str:
        """The cluster this connection speaks for: the one its enrolment token is bound to.

        Only a per-cluster principal opens a session. The shared fleet token named no
        cluster, so a session opened with it believed whichever one `hello` claimed to
        be — any holder could write any cluster's observed state. The server no longer
        resolves that token at all; this is the same rule held where it matters, so a
        caller that builds a session some other way cannot reintroduce it.

        The session is keyed by the token's cluster uid, not by what `hello` said. An
        operator configured by hand may introduce itself by name, and the name is
        accepted as long as it names the same cluster.
        """
        scope = self.principal.scope
        if self.principal.kind != "operator" or not scope:
            raise Refused(
                "the hub only admits a cluster's own enrolment token",
                code="invalid",
                close_code=CLOSE_UNAUTHORIZED,
                retryable=False,
            )
        if not hello.cluster_id:
            raise ProtocolViolation("hello carries no cluster_id")
        if hello.cluster_id not in (scope, self.principal.name):
            raise ProtocolViolation(
                f"hello cluster_id {hello.cluster_id!r} does not match the enrolment token's cluster"
            )
        return scope

    @staticmethod
    def _refusal(cluster_uid: str, reason: str) -> Refused:
        """What a registry refusal closes with. Retryable: re-enabling a cluster must not need a restart."""
        logger.warning("hub.session.refused cluster=%s reason=%s", cluster_uid, reason)
        return Refused(f"cluster {cluster_uid}: {reason}", code="unavailable", close_code=CLOSE_FORBIDDEN)

    def _resolve_session(self, cluster_uid: str, hello: Hello) -> tuple[SessionState, bool]:
        """Decides whether the requested resume point can be honoured (PROTOCOL §4).

        A fresh session is issued — and `full_resync` set — when the operator declares itself
        lossy, when the named session is unknown, or when the requested cursor has already fallen
        out of the replay buffer. Pretending to resume any of those loses frames silently.
        """
        if hello.lossy:
            if hello.session_id:
                self.registry.drop(hello.session_id)
            return self.registry.create(cluster_uid), True

        existing = self.registry.get(hello.session_id)
        if existing is None or existing.cluster_id != cluster_uid:
            return self.registry.create(cluster_uid), True

        resume_from = hello.resume_from or 0
        if resume_from > existing.next_seq - 1 or not existing.replay.retains(resume_from):
            self.registry.drop(existing.session_id)
            return self.registry.create(cluster_uid), True

        return existing, False

    async def _sync_loop(self) -> None:
        """Brings the operator up to date, opens live dispatch, then keeps it up to date.

        A task beside the reader and the writer rather than a step of the handshake. A
        snapshot is as large as the cluster's declared state, and every frame of it
        enters the outbound queue and the replay buffer; streamed before the writer
        existed, a snapshot past the queue's size could only fail, and every
        reconnect would fail the same way. Here the writer drains while this fills,
        and the reader takes the acknowledgements that free the replay buffer.

        Live sends that arrive meanwhile are held and sent after the snapshot, never
        inside it. Inside, a deployment deleted after the snapshot read it would be
        resurrected by the snapshot frame that follows the deletion; after, the
        renderer's current answer is the last word.
        """
        if self._full_resync:
            await self._stream_full_resync()
        await self._replay_open_operations()
        self._synced = True
        await self._release_deferred()

        while True:
            await asyncio.sleep(self.config.resync_interval)
            self._deferred = []
            await self._stream_full_resync()
            await self._replay_open_operations()
            await self._release_deferred()

    async def _release_deferred(self) -> None:
        """Sends what live dispatch handed over during a snapshot, or restreams if too much did."""
        while True:
            held, self._deferred = self._deferred or [], None
            for type_name, payload, reply_to in held:
                await self._send_flow(type_name, payload, reply_to=reply_to)
            if not self._resync_again:
                return
            self._resync_again = False
            self._deferred = []
            await self._stream_full_resync()

    async def _stream_full_resync(self) -> None:
        """Streams the hub's complete declared state, terminated by a sequenced `stream.end`."""
        request_id = await self._send_flow("resync.begin", ResyncBegin(scope="all"))

        count = 0
        complete = True
        try:
            async for type_name, payload in self.desired.iter_desired(self._state.cluster_id, "all"):
                await self._send_flow(type_name, payload, reply_to=request_id)
                count += 1
        except Backpressure:
            raise
        except Exception as exc:  # noqa: BLE001 - a snapshot that fails midway must end incomplete, not crash
            complete = False
            logger.warning("hub.resync.truncated cluster=%s error=%s", self._state.cluster_id, exc)

        await self._send_flow("stream.end", StreamEnd(count=count, complete=complete), reply_to=request_id)
        logger.info("hub.resync.sent cluster=%s count=%s complete=%s", self._state.cluster_id, count, complete)

    async def _replay_open_operations(self) -> None:
        """Re-sends every operation this cluster has not finished, on session start and on every resync.

        Outside the resync stream on purpose, and not counted in its `stream.end`:
        a snapshot is authoritative over what it omits, so an operation inside one
        would become prunable, and pruning a drain that is already evicting pods
        would cancel work half done. The frames go down as ordinary
        `operation.desired`, which is the same thing live dispatch sends.

        Every session start, not only a full resync, and again after every
        periodic resync. A resume replays what this hub sent and never had
        acknowledged; it says nothing about an operation created while nothing was
        connected, or whose notification was lost while the session was up. This
        is what closes that gap. Re-sending one the operator is already running is
        safe — it drops a redelivery of an operation in flight — and is the
        intended recovery path.

        A source that cannot answer must not cost the connection: the operator is
        connected and correct without these, and the next pass tries again.
        """
        if not isinstance(self.desired, OperationSource):
            return
        try:
            async for type_name, payload in self.desired.iter_open_operations(self._state.cluster_id):
                await self._send_flow(type_name, payload)
        except Backpressure:
            raise
        except Exception as exc:  # noqa: BLE001 - unsent operations are retried on the next connect
            logger.warning("hub.operations.replay_failed cluster=%s error=%s", self._state.cluster_id, exc)

    # ----------------------------------------------------------------- inbound

    async def _read_loop(self) -> None:
        while True:
            raw = await self.connection.recv()
            self._last_recv = asyncio.get_running_loop().time()
            raw_envelope = self._decode_envelope(raw)
            try:
                envelope = self._decode(raw_envelope)
            except UnknownType as exc:
                logger.info("hub.frame.unknown_type type=%s", exc.type_name)
                await self._reject(raw_envelope, f"unknown type {exc.type_name}")
                continue
            except msgspec.ValidationError as exc:
                logger.info("hub.frame.invalid error=%s", exc)
                await self._reject(raw_envelope, str(exc))
                continue

            await self._on_frame(envelope)

    async def _reject(self, envelope: Envelope[Any], message: str) -> None:
        """Drops an undecodable frame and says so, without breaking the sequence (§2).

        An unknown `type` is never fatal, which is only true if the frame still counts: the
        envelope decoded, so its `seq` is meaningful, and skipping the accounting would leave the
        peer's next frame arriving as a gap — closing the connection over exactly the frame the
        contract says to drop. Answering also finishes the frame: it will never be applied, so the
        ack is owed now rather than withheld forever.
        """
        if not self._accept(envelope):
            return
        await self._send("error", Error(code="invalid", message=message, retryable=False), reply_to=envelope.id)
        await self._settle(envelope)

    def _accept(self, envelope: Envelope[Any]) -> bool:
        """Applies the ordering rules of §4. False means a duplicate, silently dropped."""
        state = self._state
        if envelope.ack:
            state.replay.release(envelope.ack)
            self._released.set()

        if envelope.type == "hello":
            raise ProtocolViolation("hello sent twice on one connection")

        if envelope.sequenced:
            if envelope.seq is None:
                raise ProtocolViolation(f"{envelope.type} must carry a seq")
            if state.adopt_inbound:
                if envelope.seq != state.expected_seq:
                    logger.info(
                        "hub.stream.adopted cluster=%s seq=%s expected=%s",
                        state.cluster_id,
                        envelope.seq,
                        state.expected_seq,
                    )
                state.adopt(envelope.seq)
            if envelope.seq < state.expected_seq:
                logger.debug("hub.frame.duplicate seq=%s expected=%s", envelope.seq, state.expected_seq)
                return False
            if envelope.seq > state.expected_seq:
                raise ProtocolViolation(f"sequence gap: expected {state.expected_seq}, got {envelope.seq}")
            state.expected_seq = envelope.seq + 1
            state.adopt_inbound = False
        elif envelope.seq is not None:
            raise ProtocolViolation(f"{envelope.type} must not carry a seq")
        return True

    async def _settle(self, envelope: Envelope[Any]) -> None:
        """Records a frame as durably processed, releasing the ack when the cursor moves."""
        state = self._state
        if not envelope.sequenced or envelope.seq is None:
            return
        before = state.ack_cursor
        state.mark_applied(envelope.seq)
        if state.ack_cursor != before:
            await self._flush_ack()

    async def _on_frame(self, envelope: Envelope[Struct]) -> None:
        if not self._accept(envelope):
            return

        try:
            await self._dispatch(envelope)
        except (ProtocolViolation, asyncio.CancelledError):
            raise
        except Exception:
            logger.exception("hub.frame.not_applied type=%s seq=%s", envelope.type, envelope.seq)
            return

        await self._settle(envelope)

    async def _dispatch(self, envelope: Envelope[Struct]) -> None:
        payload = envelope.payload
        cluster_id = self._state.cluster_id
        match envelope.type:
            case "ping":
                await self._send("pong", Pong(), reply_to=envelope.id)
            case "pong" | "ack":
                return
            case "welcome":
                raise ProtocolViolation("welcome is hub to operator only")
            case "error":
                assert isinstance(payload, Error)
                logger.warning(
                    "hub.peer.error code=%s retryable=%s message=%s",
                    payload.code,
                    payload.retryable,
                    payload.message,
                )
            case "resync.begin":
                assert isinstance(payload, ResyncBegin)
                await self.sink.begin_resync(cluster_id, payload.scope, envelope.reply_to or envelope.id)
            case "stream.end":
                assert isinstance(payload, StreamEnd)
                await self.sink.end_resync(
                    cluster_id, envelope.reply_to or envelope.id, payload.count, payload.complete
                )
            case "operation.observed":
                await self.sink.record_operation(self._sink_frame(envelope))
            case "workload.discovered":
                await self.sink.apply_observed(self._sink_frame(envelope))
            case _:
                if envelope.type.endswith(_EVENT_SUFFIX):
                    await self.sink.record_event(self._sink_frame(envelope))
                elif envelope.type.endswith(_OBSERVED_SUFFIX):
                    await self.sink.apply_observed(self._sink_frame(envelope))
                else:
                    logger.info("hub.frame.unhandled type=%s", envelope.type)

    def _sink_frame(self, envelope: Envelope[Struct]) -> SinkFrame:
        return SinkFrame(
            cluster_id=self._state.cluster_id,
            type=envelope.type,
            payload=envelope.payload,
            seq=envelope.seq,
            id=envelope.id,
            reply_to=envelope.reply_to,
        )

    # ----------------------------------------------------------------- outbound

    async def _write_loop(self) -> None:
        while True:
            frame = await self._outbound.get()
            self._drained.set()
            await self.connection.send(frame.decode())

    async def _liveness_loop(self) -> None:
        """Pings on an interval and declares the peer dead after `liveness_timeout` of silence."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.config.ping_interval)
            if loop.time() - self._last_recv > self.config.liveness_timeout:
                raise ProtocolViolation(
                    f"peer silent for more than {self.config.liveness_timeout}s",
                    code="unavailable",
                    close_code=CLOSE_TRY_AGAIN_LATER,
                )
            await self._send("ping", Ping())

    async def _ownership_loop(self) -> None:
        """Polls this session's own claim row and self-evicts the moment it no longer wins.

        Nothing pushes a displaced session a notification — it may be mid-stream on a
        different pod than the one that displaced it — so it has to ask (ADR 0005).

        The same poll asks whether the cluster may still hold a session at all. A
        cluster disabled in the registry, or one whose enrolment token was rotated
        out from under this connection, is cut here within one interval rather than
        on whatever reconnect the operator happens to make next.
        """
        while True:
            await asyncio.sleep(self.config.ownership_check_interval)
            state = self.state
            if state is None:
                continue
            if not await self.claims.owns(state.cluster_id, state.session_id):
                raise Displaced(state.cluster_id)
            refusal = await self.standing.refusal(state.cluster_id, self.credential)
            if refusal is not None:
                raise self._refusal(state.cluster_id, refusal)

    async def send_desired(self, type_name: str, payload: Struct, reply_to: str | None = None) -> None:
        """Queues declared state for the operator. The seam live dispatch sends through.

        Held while a snapshot is streaming (see `_sync_loop`). The hold is bounded by
        the replay buffer: past that, it is dropped and the snapshot is simply sent
        again once this one ends, which carries everything the dropped sends would
        have.
        """
        if self._deferred is not None:
            if len(self._deferred) >= self.config.replay_capacity:
                self._deferred.clear()
                self._resync_again = True
            else:
                self._deferred.append((type_name, payload, reply_to))
            return
        await self._send(type_name, payload, reply_to=reply_to)

    async def _flush_ack(self) -> None:
        """Emits a bare `ack` when there is no outbound frame to piggyback the cursor on (§4)."""
        if self._outbound.empty():
            await self._send("ack", Ack())

    async def _send(self, type_name: str, payload: Struct, reply_to: str | None = None) -> None:
        try:
            frame = self._frame(type_name, payload, reply_to=reply_to)
        except Backpressure as exc:
            await self._close(exc.close_code, str(exc))
            raise
        await self._enqueue(frame)

    async def _send_flow(self, type_name: str, payload: Struct, reply_to: str | None = None) -> str:
        """Sends one frame of a stream, waiting for room rather than failing for want of it.

        A stream is something this side chose to send all at once, so it yields to the
        rest of the session: it keeps half the outbound queue and a quarter of the
        replay buffer free for the frames the reader and the pinger must never be
        refused, and waits for the writer to drain and the peer to acknowledge.

        A peer that stops acknowledging for a whole liveness window is not coming
        back, and that is the one case that still ends in `Backpressure`.

        The seq is taken only once there is room, with no await between taking it and
        queueing the frame, so the queue stays in seq order whatever else is sending.
        Returns the frame's id, which is what a resync's frames reply to.
        """
        state = self._state
        queue_limit = max(1, self.config.send_queue_size // 2)
        replay_limit = max(1, state.replay.capacity - max(1, state.replay.capacity // 4))
        sequenced = type_name not in UNSEQUENCED_TYPES
        while True:
            if self._outbound.qsize() >= queue_limit:
                self._drained.clear()
                await self._drained.wait()
                continue
            if sequenced and len(state.replay) >= replay_limit:
                self._released.clear()
                try:
                    async with asyncio.timeout(self.config.liveness_timeout):
                        await self._released.wait()
                except TimeoutError:
                    exc = Backpressure("peer stopped acknowledging a stream; closing so resume can repair it")
                    await self._close(exc.close_code, str(exc))
                    raise exc from None
                continue
            break
        frame = self._frame(type_name, payload, reply_to=reply_to)
        self._outbound.put_nowait(frame)
        return msgjson.decode(frame, type=RawEnvelope).id

    async def _enqueue(self, frame: bytes) -> None:
        """Queues one encoded frame, closing the connection rather than dropping it (§6)."""
        try:
            self._outbound.put_nowait(frame)
        except asyncio.QueueFull:
            exc = Backpressure("outbound queue full; closing so resume can repair it")
            await self._close(exc.close_code, str(exc))
            raise exc from None

    def _frame(self, type_name: str, payload: Struct, reply_to: str | None = None) -> bytes:
        state = self.state
        seq: int | None = None
        if type_name not in UNSEQUENCED_TYPES and state is not None:
            seq = state.take_seq()
        envelope = Envelope(
            v=ENVELOPE_VERSION,
            type=type_name,
            id=new_id(),
            ts=now_ts(),
            payload=payload,
            seq=seq,
            ack=state.ack_cursor if state is not None and state.ack_cursor else None,
            reply_to=reply_to,
        )
        frame = msgjson.encode(envelope)
        if seq is not None and state is not None and not state.replay.add(seq, frame):
            raise Backpressure("replay buffer full; closing so resume can repair it")
        return frame

    def _safe_frame(self, payload: Error) -> bytes:
        """An unsequenced error frame usable before a session exists."""
        envelope = Envelope(
            v=ENVELOPE_VERSION,
            type="error",
            id=new_id(),
            ts=now_ts(),
            payload=payload,
        )
        return msgjson.encode(envelope)

    async def _send_now(self, frame: bytes) -> None:
        """Writes straight to the socket, bypassing the queue. Handshake and replay only."""
        await self.connection.send(frame.decode())

    async def _try_send(self, frame: bytes) -> None:
        try:
            await self._send_now(frame)
        except Exception:
            logger.debug("hub.session.send_failed", exc_info=True)

    # ----------------------------------------------------------------- decoding

    def _decode_envelope(self, raw: Any) -> Envelope[Raw]:
        """Decodes the envelope alone. Failing here is fatal: an unreadable frame has no `seq`."""
        data = raw.encode() if isinstance(raw, str) else bytes(raw)
        if len(data) > MAX_FRAME_BYTES:
            raise ProtocolViolation("frame exceeds the maximum size")
        try:
            envelope = msgjson.decode(data, type=RawEnvelope)
        except msgspec.DecodeError as exc:
            raise ProtocolViolation(f"undecodable frame: {exc}") from exc
        if envelope.v != ENVELOPE_VERSION:
            raise ProtocolViolation(f"unsupported envelope version {envelope.v}")
        return envelope

    def _decode(self, envelope: Envelope[Raw]) -> Envelope[Struct]:
        """Decodes the payload against the type registry. Failing here is not fatal (§2)."""
        struct = MESSAGE_TYPES.get(envelope.type)
        if struct is None:
            raise UnknownType(envelope.type, envelope.id)
        payload = msgjson.decode(envelope.payload, type=struct)
        return Envelope(
            v=envelope.v,
            type=envelope.type,
            id=envelope.id,
            ts=envelope.ts,
            payload=payload,
            seq=envelope.seq,
            ack=envelope.ack,
            reply_to=envelope.reply_to,
        )

    @property
    def _state(self) -> SessionState:
        if self.state is None:
            raise ProtocolViolation("no session; hello has not completed")
        return self.state
