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

from app.auth import Principal
from app.auth import AuthManager
from app.hub.sink import MemorySink
from app.hub.state import SessionState
from app.hub.state import SessionRegistry
from app.hub.state import new_id
from app.hub.session import Backpressure
from app.hub.session import SessionConfig
from app.hub.session import OperatorSession
from app.hub.dispatch import LiveSessions
from app.hub.messages import DeviceObserved
from app.hub.messages import OperationTarget
from app.hub.messages import OperationDesired
from app.hub.messages import DeploymentDesired
from app.hub.standing import ROTATED
from app.hub.standing import UNKNOWN
from app.hub.standing import DISABLED
from app.hub.standing import MemoryStanding
from app.hub.standing import judge
from app.hub.ownership import ClaimStore
from app.hub.ownership import MemoryClaimStore


FIXTURES = Path(__file__).resolve().parents[2] / "packages" / "contract" / "fixtures"

CLUSTER = "clu_01J9X2AAAAAAAAAAAAAAAAAAAA"

OPERATOR = AuthManager.operator_principal(cluster_uid=CLUSTER, cluster_name="alpha")
"""What a connection opened with this cluster's enrolment token runs as."""


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
        if not self.inbox.empty():
            return self.inbox.get_nowait()
        if self.closed.is_set():
            raise ClosedError("closed")
        received = asyncio.ensure_future(self.inbox.get())
        closed = asyncio.ensure_future(self.closed.wait())
        try:
            await asyncio.wait({received, closed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            closed.cancel()
            if not received.done():
                received.cancel()
        if received.done() and not received.cancelled():
            return received.result()
        raise ClosedError("closed")

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
        claims: ClaimStore | None = None,
        instance: str | None = None,
        desired: Any | None = None,
        live: Any | None = None,
        principal: Principal | None = None,
        standing: Any | None = None,
        credential: str | None = None,
    ) -> None:
        self.connection = FakeConnection(send_gate=send_gate)
        self.live = live
        self.sink = sink or MemorySink()
        self.registry = SessionRegistry(replay_capacity=16) if registry is None else registry
        self.claims = claims or MemoryClaimStore()
        self.session = OperatorSession(
            self.connection,
            OPERATOR if principal is None else principal,
            registry=self.registry,
            sink=self.sink,
            config=config or SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=64),
            claims=self.claims,
            instance=instance or "hub-test",
            desired=desired,
            live=live,
            standing=standing,
            credential=credential,
        )
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.session.run())

    async def hello(self, **payload: Any) -> dict[str, Any]:
        body = {"cluster_id": CLUSTER, "operator_version": "0.4.2", "lossy": False}
        body.update(payload)
        self.connection.push(frame("hello", body))
        return await self.connection.next_of_type("welcome")

    async def stop(self) -> None:
        await self.connection.close(1000, "test over")
        if self.task is not None:
            await asyncio.wait_for(self.task, timeout=2)


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

    harness.connection.push(frame("hello", {"cluster_id": CLUSTER, "operator_version": "0.4.2", "lossy": False}))
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


# --------------------------------------------------------------------------- who may hold a session


async def test_the_shared_fleet_principal_opens_no_session() -> None:
    """It names no cluster, so `hello` would decide which cluster's state it writes."""
    harness = Harness(principal=Principal(id="operator", kind="operator", name="operator", scope=None))
    await harness.start()
    harness.connection.push(frame("hello", {"cluster_id": CLUSTER, "operator_version": "0.4.2", "lossy": False}))

    error = await harness.connection.next_of_type("error")
    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)
    assert error["payload"]["retryable"] is False, "no reconnect with the same token can succeed"
    assert harness.connection.close_code == 4401
    assert harness.session.state is None
    await harness.stop()


async def test_a_hello_for_another_cluster_is_refused() -> None:
    harness = Harness()
    await harness.start()
    harness.connection.push(
        frame("hello", {"cluster_id": "clu_01J9X2BBBBBBBBBBBBBBBBBBBB", "operator_version": "0.4.2", "lossy": False})
    )

    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)
    assert harness.connection.close_code == 1002
    await harness.stop()


async def test_a_hello_by_name_is_keyed_by_the_tokens_cluster() -> None:
    harness = Harness()
    await harness.start()
    await harness.hello(cluster_id="alpha")

    assert harness.session.state is not None
    assert harness.session.state.cluster_id == CLUSTER
    await harness.stop()


@pytest.mark.parametrize("reason", [UNKNOWN, DISABLED])
async def test_a_cluster_the_registry_does_not_admit_gets_no_session_and_no_snapshot(reason: str) -> None:
    """An unknown cluster's empty snapshot, sent as complete, is an instruction to delete everything."""
    standing = MemoryStanding()
    standing.refusals[CLUSTER] = reason
    sink = MemorySink()
    harness = Harness(standing=standing, sink=sink)
    await harness.start()
    harness.connection.push(frame("hello", {"cluster_id": CLUSTER, "operator_version": "0.4.2", "lossy": False}))

    error = await harness.connection.next_of_type("error")
    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)
    assert reason in error["payload"]["message"]
    assert error["payload"]["retryable"] is True, "re-enabling the cluster must not need an operator restart"
    assert harness.connection.close_code == 4403
    assert not [doc for doc in harness.connection.frames() if doc["type"] in ("welcome", "stream.end")]
    assert sink.connections == []
    await harness.stop()


@pytest.mark.parametrize("reason", [DISABLED, ROTATED])
async def test_disabling_or_rotating_a_cluster_cuts_its_live_session(reason: str) -> None:
    standing = MemoryStanding()
    config = SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=64, ownership_check_interval=0.01)
    harness = Harness(standing=standing, config=config)
    await harness.start()
    await harness.hello()
    assert not harness.connection.closed.is_set()

    standing.refusals[CLUSTER] = reason

    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)
    assert harness.connection.close_code == 4403
    await harness.stop()


def test_standing_is_judged_on_the_registry_row_and_the_token_the_session_opened_with() -> None:
    assert judge(None, None, None) == UNKNOWN
    assert judge("disabled", "abc", "abc") == DISABLED
    assert judge("active", "new", "old") == ROTATED
    assert judge("draining", "abc", "abc") is None
    assert judge("pending", "abc", None) is None


# --------------------------------------------------------------------------- versions


async def test_a_hello_from_before_operator_version_was_required_still_opens_a_session() -> None:
    """The field was renamed once and a peer that omits it must not be refused for that alone."""
    harness = Harness()
    await harness.start()
    harness.connection.push(frame("hello", {"cluster_id": CLUSTER, "lossy": False}))

    welcome = await harness.connection.next_of_type("welcome")
    assert welcome["payload"]["full_resync"] is True
    await harness.stop()


@pytest.mark.parametrize("version", ["0.4.2", "v0.9.0", "0.1.0-rc.1", "dev", "3f9c2ab", ""])
async def test_an_operator_on_the_supported_line_or_built_from_source_is_admitted(version: str) -> None:
    harness = Harness()
    await harness.start()
    welcome = await harness.hello(operator_version=version)
    assert welcome["type"] == "welcome"
    await harness.stop()


@pytest.mark.parametrize("version", ["1.0.0", "v2.3.4"])
async def test_an_operator_from_another_release_line_is_refused_for_good(version: str) -> None:
    harness = Harness()
    await harness.start()
    harness.connection.push(frame("hello", {"cluster_id": CLUSTER, "operator_version": version, "lossy": False}))

    error = await harness.connection.next_of_type("error")
    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)
    assert error["payload"]["retryable"] is False, "redialling with the same build cannot succeed"
    assert harness.connection.close_code == 1002
    await harness.stop()


async def test_an_envelope_version_outside_the_supported_range_is_refused() -> None:
    harness = Harness()
    await harness.start()
    harness.connection.push(frame("hello", {"cluster_id": CLUSTER, "operator_version": "0.4.2", "lossy": False}, v=2))

    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)
    assert harness.connection.close_code == 1002
    assert harness.session.state is None
    await harness.stop()


def test_the_runtime_travels_under_one_name_only() -> None:
    from msgspec.structs import fields

    from app.hub.messages import ServingSpec
    from app.hub.messages import ReplicaObserved

    assert "engine_version" not in {f.name for f in fields(ServingSpec)}
    assert "engine_image" not in {f.name for f in fields(ServingSpec)}
    assert "engine_version" not in {f.name for f in fields(ReplicaObserved)}


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
    # Frames are handled in order, so once seq 2 is acked the duplicate has been dealt with.
    harness.connection.push(frame("device.observed", observed, seq=2))
    await until(lambda: harness.session.state is not None and harness.session.state.ack_cursor == 2)

    assert len(harness.sink.observed) == 2
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


FAST_RETRY = SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=64, apply_retry_delay=0.001)


async def test_a_frame_that_will_not_apply_closes_the_connection_so_resume_replays_it() -> None:
    """Skipping it left the ack stuck at the hole with nothing that would ever reconnect."""
    harness = Harness(sink=MemorySink(fail_types={"node.event"}), config=FAST_RETRY)
    await harness.start()
    await harness.hello()

    harness.connection.push(frame("device.observed", payload_of("device.observed"), seq=1))
    harness.connection.push(frame("node.event", payload_of("node.event"), seq=2))
    harness.connection.push(frame("device.observed", payload_of("device.observed"), seq=3))
    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)

    assert harness.connection.close_code == 1013
    assert harness.session.state is not None
    assert harness.session.state.ack_cursor == 1, "the cursor never advances past a hole"
    assert len(harness.sink.observed) == 1, "nothing behind the hole is applied"
    assert harness.sink.events == []
    await harness.stop()


async def test_a_database_blink_is_retried_in_place() -> None:
    harness = Harness(sink=MemorySink(transient={"node.event": 2}), config=FAST_RETRY)
    await harness.start()
    await harness.hello()

    harness.connection.push(frame("node.event", payload_of("node.event"), seq=1))
    await until(lambda: harness.session.state is not None and harness.session.state.ack_cursor == 1)

    assert len(harness.sink.events) == 1
    assert not harness.connection.closed.is_set()
    await harness.stop()


async def test_a_write_refused_for_a_lost_claim_closes_as_displaced() -> None:
    sink = MemorySink()
    harness = Harness(sink=sink)
    await harness.start()
    await harness.hello()
    sink.claim_lost = True

    harness.connection.push(frame("device.observed", payload_of("device.observed"), seq=1))
    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)

    assert harness.connection.close_code == 1013
    assert harness.session.state is not None
    assert harness.session.state.ack_cursor == 0
    await harness.stop()


async def test_frames_carry_the_session_their_writes_are_fenced_on() -> None:
    harness = Harness()
    await harness.start()
    await harness.hello()

    harness.connection.push(frame("device.observed", payload_of("device.observed"), seq=1))
    await until(lambda: len(harness.sink.observed) == 1)

    assert harness.session.state is not None
    assert harness.sink.observed[0].session_id == harness.session.state.session_id
    assert harness.sink.observed[0].cluster_id == CLUSTER
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
    # The one observed type whose name does not end in `.observed`. Routing by suffix alone
    # dropped it silently, so an adopted cluster reported its running workloads and the plane
    # logged them as unhandled.
    harness.connection.push(frame("workload.discovered", payload_of("workload.discovered"), seq=4))
    harness.connection.push(frame("stream.end", payload_of("stream.end"), seq=5, reply_to="req"))
    await until(lambda: len(harness.sink.resyncs) == 1)

    assert [f.type for f in harness.sink.observed] == ["device.observed", "workload.discovered"]
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
    gate.set()
    harness = Harness(
        config=SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=2),
        send_gate=gate,
    )
    await harness.start()
    await harness.hello()
    await harness.connection.next_of_type("stream.end")
    await until(lambda: harness.session._deferred is None)
    gate.clear()

    with pytest.raises(Backpressure):
        for _ in range(8):
            await harness.session.send_desired("deployment.desired", desired_payload())

    assert harness.connection.closed.is_set(), "hub -> operator never drops; it closes instead"
    assert harness.connection.close_code == 1013
    gate.set()
    await harness.stop()


async def test_a_full_replay_buffer_closes_the_connection() -> None:
    harness = Harness(
        registry=SessionRegistry(replay_capacity=4),
        config=SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=64),
    )
    await harness.start()
    await harness.hello()
    await harness.connection.next_of_type("stream.end")
    await until(lambda: harness.session._deferred is None)

    with pytest.raises(Backpressure):
        for _ in range(8):
            await harness.session.send_desired("deployment.desired", desired_payload())

    assert harness.connection.closed.is_set()
    await harness.stop()


class ManyDesired:
    """A `DesiredSource` with a declared state larger than any buffer the session has."""

    def __init__(self, count: int, stall_after: int | None = None) -> None:
        self.count = count
        self.stall_after = stall_after

    async def iter_desired(self, cluster_id: str, scope: str):
        for index in range(self.count):
            if index == self.stall_after:
                await asyncio.Event().wait()
            yield "deployment.desired", desired_payload()


async def acknowledge_until_stream_end(harness: Harness) -> dict[str, Any]:
    """Plays an operator that applies and acknowledges everything it is sent."""
    while True:
        doc = await harness.connection.next_frame(timeout=2)
        if doc.get("seq") is not None:
            harness.connection.push(frame("ack", {}, ack=doc["seq"]))
        if doc["type"] == "stream.end":
            return doc


async def test_a_snapshot_larger_than_both_buffers_streams_whole() -> None:
    """A snapshot past the queue's size used to be enqueued before the writer existed and fail
    on every reconnect, so a large enough cluster could never be brought up to date."""
    source = ManyDesired(100)
    harness = Harness(
        registry=SessionRegistry(replay_capacity=16),
        config=SessionConfig(ping_interval=60, liveness_timeout=5, send_queue_size=8),
        desired=source,
    )
    await harness.start()
    await harness.hello()

    end = await acknowledge_until_stream_end(harness)

    assert end["payload"] == {"count": 100, "complete": True}
    assert not harness.connection.closed.is_set()
    await harness.stop()


async def test_a_peer_that_stops_acknowledging_a_snapshot_is_closed_rather_than_waited_on_forever() -> None:
    harness = Harness(
        registry=SessionRegistry(replay_capacity=8),
        config=SessionConfig(ping_interval=60, liveness_timeout=0.1, send_queue_size=8),
        desired=ManyDesired(50),
    )
    await harness.start()
    await harness.hello()

    await asyncio.wait_for(harness.connection.closed.wait(), timeout=2)
    assert harness.connection.close_code == 1013
    await harness.stop()


async def test_a_session_whose_snapshot_was_cut_off_is_not_resumable() -> None:
    """Resuming it would replay the part that was sent, then nothing: a shorter snapshot, silently."""
    registry = SessionRegistry(replay_capacity=64)
    first = Harness(registry=registry, desired=ManyDesired(40, stall_after=5))
    await first.start()
    welcome = await first.hello()
    for _ in range(5):
        await first.connection.next_of_type("deployment.desired")
    await first.stop()

    assert welcome["payload"]["session_id"] not in registry

    second = Harness(registry=registry)
    await second.start()
    answer = await second.hello(session_id=welcome["payload"]["session_id"], resume_from=1)
    assert answer["payload"]["full_resync"] is True
    await second.stop()


class StallsOnPass:
    """A `DesiredSource` whose snapshot hangs half way on one pass, the first or a periodic one."""

    def __init__(self, count: int, stall_pass: int) -> None:
        self.count = count
        self.stall_pass = stall_pass
        self.passes = 0

    async def iter_desired(self, cluster_id: str, scope: str):
        self.passes += 1
        for index in range(self.count):
            if self.passes == self.stall_pass and index == 1:
                await asyncio.Event().wait()
            yield "deployment.desired", desired_payload()


async def test_a_session_whose_periodic_snapshot_was_cut_off_is_not_resumable() -> None:
    """The live sends held during it go with it: the next hello gets a full resync, which carries them."""
    registry = SessionRegistry(replay_capacity=64)
    source = StallsOnPass(3, stall_pass=2)
    first = Harness(
        registry=registry,
        config=SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=64, resync_interval=0.02),
        desired=source,
    )
    await first.start()
    welcome = await first.hello()
    await first.connection.next_of_type("stream.end")
    await until(lambda: source.passes == 2)
    await first.session.send_desired("deployment.desired", desired_payload())
    assert first.session._deferred, "held while the snapshot streams"
    await first.stop()

    session_id = welcome["payload"]["session_id"]
    assert session_id not in registry

    second = Harness(registry=registry)
    await second.start()
    answer = await second.hello(session_id=session_id, resume_from=1)
    assert answer["payload"]["full_resync"] is True
    await second.stop()


async def test_a_session_between_snapshots_stays_resumable() -> None:
    registry = SessionRegistry(replay_capacity=64)
    first = Harness(registry=registry, desired=ManyDesired(2))
    await first.start()
    welcome = await first.hello()
    await first.connection.next_of_type("stream.end")
    await until(lambda: first.session._synced)
    await first.stop()

    assert welcome["payload"]["session_id"] in registry


async def test_live_sends_during_a_snapshot_follow_it_rather_than_land_inside_it() -> None:
    """Inside, a deletion sent before the snapshot's own frame for the same deployment would be undone."""
    gate = asyncio.Event()
    harness = Harness(desired=ManyDesired(3), send_gate=gate)
    await harness.start()
    harness.connection.push(frame("hello", {"cluster_id": CLUSTER, "operator_version": "0.4.2", "lossy": False}))
    await until(lambda: harness.session.state is not None)

    await harness.session.send_desired("deployment.desired", desired_payload())
    gate.set()

    end = await harness.connection.next_of_type("stream.end")
    live = await harness.connection.next_of_type("deployment.desired")
    assert end["payload"]["count"] == 3
    assert live["reply_to"] is None
    assert live["seq"] > end["seq"]
    await harness.stop()


async def test_a_live_session_restreams_its_declared_state_on_an_interval() -> None:
    """A lost wakeup leaves a cluster running what it last knew; nothing else would correct it."""
    harness = Harness(
        config=SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=64, resync_interval=0.02),
        desired=ManyDesired(2),
    )
    await harness.start()
    await harness.hello()

    first = await harness.connection.next_of_type("stream.end")
    second = await harness.connection.next_of_type("stream.end")
    assert first["payload"] == second["payload"] == {"count": 2, "complete": True}
    assert second["seq"] > first["seq"]
    await harness.stop()


# --------------------------------------------------------------------------- ownership (ADR 0005)


async def test_a_second_session_for_one_cluster_displaces_the_first() -> None:
    """Two live sessions for one `cluster_id` — the pre-ADR-0005 race across pods, modeled
    here with two `Harness`es sharing a claim store but not a registry, the way two pods
    would share Postgres but not memory.
    """
    claims = MemoryClaimStore()
    ownership_config = SessionConfig(
        ping_interval=60, liveness_timeout=120, send_queue_size=64, ownership_check_interval=0.01
    )
    first = Harness(claims=claims, config=ownership_config, instance="pod-a")
    await first.start()
    await first.hello()
    assert first.session.state is not None

    second = Harness(claims=claims, config=ownership_config, instance="pod-b")
    await second.start()
    await second.hello()

    await asyncio.wait_for(first.connection.closed.wait(), timeout=1)
    assert first.connection.close_code == 1013, "displaced closes with try-again, not a protocol error"
    assert not await claims.owns(CLUSTER, first.session.state.session_id)
    assert await claims.owns(CLUSTER, second.session.state.session_id)
    await first.stop()
    await second.stop()


async def test_claim_is_atomic_under_concurrent_claims() -> None:
    """`ClusterClaimRepository.upsert` is one statement — racing callers land in some
    order, never in a state where the row disagrees with itself.
    """
    claims = MemoryClaimStore()
    session_ids = [new_id() for _ in range(8)]
    await asyncio.gather(*(claims.claim(CLUSTER, sid, f"pod-{i}") for i, sid in enumerate(session_ids)))

    winners = [sid for sid in session_ids if await claims.owns(CLUSTER, sid)]
    assert len(winners) == 1, "exactly one claim wins; the row never names two owners at once"


async def test_a_session_that_lost_its_claim_stops_writing() -> None:
    """Once displaced, the session must not go on treating frames as applied — it should
    raise and close rather than silently keep acking on behalf of a claim it no longer holds.
    """
    claims = MemoryClaimStore()
    config = SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=64, ownership_check_interval=0.01)
    harness = Harness(claims=claims, config=config)
    await harness.start()
    await harness.hello()
    session_id = harness.session.state.session_id

    await claims.claim(CLUSTER, new_id(), "someone-else")

    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)
    assert harness.connection.close_code == 1013
    assert not await claims.owns(CLUSTER, session_id)
    await harness.stop()


class FlakyClaims(MemoryClaimStore):
    def __init__(self) -> None:
        super().__init__()
        self.failing = 0

    async def owns(self, cluster_id: str, session_id: str) -> bool:
        if self.failing:
            self.failing -= 1
            raise RuntimeError("database unreachable")
        return await super().owns(cluster_id, session_id)


async def test_an_ownership_poll_that_cannot_reach_the_database_keeps_the_session() -> None:
    claims = FlakyClaims()
    config = SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=64, ownership_check_interval=0.01)
    harness = Harness(claims=claims, config=config)
    await harness.start()
    await harness.hello()

    claims.failing = 2
    await until(lambda: claims.failing == 0)
    await until(lambda: len(harness.sink.touches) > 0)

    assert not harness.connection.closed.is_set()
    await harness.stop()


async def test_an_ownership_poll_that_keeps_failing_gives_the_session_up() -> None:
    claims = FlakyClaims()
    config = SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=64, ownership_check_interval=0.01)
    harness = Harness(claims=claims, config=config)
    await harness.start()
    await harness.hello()

    claims.failing = 100
    await asyncio.wait_for(harness.connection.closed.wait(), timeout=1)
    assert harness.connection.close_code == 1013
    await harness.stop()


async def test_a_live_session_keeps_its_cluster_seen() -> None:
    config = SessionConfig(ping_interval=60, liveness_timeout=120, send_queue_size=64, ownership_check_interval=0.01)
    harness = Harness(config=config)
    await harness.start()
    await harness.hello()

    await until(lambda: len(harness.sink.touches) >= 2)
    assert harness.session.state is not None
    assert harness.sink.touches[0] == (CLUSTER, harness.session.state.session_id)
    await harness.stop()


async def test_a_connection_closing_after_its_session_resumed_does_not_report_the_cluster_gone() -> None:
    """A half-open socket noticed late used to mark unreachable a cluster its successor was serving."""
    registry = SessionRegistry(replay_capacity=16)
    sink = MemorySink()
    first = Harness(registry=registry, sink=sink)
    await first.start()
    welcome = await first.hello()
    await first.connection.next_of_type("stream.end")

    second = Harness(registry=registry, sink=sink)
    await second.start()
    resumed = await second.hello(session_id=welcome["payload"]["session_id"], resume_from=2)
    assert resumed["payload"]["full_resync"] is False

    await first.stop()
    assert sink.disconnections == []

    await second.stop()
    assert len(sink.disconnections) == 1


# --------------------------------------------------------------------------- ttl reaper (ADR 0005)


async def test_the_reaper_drops_idle_sessions_but_never_live_ones() -> None:
    registry = SessionRegistry(replay_capacity=16)
    live = Harness(registry=registry)
    await live.start()
    await live.hello()
    live_id = live.session.state.session_id

    idle = Harness(registry=registry)
    await idle.start()
    await idle.hello()
    idle_id = idle.session.state.session_id
    await idle.stop()

    await until(lambda: registry.get(idle_id) is not None and registry.get(idle_id).idle_since is not None)

    dropped = registry.reap(ttl=0.0)

    assert idle_id in dropped
    assert idle_id not in registry
    assert live_id in registry, "a session with a live connection is never reaped"
    await live.stop()


async def test_reap_respects_the_ttl() -> None:
    registry = SessionRegistry(replay_capacity=16)
    harness = Harness(registry=registry)
    await harness.start()
    await harness.hello()
    session_id = harness.session.state.session_id
    await harness.stop()

    await until(lambda: registry.get(session_id) is not None and registry.get(session_id).idle_since is not None)

    assert registry.reap(ttl=3600.0) == [], "not idle long enough yet"
    assert session_id in registry


# --------------------------------------------------------------------------- resume still works within one instance


async def test_resume_still_works_within_one_hub_instance() -> None:
    """The baseline ADR-0005 must not regress: one instance, one claim store, resume as before."""
    registry = SessionRegistry(replay_capacity=16)
    claims = MemoryClaimStore()
    first = Harness(registry=registry, claims=claims, instance="pod-a")
    await first.start()
    welcome = await first.hello()
    session_id = welcome["payload"]["session_id"]
    await first.session.send_desired("deployment.desired", desired_payload())
    await first.connection.next_of_type("deployment.desired")
    await first.stop()

    second = Harness(registry=registry, claims=claims, instance="pod-a")
    await second.start()
    resumed = await second.hello(session_id=session_id, resume_from=3)

    assert resumed["payload"]["full_resync"] is False
    assert resumed["payload"]["session_id"] == session_id
    assert await claims.owns(CLUSTER, session_id)
    await second.stop()


# --------------------------------------------------------------------------- open operations on connect


def an_operation(uid: str, kind: str = "drain") -> OperationDesired:
    return OperationDesired(
        operation_id=uid,
        cluster_id=CLUSTER,
        kind=kind,
        target=OperationTarget(kind="node", id="nod_01J9X2EEEEEEEEEEEEEEEEEEEE"),
        reason="driver upgrade",
        requested_by="operator@example.com",
        deadline="",
        parameters={},
        created_at="2026-08-19T14:02:11.482Z",
    )


class FakeOperationSource:
    """A `DesiredSource` that declares nothing and owes two operations."""

    def __init__(self, operations: list[OperationDesired] | None = None, fail: bool = False) -> None:
        self.operations = [] if operations is None else operations
        self.fail = fail
        self.calls: list[str] = []

    async def iter_desired(self, cluster_id: str, scope: str):
        return
        yield  # pragma: no cover

    async def iter_open_operations(self, cluster_id: str):
        self.calls.append(cluster_id)
        if self.fail:
            raise RuntimeError("no database")
        for payload in self.operations:
            yield "operation.desired", payload


async def test_open_operations_are_sent_on_connect_outside_the_resync_stream() -> None:
    """The only path an operation created while the cluster was offline has left.

    They must not be inside the snapshot: a resync is authoritative over what it
    omits, so an operation carried in one would become prunable, and pruning a
    drain already under way would cancel it.
    """
    source = FakeOperationSource(
        [an_operation("opr_01J9X2DDDDDDDDDDDDDDDDDDDD"), an_operation("opr_01J9X2FFFFFFFFFFFFFFFFFFFF", "cordon")]
    )
    harness = Harness(desired=source)
    await harness.start()
    await harness.hello()

    end = await harness.connection.next_of_type("stream.end")
    first = await harness.connection.next_of_type("operation.desired")
    second = await harness.connection.next_of_type("operation.desired")

    assert source.calls == [CLUSTER]
    assert end["payload"]["count"] == 0, "operations are not counted in the snapshot they are not part of"
    assert first["payload"]["operation_id"] == "opr_01J9X2DDDDDDDDDDDDDDDDDDDD"
    assert second["payload"]["kind"] == "cordon"
    assert first["reply_to"] is None, "an operation answers no resync request"
    await harness.stop()


async def test_open_operations_are_sent_on_a_resume_too() -> None:
    """A resume replays what went unacknowledged, which says nothing about an offline gap."""
    registry = SessionRegistry(replay_capacity=16)
    source = FakeOperationSource([an_operation("opr_01J9X2DDDDDDDDDDDDDDDDDDDD")])
    first = Harness(registry=registry, desired=source)
    await first.start()
    welcome = await first.hello()
    await first.connection.next_of_type("operation.desired")
    await first.stop()

    second = Harness(registry=registry, desired=source)
    await second.start()
    resumed = await second.hello(session_id=welcome["payload"]["session_id"], resume_from=2)

    assert resumed["payload"]["full_resync"] is False
    replayed = await second.connection.next_of_type("operation.desired")
    assert replayed["payload"]["operation_id"] == "opr_01J9X2DDDDDDDDDDDDDDDDDDDD"
    await second.stop()


async def test_a_source_with_no_operations_to_offer_is_a_complete_source() -> None:
    """`EmptyDesiredSource` implements no operations half, and the handshake must not care."""
    harness = Harness()
    await harness.start()
    welcome = await harness.hello()

    assert welcome["payload"]["full_resync"] is True
    await harness.stop()


async def test_operations_that_cannot_be_read_do_not_cost_the_connection() -> None:
    """The operator is connected and correct without them; the next connect tries again."""
    harness = Harness(desired=FakeOperationSource(fail=True))
    await harness.start()
    await harness.hello()
    await harness.connection.next_of_type("stream.end")

    harness.connection.push(frame("ping", {}))
    await harness.connection.next_of_type("pong")
    await harness.stop()


# --------------------------------------------------------------------------- live dispatch keys


class NamingSink(MemorySink):
    """A sink whose cluster answers to a uid the operator has never heard of."""

    async def mark_connected(self, cluster_uid: str, operator_version: str) -> str | None:
        await super().mark_connected(cluster_uid, operator_version)
        return CLUSTER


async def test_a_session_that_said_its_name_is_dispatchable_by_uid() -> None:
    """An operator is configured with a name; every writer announces changes by uid.

    Without the alias the hint finds no session and the operation waits for a
    reconnect, which for an operation is the only thing that would ever carry it.
    """
    live = LiveSessions()
    principal = AuthManager.operator_principal(cluster_uid=CLUSTER, cluster_name="us-cluster-1")
    harness = Harness(sink=NamingSink(), live=live, principal=principal)
    await harness.start()
    await harness.hello(cluster_id="us-cluster-1")
    await until(lambda: live.get(CLUSTER) is not None)

    assert live.get("us-cluster-1") is harness.session
    assert live.get(CLUSTER) is harness.session

    await harness.stop()
    assert live.get(CLUSTER) is None


def test_a_restarted_operator_is_not_read_as_a_duplicate() -> None:
    """The operator holds no durable counter, so a restart numbers its stream from one again.

    Judged against the expectation the hub remembers, every frame it pushes is below the cursor
    and silently dropped — which is how a deployment could report `WeightsNotResident` every
    fifteen seconds while the console showed nothing at all.
    """
    state = SessionState("ses_1", "clu_1", replay_capacity=16)
    state.mark_applied(1)
    state.mark_applied(2)
    assert state.ack_cursor == 2

    state.rewind_inbound()
    assert state.expected_seq == 3
    assert state.adopt_inbound is True

    state.adopt(1)
    assert state.expected_seq == 1
    assert state.adopt_inbound is False
    assert state.ack_cursor == 0, "the old cursor counts a stream the peer is no longer sending"

    state.mark_applied(1)
    assert state.ack_cursor == 1


def test_an_operator_that_did_not_restart_keeps_its_place() -> None:
    """Adoption is for the first frame after a reconnect and must not loosen the gap check."""
    state = SessionState("ses_1", "clu_1", replay_capacity=16)
    state.mark_applied(1)
    state.rewind_inbound()

    state.adopt(2)
    assert state.expected_seq == 2
    assert state.adopt_inbound is False


def test_a_session_with_a_connection_still_attached_is_never_reaped() -> None:
    """Two connections, one closed: the session is still live, and the reaper must see it so."""
    registry = SessionRegistry(replay_capacity=16)
    state = registry.create(CLUSTER)
    registry.connected(state.session_id)
    registry.connected(state.session_id)

    assert registry.disconnected(state.session_id) is False
    assert registry.reap(ttl=0.0) == []
    assert registry.disconnected(state.session_id) is True
    assert registry.reap(ttl=0.0) == [state.session_id]
