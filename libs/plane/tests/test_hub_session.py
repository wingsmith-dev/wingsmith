"""Drives `OperatorSession` over an in-memory connection pair.

The point of these tests is the four things ADR 0001 gave up codegen for — framing, correlation,
resume and backpressure — so they exercise the protocol rather than the Structs. No database, no
socket: the `Sink` seam is what makes that possible.
"""

import json
import asyncio

from typing import Any
from pathlib import Path

import pytest

from msgspec import json as msgjson

from app.hub.sink import MemorySink
from app.core.auth import STATIC_OPERATOR
from app.hub.state import SessionRegistry
from app.hub.state import new_id
from app.hub.session import Backpressure
from app.hub.session import SessionConfig
from app.hub.session import OperatorSession
from app.hub.messages import DeviceObserved
from app.hub.messages import DeploymentDesired


FIXTURES = Path(__file__).resolve().parents[3] / "contract" / "fixtures"

CLUSTER = "clu_01J9X2AAAAAAAAAAAAAAAAAAAA"


def desired_payload() -> DeploymentDesired:
    raw = (FIXTURES / "deployment.desired.json").read_bytes()
    return msgjson.decode(json.dumps(json.loads(raw)["payload"]).encode(), type=DeploymentDesired)


def payload_of(type_name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{type_name}.json").read_text())["payload"]


def frame(type_name: str, payload: dict[str, Any], **envelope: Any) -> str:
    doc: dict[str, Any] = {
        "v": 1,
        "type": type_name,
        "id": new_id(),
        "ts": "2026-08-19T14:02:11.482Z",
        "payload": payload,
    }
    doc.update(envelope)
    return json.dumps(doc)


class ClosedError(Exception):
    """Stands in for `websockets.ConnectionClosed` on the in-memory pair."""


class FakeConnection:
    """The hub's half of an in-memory duplex link."""

    def __init__(self, send_gate: asyncio.Event | None = None) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = asyncio.Event()
        self.close_code: int | None = None
        self.close_reason: str = ""
        self._send_gate = send_gate
        self._delivered: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, message: Any) -> None:
        if self.closed.is_set():
            raise ClosedError("closed")
        if self._send_gate is not None:
            await self._send_gate.wait()
        self.sent.append(message)
        self._delivered.put_nowait(message)

    async def recv(self) -> str:
        while True:
            if not self.inbox.empty():
                return self.inbox.get_nowait()
            if self.closed.is_set():
                raise ClosedError("closed")
            await asyncio.sleep(0.0005)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.close_code is None:
            self.close_code = code
            self.close_reason = reason
        self.closed.set()

    # ------------------------------------------------------------ operator side

    def push(self, message: str) -> None:
        self.inbox.put_nowait(message)

    async def next_frame(self, timeout: float = 1.0) -> dict[str, Any]:
        raw = await asyncio.wait_for(self._delivered.get(), timeout)
        return json.loads(raw)

    async def next_of_type(self, type_name: str, timeout: float = 1.0) -> dict[str, Any]:
        async with asyncio.timeout(timeout):
            while True:
                doc = await self.next_frame(timeout)
                if doc["type"] == type_name:
                    return doc

    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(raw) for raw in self.sent]


class Harness:
    """A running session plus the socket the operator sees."""

    def __init__(
        self,
        registry: SessionRegistry | None = None,
        sink: MemorySink | None = None,
        config: SessionConfig | None = None,
        send_gate: asyncio.Event | None = None,
    ) -> None:
        self.connection = FakeConnection(send_gate=send_gate)
        self.sink = sink or MemorySink()
        self.registry = SessionRegistry(replay_capacity=16) if registry is None else registry
        self.session = OperatorSession(
            self.connection,
            STATIC_OPERATOR,
            registry=self.registry,
            sink=self.sink,
            config=config or SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=64),
        )
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.session.run())

    async def hello(self, **payload: Any) -> dict[str, Any]:
        body = {"cluster_id": CLUSTER, "agent_version": "0.4.2", "lossy": False}
        body.update(payload)
        self.connection.push(frame("hello", body))
        return await self.connection.next_of_type("welcome")

    async def stop(self) -> None:
        await self.connection.close(1000, "test over")
        if self.task is not None:
            await asyncio.wait_for(self.task, timeout=2)


async def settle() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


async def until(predicate: Any, timeout: float = 1.0) -> None:
    """Waits for a condition the session reaches on its own event-loop turns."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


# --------------------------------------------------------------------------- handshake


async def test_handshake_issues_a_session() -> None:
    harness = Harness()
    await harness.start()
    welcome = await harness.hello()

    assert welcome["type"] == "welcome"
    assert welcome["seq"] is None, "welcome is unsequenced"
    assert welcome["payload"]["full_resync"] is True, "an operator with no session gets a fresh one"
    assert welcome["payload"]["ack_seq"] == 0
    assert len(welcome["payload"]["session_id"]) == 26
    await harness.stop()


async def test_a_second_hello_is_a_protocol_error() -> None:
    harness = Harness()
    await harness.start()
    await harness.hello()

    harness.connection.push(frame("hello", {"cluster_id": CLUSTER, "agent_version": "0.4.2", "lossy": False}))
    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)
    assert harness.connection.close_code == 1002
    await harness.stop()


async def test_a_first_frame_that_is_not_hello_is_refused() -> None:
    harness = Harness()
    await harness.start()
    harness.connection.push(frame("device.observed", payload_of("device.observed"), seq=1))

    error = await harness.connection.next_of_type("error")
    assert error["payload"]["code"] == "invalid"
    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)
    await harness.stop()


async def test_handshake_timeout_closes_the_connection() -> None:
    harness = Harness(config=SessionConfig(handshake_timeout=0.05))
    await harness.start()
    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)
    assert harness.connection.close_code == 1002
    await harness.stop()


# --------------------------------------------------------------------------- resume


async def test_resume_with_a_known_cursor_replays_unacknowledged_frames() -> None:
    registry = SessionRegistry(replay_capacity=16)
    first = Harness(registry=registry)
    await first.start()
    welcome = await first.hello()
    session_id = welcome["payload"]["session_id"]

    await first.session.send_desired("deployment.desired", desired_payload())
    await first.session.send_desired("deployment.desired", desired_payload())
    sent = [await first.connection.next_of_type("deployment.desired") for _ in range(2)]
    assert [doc["seq"] for doc in sent] == [3, 4], "resync.begin and stream.end took seq 1 and 2"
    await first.stop()

    second = Harness(registry=registry)
    await second.start()
    resumed = await second.hello(session_id=session_id, resume_from=3)

    assert resumed["payload"]["full_resync"] is False
    assert resumed["payload"]["session_id"] == session_id
    replayed = await second.connection.next_of_type("deployment.desired")
    assert replayed["seq"] == 4, "only the frame past the cursor is replayed"
    await second.stop()


async def test_an_unretainable_cursor_forces_a_full_resync() -> None:
    registry = SessionRegistry(replay_capacity=16)
    first = Harness(registry=registry)
    await first.start()
    welcome = await first.hello()
    session_id = welcome["payload"]["session_id"]
    await first.session.send_desired("deployment.desired", desired_payload())
    await first.connection.next_of_type("deployment.desired")
    await first.stop()

    second = Harness(registry=registry)
    await second.start()
    answer = await second.hello(session_id=session_id, resume_from=99)

    assert answer["payload"]["full_resync"] is True
    assert answer["payload"]["session_id"] != session_id
    await second.stop()


async def test_an_unknown_session_forces_a_full_resync() -> None:
    harness = Harness()
    await harness.start()
    answer = await harness.hello(session_id=new_id(), resume_from=17)
    assert answer["payload"]["full_resync"] is True
    await harness.stop()


async def test_a_lossy_hello_is_never_resumed() -> None:
    registry = SessionRegistry(replay_capacity=16)
    first = Harness(registry=registry)
    await first.start()
    session_id = (await first.hello())["payload"]["session_id"]
    await first.stop()

    second = Harness(registry=registry)
    await second.start()
    answer = await second.hello(session_id=session_id, resume_from=1, lossy=True)

    assert answer["payload"]["full_resync"] is True
    assert answer["payload"]["session_id"] != session_id
    await second.stop()


async def test_full_resync_streams_a_sequenced_stream_end() -> None:
    harness = Harness()
    await harness.start()
    await harness.hello()

    begin = await harness.connection.next_of_type("resync.begin")
    end = await harness.connection.next_of_type("stream.end")
    assert begin["payload"]["scope"] == "all"
    assert end["reply_to"] == begin["id"], "reply_to is the sole correlation for a resync"
    assert end["seq"] is not None, "stream.end is sequenced"
    assert end["payload"] == {"count": 0, "complete": True}
    await harness.stop()


# --------------------------------------------------------------------------- sequencing


async def test_a_duplicate_seq_is_dropped_silently() -> None:
    harness = Harness()
    await harness.start()
    await harness.hello()

    observed = payload_of("device.observed")
    harness.connection.push(frame("device.observed", observed, seq=1))
    harness.connection.push(frame("device.observed", observed, seq=1))
    await until(lambda: harness.session.state is not None and harness.session.state.ack_cursor == 1)
    await settle()

    assert len(harness.sink.observed) == 1
    assert not harness.connection.closed.is_set()
    await harness.stop()


async def test_a_sequence_gap_closes_the_connection() -> None:
    harness = Harness()
    await harness.start()
    await harness.hello()

    harness.connection.push(frame("device.observed", payload_of("device.observed"), seq=4))
    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)
    assert harness.connection.close_code == 1002
    assert harness.sink.observed == []
    await harness.stop()


async def test_ack_is_contiguous_and_withheld_for_a_frame_that_did_not_apply() -> None:
    harness = Harness(sink=MemorySink(fail_types={"node.event"}))
    await harness.start()
    await harness.hello()

    harness.connection.push(frame("device.observed", payload_of("device.observed"), seq=1))
    harness.connection.push(frame("node.event", payload_of("node.event"), seq=2))
    harness.connection.push(frame("device.observed", payload_of("device.observed"), seq=3))
    await until(lambda: len(harness.sink.observed) == 2)

    assert harness.session.state is not None
    assert harness.session.state.ack_cursor == 1, "the cursor never advances past a hole"
    assert len(harness.sink.observed) == 2, "later frames are still delivered in order"
    assert harness.sink.events == []
    await harness.stop()


async def test_a_bare_ack_carries_the_cursor_when_there_is_nothing_else_to_send() -> None:
    harness = Harness()
    await harness.start()
    await harness.hello()
    await harness.connection.next_of_type("stream.end")

    harness.connection.push(frame("device.observed", payload_of("device.observed"), seq=1))
    ack = await harness.connection.next_of_type("ack")

    assert ack["seq"] is None, "ack is unsequenced and never enters a replay buffer"
    assert ack["ack"] == 1
    assert ack["payload"] == {}
    await harness.stop()


async def test_an_inbound_ack_releases_the_hub_replay_buffer() -> None:
    harness = Harness()
    await harness.start()
    await harness.hello()
    await harness.session.send_desired("deployment.desired", desired_payload())
    await harness.connection.next_of_type("deployment.desired")

    state = harness.session.state
    assert state is not None
    assert len(state.replay) > 0
    harness.connection.push(frame("ack", {}, ack=state.next_seq - 1))
    await until(lambda: len(state.replay) == 0)

    assert len(state.replay) == 0
    await harness.stop()


# --------------------------------------------------------------------------- dispatch


async def test_an_unknown_type_is_answered_with_an_error_and_is_not_fatal() -> None:
    harness = Harness()
    await harness.start()
    await harness.hello()

    harness.connection.push(frame("banana.observed", {"whatever": 1}, seq=1))
    error = await harness.connection.next_of_type("error")

    assert error["payload"]["code"] == "invalid"
    assert error["payload"]["retryable"] is False
    assert not harness.connection.closed.is_set()
    await harness.stop()


async def test_a_malformed_payload_is_answered_with_an_error_and_is_not_fatal() -> None:
    harness = Harness()
    await harness.start()
    await harness.hello()

    harness.connection.push(frame("device.observed", {"device_id": "dev_1"}, seq=1))
    error = await harness.connection.next_of_type("error")

    assert error["payload"]["code"] == "invalid"
    assert not harness.connection.closed.is_set()
    await harness.stop()


async def test_ping_is_answered_with_a_correlated_pong() -> None:
    harness = Harness()
    await harness.start()
    await harness.hello()

    ping = json.loads(frame("ping", {}))
    harness.connection.push(json.dumps(ping))
    pong = await harness.connection.next_of_type("pong")

    assert pong["reply_to"] == ping["id"]
    assert pong["seq"] is None
    await harness.stop()


async def test_frames_are_routed_by_kind() -> None:
    harness = Harness()
    await harness.start()
    await harness.hello()

    harness.connection.push(frame("device.observed", payload_of("device.observed"), seq=1))
    harness.connection.push(frame("device.event", payload_of("device.event"), seq=2))
    harness.connection.push(frame("operation.observed", payload_of("operation.observed"), seq=3))
    harness.connection.push(frame("stream.end", payload_of("stream.end"), seq=4, reply_to="req"))
    await until(lambda: len(harness.sink.resyncs) == 1)

    assert [f.type for f in harness.sink.observed] == ["device.observed"]
    assert [f.type for f in harness.sink.events] == ["device.event"]
    assert [f.type for f in harness.sink.operations] == ["operation.observed"]
    assert harness.sink.resyncs[-1][0] == "end"
    assert harness.sink.resyncs[-1][4] is True
    assert isinstance(harness.sink.observed[0].payload, DeviceObserved)
    await harness.stop()


# --------------------------------------------------------------------------- liveness


async def test_silence_past_the_liveness_timeout_closes_the_connection() -> None:
    harness = Harness(config=SessionConfig(ping_interval=0.02, liveness_timeout=0.05, send_queue_size=64))
    await harness.start()
    await harness.hello()

    await asyncio.wait_for(harness.connection.closed.wait(), timeout=2)
    assert harness.connection.close_code == 1013
    assert any(doc["type"] == "ping" for doc in harness.connection.frames())
    await harness.stop()


# --------------------------------------------------------------------------- backpressure


async def test_a_full_outbound_buffer_closes_the_connection() -> None:
    gate = asyncio.Event()
    harness = Harness(
        config=SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=2),
        send_gate=gate,
    )
    await harness.start()
    harness.connection.push(frame("hello", {"cluster_id": CLUSTER, "agent_version": "0.4.2", "lossy": False}))
    await until(lambda: harness.session.state is not None)

    with pytest.raises(Backpressure):
        for _ in range(8):
            await harness.session.send_desired("deployment.desired", desired_payload())

    assert harness.connection.closed.is_set(), "hub -> operator never drops; it closes instead"
    assert harness.connection.close_code == 1013
    gate.set()
    await harness.stop()


async def test_a_full_replay_buffer_closes_the_connection() -> None:
    harness = Harness(
        registry=SessionRegistry(replay_capacity=2),
        config=SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=64),
    )
    await harness.start()
    await harness.hello()

    with pytest.raises(Backpressure):
        for _ in range(8):
            await harness.session.send_desired("deployment.desired", desired_payload())

    assert harness.connection.closed.is_set()
    await harness.stop()


