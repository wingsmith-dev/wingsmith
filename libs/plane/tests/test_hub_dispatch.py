"""Live dispatch: a write in the API process reaching an operator on the hub process.

Two seams are exercised and neither needs a database. The writer's half is a
`Notifier`, so a service can be watched announcing without Postgres; the reader's
half takes its connection and its renderer as arguments, so the listen loop can be
driven by hand. What is *not* asserted anywhere here is delivery:
a dropped notification is corrected by the next resync, and a test that pinned it
down would be inventing a guarantee the design does not make.
"""

import asyncio

from msgspec import Struct

from app.db.notify import CHANNEL
from app.db.notify import DeclaredChange
from app.db.notify import decode
from app.db.notify import encode
from app.hub.dispatch import Dispatcher
from app.hub.dispatch import LiveSessions
from app.hub.dispatch import DatabaseRenderer
from app.hub.dispatch import dispatch
from app.hub.dispatch import listen_loop
from app.hub.messages import OperationTarget
from app.hub.messages import OperationDesired
from app.hub.messages import DeploymentDeleted
from app.hub.messages import DeploymentDesired


CLUSTER = "clu_01J9X2AAAAAAAAAAAAAAAAAAAA"
OTHER_CLUSTER = "clu_01J9X2CCCCCCCCCCCCCCCCCCCC"
DEPLOYMENT = "dep_01J9X2BBBBBBBBBBBBBBBBBBBB"
OPERATION = "opr_01J9X2DDDDDDDDDDDDDDDDDDDD"
NODE = "nod_01J9X2EEEEEEEEEEEEEEEEEEEE"


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
    """Renders a fixed answer. A single frame is accepted and wrapped, because most
    of these tests are about the dispatch loop, not about dependency order."""

    def __init__(self, rendered: tuple[str, Struct] | list[tuple[str, Struct]] | None) -> None:
        if rendered is None:
            self.rendered: list[tuple[str, Struct]] = []
        elif isinstance(rendered, list):
            self.rendered = rendered
        else:
            self.rendered = [rendered]
        self.calls: list[DeclaredChange] = []

    async def render(self, change: DeclaredChange) -> list[tuple[str, Struct]]:
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


async def until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


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
    await until(lambda: session.sent)
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
    await until(lambda: session.sent)
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


# --------------------------------------------------------------------------- operations


def operation(**overrides) -> OperationDesired:
    fields = {
        "operation_id": OPERATION,
        "cluster_id": CLUSTER,
        "kind": "drain",
        "target": OperationTarget(kind="node", id=NODE),
        "reason": "driver upgrade",
        "requested_by": "operator@example.com",
        "deadline": "",
        "parameters": {},
        "created_at": "2026-08-19T14:02:11.482Z",
    }
    fields.update(overrides)
    return OperationDesired(**fields)


async def test_a_new_operation_reaches_the_session_for_its_cluster_and_no_other() -> None:
    """An operation is not declared state and still travels the declared-state channel."""
    live = LiveSessions()
    target, bystander = FakeSession(), FakeSession()
    live.register(CLUSTER, target)
    live.register(OTHER_CLUSTER, bystander)
    hint = change(kind="operation", uid=OPERATION, generation=0)

    assert await dispatch(hint, live, FakeRenderer(("operation.desired", operation()))) is True
    assert [type_name for type_name, _ in target.sent] == ["operation.desired"]
    assert bystander.sent == []


async def test_an_operation_hint_is_rendered_by_the_operations_renderer(monkeypatch) -> None:
    """The kind is what picks a renderer: the snapshot's cannot answer for an operation."""
    seen: list[str] = []

    class FakeFactory:
        """A session factory whose session is never used, because the renderer is stubbed."""

        def __call__(self):
            return self

        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc) -> bool:
            return False

    async def fake_operation_desired(db, uid):
        seen.append(uid)
        return "operation.desired", operation()

    monkeypatch.setattr("app.hub.dispatch.operation_desired", fake_operation_desired)
    rendered = await DatabaseRenderer(FakeFactory()).render(change(kind="operation", uid=OPERATION))

    assert seen == [OPERATION]
    assert [type_name for type_name, _ in rendered] == ["operation.desired"]


CLUSTER_NAME = "us-cluster-1"


async def test_a_session_that_named_itself_is_reachable_by_uid() -> None:
    """The bug this pair exists for: writers hold a uid, an operator announces a name.

    Every other cluster lookup in the plane accepts either identity, so a hint
    carrying the uid must reach a session that introduced itself as a name.
    """
    live = LiveSessions()
    session = FakeSession()
    live.register(CLUSTER_NAME, session)
    live.alias(CLUSTER, session)

    sent = await dispatch(change(cluster_id=CLUSTER), live, FakeRenderer(("deployment.desired", desired())))

    assert sent is True
    assert live.get(CLUSTER_NAME) is session


async def test_closing_drops_every_identity_a_session_answered_to() -> None:
    """An alias that outlived its session would send the next hint into a dead socket."""
    live = LiveSessions()
    session = FakeSession()
    live.register(CLUSTER_NAME, session)
    live.alias(CLUSTER, session)

    live.unregister(CLUSTER_NAME, session)

    assert live.get(CLUSTER) is None
    assert live.get(CLUSTER_NAME) is None


async def test_a_displaced_session_takes_only_its_own_aliases() -> None:
    live = LiveSessions()
    old, new = FakeSession(), FakeSession()
    live.register(CLUSTER_NAME, old)
    live.alias(CLUSTER, old)
    live.register(CLUSTER_NAME, new)
    live.alias(CLUSTER, new)

    live.unregister(CLUSTER_NAME, old)

    assert live.get(CLUSTER) is new
    assert live.get(CLUSTER_NAME) is new


# --------------------------------------------------------------------------- concurrency


class SlowSession(FakeSession):
    """A session whose send never finishes, the way one with a full outbound path behaves."""

    async def send_desired(self, type_name: str, payload: Struct, reply_to: str | None = None) -> None:
        await asyncio.Event().wait()


async def test_a_slow_cluster_does_not_hold_up_another_clusters_changes() -> None:
    live = LiveSessions()
    stuck, healthy = SlowSession(), FakeSession()
    live.register("clu_stuck", stuck)
    live.register(CLUSTER, healthy)
    dispatcher = Dispatcher(live, FakeRenderer(("deployment.desired", desired())))

    dispatcher.submit(DeclaredChange(cluster_id="clu_stuck", kind="deployment", uid=DEPLOYMENT))
    dispatcher.submit(change())

    await until(lambda: healthy.sent)
    await dispatcher.close()


async def test_hints_for_one_object_waiting_together_are_sent_once() -> None:
    live = LiveSessions()
    session = FakeSession()
    live.register(CLUSTER, session)
    renderer = FakeRenderer(("deployment.desired", desired()))
    dispatcher = Dispatcher(live, renderer)

    for generation in range(5):
        dispatcher.submit(DeclaredChange(cluster_id=CLUSTER, kind="deployment", uid=DEPLOYMENT, generation=generation))

    await until(lambda: session.sent)
    await dispatcher.close()
    assert len(session.sent) == 1
    assert renderer.calls[0].generation == 4, "the newest hint is the one that is rendered"


async def test_a_hint_for_a_cluster_this_process_does_not_serve_starts_no_worker() -> None:
    dispatcher = Dispatcher(LiveSessions(), FakeRenderer(None))
    dispatcher.submit(change())
    assert dispatcher._workers == {}


async def test_hints_past_the_bound_are_dropped_rather_than_held() -> None:
    live = LiveSessions()
    live.register("clu_stuck", SlowSession())
    dispatcher = Dispatcher(live, FakeRenderer(("deployment.desired", desired())), max_pending=2)

    for index in range(5):
        dispatcher.submit(DeclaredChange(cluster_id="clu_stuck", kind="deployment", uid=f"dep_{index}"))

    assert dispatcher._queued <= 2
    await dispatcher.close()
    assert dispatcher._queued == 0
