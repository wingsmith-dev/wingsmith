"""The hub half of the plane <-> operator session protocol.

Implements `contract/PROTOCOL.md`: handshake (§1), envelope and dispatch (§2), correlation (§3),
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

from app.hub.sink import Sink
from app.hub.sink import Liveness
from app.hub.sink import SinkFrame
from app.hub.sink import MemorySink
from app.hub.sink import DesiredSource
from app.hub.sink import EmptyDesiredSource
from app.core.auth import Principal
from app.hub.state import SessionState
from app.hub.state import SessionRegistry
from app.hub.state import new_id
from app.hub.identity import instance_id
from app.hub.ownership import ClaimStore
from app.hub.ownership import MemoryClaimStore
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


logger = getLogger(__name__)


CLOSE_PROTOCOL_ERROR = 1002
CLOSE_INTERNAL_ERROR = 1011
CLOSE_TRY_AGAIN_LATER = 1013

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
    """A peer did something the contract forbids. The connection does not survive it."""

    __slots__ = ("close_code", "code")

    def __init__(self, message: str, code: str = "invalid", close_code: int = CLOSE_PROTOCOL_ERROR) -> None:
        super().__init__(message)
        self.code = code
        self.close_code = close_code


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
        "_last_recv",
        "_outbound",
        "_stopped",
        "claims",
        "config",
        "connection",
        "desired",
        "instance",
        "principal",
        "registry",
        "sink",
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
    ) -> None:
        self.connection = connection
        self.principal = principal
        self.config = SessionConfig() if config is None else config
        self.registry = SessionRegistry(self.config.replay_capacity) if registry is None else registry
        self.sink: Sink = MemorySink() if sink is None else sink
        self.desired: DesiredSource = EmptyDesiredSource() if desired is None else desired
        self.claims: ClaimStore = MemoryClaimStore() if claims is None else claims
        self.instance = instance_id() if instance is None else instance
        self.state: SessionState | None = None
        self._outbound: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self.config.send_queue_size)
        self._last_recv = 0.0
        self._closing = False
        self._close_reason: str | None = None
        self._stopped = asyncio.Event()

    # ----------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        logger.info("hub.session.open principal=%s", self.principal.name)
        self._last_recv = asyncio.get_running_loop().time()
        try:
            try:
                await self._handshake()
            except ProtocolViolation as exc:
                logger.warning("hub.session.handshake_failed reason=%s", exc)
                await self._try_send(self._safe_frame(Error(code=exc.code, message=str(exc), retryable=True)))
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
                self.registry.disconnected(self.state.session_id)
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
        if not hello.cluster_id:
            raise ProtocolViolation("hello carries no cluster_id")

        state, full_resync = self._resolve_session(hello)
        self.state = state
        await self.claims.claim(state.cluster_id, state.session_id, self.instance)
        self.registry.connected(state.session_id)

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

        if full_resync:
            await self._stream_full_resync()
            return

        for frame in state.replay.pending():
            await self._send_now(frame)

    async def _mark_connected(self, hello: Hello) -> None:
        """Reports the peer as reachable. A sink that cannot record it must not fail the session."""
        if not isinstance(self.sink, Liveness):
            return
        try:
            await self.sink.mark_connected(hello.cluster_id, hello.agent_version)
        except Exception:
            logger.warning("hub.session.sync_state_failed phase=connect cluster=%s", hello.cluster_id, exc_info=True)

    async def _mark_disconnected(self) -> None:
        if self.state is None or not isinstance(self.sink, Liveness):
            return
        try:
            await self.sink.mark_disconnected(self.state.cluster_id, self._close_reason)
        except Exception:
            logger.warning(
                "hub.session.sync_state_failed phase=disconnect cluster=%s", self.state.cluster_id, exc_info=True
            )

    def _resolve_session(self, hello: Hello) -> tuple[SessionState, bool]:
        """Decides whether the requested resume point can be honoured (PROTOCOL §4).

        A fresh session is issued — and `full_resync` set — when the operator declares itself
        lossy, when the named session is unknown, or when the requested cursor has already fallen
        out of the replay buffer. Pretending to resume any of those loses frames silently.
        """
        if hello.lossy:
            if hello.session_id:
                self.registry.drop(hello.session_id)
            return self.registry.create(hello.cluster_id), True

        existing = self.registry.get(hello.session_id)
        if existing is None or existing.cluster_id != hello.cluster_id:
            return self.registry.create(hello.cluster_id), True

        resume_from = hello.resume_from or 0
        if resume_from > existing.next_seq - 1 or not existing.replay.retains(resume_from):
            self.registry.drop(existing.session_id)
            return self.registry.create(hello.cluster_id), True

        return existing, False

    async def _stream_full_resync(self) -> None:
        """Streams the hub's complete declared state, terminated by a sequenced `stream.end`."""
        request = self._frame("resync.begin", ResyncBegin(scope="all"))
        request_id = msgjson.decode(request, type=RawEnvelope).id
        await self._enqueue(request)

        count = 0
        complete = True
        try:
            async for type_name, payload in self.desired.iter_desired(self._state.cluster_id, "all"):
                await self._send(type_name, payload, reply_to=request_id)
                count += 1
        except Backpressure:
            raise
        except Exception as exc:  # noqa: BLE001 - a snapshot that fails midway must end incomplete, not crash
            complete = False
            logger.warning("hub.resync.truncated error=%s", exc)

        await self._send("stream.end", StreamEnd(count=count, complete=complete), reply_to=request_id)

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

        if envelope.type == "hello":
            raise ProtocolViolation("hello sent twice on one connection")

        if envelope.sequenced:
            if envelope.seq is None:
                raise ProtocolViolation(f"{envelope.type} must carry a seq")
            if envelope.seq < state.expected_seq:
                logger.debug("hub.frame.duplicate seq=%s expected=%s", envelope.seq, state.expected_seq)
                return False
            if envelope.seq > state.expected_seq:
                raise ProtocolViolation(f"sequence gap: expected {state.expected_seq}, got {envelope.seq}")
            state.expected_seq = envelope.seq + 1
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
        """
        while True:
            await asyncio.sleep(self.config.ownership_check_interval)
            state = self.state
            if state is None:
                continue
            if not await self.claims.owns(state.cluster_id, state.session_id):
                raise Displaced(state.cluster_id)

    async def send_desired(self, type_name: str, payload: Struct, reply_to: str | None = None) -> None:
        """Queues declared state for the operator. The public seam for the registry track."""
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
