"""Live dispatch: a write in the API process reaching an operator on the hub process.

Two seams are exercised and neither needs a database. The writer's half is a
`Notifier`, so a service can be watched announcing without Postgres; the reader's
half takes its connection and its renderer as arguments, so the listen loop can be
driven by hand. What is deliberately *not* asserted anywhere here is delivery —
a dropped notification is corrected by the next resync, and a test that pinned it
down would be inventing a guarantee the design does not make.
"""

import asyncio

from msgspec import Struct

from app.db.notify import CHANNEL
from app.db.notify import DeclaredChange
from app.db.notify import decode
from app.db.notify import encode
from app.hub.dispatch import LiveSessions
from app.hub.dispatch import dispatch
from app.hub.dispatch import listen_loop
from app.hub.messages import DeploymentDeleted
from app.hub.messages import DeploymentDesired


CLUSTER = "clu_01J9X2AAAAAAAAAAAAAAAAAAAA"
DEPLOYMENT = "dep_01J9X2BBBBBBBBBBBBBBBBBBBB"


def change(**overrides) -> DeclaredChange:
    fields = {"cluster_id": CLUSTER, "kind": "deployment", "uid": DEPLOYMENT, "generation": 3}
    fields.update(overrides)
    return DeclaredChange(**fields)


class FakeSession:
    """Stands in for `OperatorSession`, which dispatch only ever calls one method on."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Struct]] = []

    async def send_desired(self, type_name: str, payload: Struct, reply_to: str | None = None) -> None:
        self.sent.append((type_name, payload))


class FakeRenderer:
    def __init__(self, rendered: tuple[str, Struct] | None) -> None:
        self.rendered = rendered
        self.calls: list[DeclaredChange] = []

    async def render(self, change: DeclaredChange) -> tuple[str, Struct] | None:
        self.calls.append(change)
        return self.rendered


class FakeNotify:
    def __init__(self, payload: str) -> None:
        self.payload = payload


class FakeListenConnection:
    """One connection's worth of `LISTEN`, ending when its notifications run out."""

    def __init__(self, payloads: list[str], done: asyncio.Event) -> None:
        self.payloads = payloads
        self.done = done
        self.statements: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, statement: str) -> None:
        self.statements.append(statement)

    async def notifies(self):
        for payload in self.payloads:
            yield FakeNotify(payload)
        self.done.set()
        await asyncio.Event().wait()


def desired() -> DeploymentDesired:
    from tests.test_hub_session import desired_payload

    return desired_payload()


def test_the_hint_survives_a_round_trip_through_the_channel() -> None:
    assert decode(encode(change())) == change()


async def test_a_change_is_sent_down_the_session_for_its_cluster() -> None:
    live = LiveSessions()
    session = FakeSession()
    live.register(CLUSTER, session)
    renderer = FakeRenderer(("deployment.desired", desired()))

    assert await dispatch(change(), live, renderer) is True
    assert [type_name for type_name, _ in session.sent] == ["deployment.desired"]


async def test_a_change_for_a_cluster_this_process_does_not_hold_is_dropped() -> None:
    """The ordinary case on every hub but one, and it must not cost a database read."""
    renderer = FakeRenderer(("deployment.desired", desired()))

    assert await dispatch(change(), LiveSessions(), renderer) is False
    assert renderer.calls == []


async def test_a_deleted_deployment_is_dispatched_as_a_deletion() -> None:
    live = LiveSessions()
    session = FakeSession()
    live.register(CLUSTER, session)
    deleted = DeploymentDeleted(
        deployment_id=DEPLOYMENT, cluster_id=CLUSTER, generation=3, deleted_at="2026-08-19T14:02:11.482Z"
    )

    await dispatch(change(), live, FakeRenderer(("deployment.deleted", deleted)))

    assert session.sent == [("deployment.deleted", deleted)]


async def test_a_closing_session_cannot_evict_the_one_that_displaced_it() -> None:
    """Displacement is a reconnect elsewhere, but the loser's close still runs afterwards."""
    live = LiveSessions()
    old, new = FakeSession(), FakeSession()
    live.register(CLUSTER, old)
    live.register(CLUSTER, new)

    live.unregister(CLUSTER, old)

    assert live.get(CLUSTER) is new


async def test_the_listener_dispatches_what_arrives_on_the_channel() -> None:
    live = LiveSessions()
    session = FakeSession()
    live.register(CLUSTER, session)
    done = asyncio.Event()
    connection = FakeListenConnection([encode(change())], done)

    async def connect(dsn: str):
        return connection

    task = asyncio.create_task(
        listen_loop(live, renderer=FakeRenderer(("deployment.desired", desired())), dsn="x", connect=connect)
    )
    await asyncio.wait_for(done.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert connection.statements == [f"LISTEN {CHANNEL}"]
    assert [type_name for type_name, _ in session.sent] == ["deployment.desired"]


async def test_a_garbled_payload_does_not_end_the_listener() -> None:
    """One bad notification is one lost wakeup, not a hub that stops dispatching."""
    live = LiveSessions()
    session = FakeSession()
    live.register(CLUSTER, session)
    done = asyncio.Event()
    connection = FakeListenConnection(["{not json", encode(change())], done)

    async def connect(dsn: str):
        return connection

    task = asyncio.create_task(
        listen_loop(live, renderer=FakeRenderer(("deployment.desired", desired())), dsn="x", connect=connect)
    )
    await asyncio.wait_for(done.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(session.sent) == 1


async def test_a_lost_connection_is_retried() -> None:
    """A reconnect gap loses the notifications that fell in it, and that is survivable."""
    attempts: list[str] = []
    done = asyncio.Event()

    async def connect(dsn: str):
        attempts.append(dsn)
        if len(attempts) == 1:
            raise ConnectionError("server closed the connection")
        return FakeListenConnection([], done)

    task = asyncio.create_task(
        listen_loop(LiveSessions(), renderer=FakeRenderer(None), dsn="x", connect=connect, retry_delay=0.0)
    )
    await asyncio.wait_for(done.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(attempts) == 2
