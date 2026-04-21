"""The threshold table against a real Postgres: the read, the scope index, and the pass.

The table is created by the fixture rather than by the migration chain, because the
migration for `ops_alert_threshold` is written by hand and is not in the chain yet.
Everything else here is the real thing — the real `SELECT`, the real `NUMERIC`, the
real unique index — and the metrics store is the only stub, which is the point of
`app/reactor/measures.py` existing at all.
"""

import uuid

import pytest

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core import ids
from app.reactor import rules
from app.db.models import AlertThreshold
from app.reactor.rules import Threshold
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.reactor.measures import Reading
from app.reactor.conditions import evaluate
from app.reactor.conditions import node_subjects
from app.reactor.conditions import metric_thresholds
from app.reactor.conditions import declared_thresholds
from app.modules.alerts.schemas import AlertThresholdCreateRequest
from app.modules.alerts.schemas import AlertThresholdUpdateRequest
from app.modules.alerts.services import AlertThresholdService

from tests.conftest import fetch


pytestmark = pytest.mark.database

TEMPERATURE = "node.gpu_temperature_high"
NODE = "nod_01j9x2cccccccccccccccccccc"


class StubReader:
    """One number, whatever is asked. The store is not what these tests are about."""

    def __init__(self, value: float | None) -> None:
        self.value = value

    async def read(self, query) -> Reading:
        return Reading(query.metric, value=self.value, withheld=None if self.value is not None else "no series")


@pytest.fixture
async def thresholds(sessions):
    """Creates `ops_alert_threshold` and takes it away again.

    Dropped rather than left behind so that a suite run twice sees the same
    database twice, and so that the day the migration lands this fixture stops
    being load-bearing without anything else changing.
    """
    async with sessions() as db:
        await db.run_sync(lambda s: AlertThreshold.__table__.create(s.get_bind(), checkfirst=True))
        await db.commit()
    yield
    async with sessions() as db:
        await db.run_sync(lambda s: AlertThreshold.__table__.drop(s.get_bind(), checkfirst=True))
        await db.commit()


async def seed_node(sessions, cluster: uuid.UUID) -> None:
    async with sessions.begin() as db:
        await db.execute(
            text(
                "UPDATE supply_cluster SET phase = 'active' WHERE id = :cluster",
            ),
            {"cluster": cluster},
        )
        await db.execute(
            text(
                "INSERT INTO supply_node (id, uid, cluster_id, name, condition)"
                " VALUES (:id, :uid, :cluster, 'gpu-1', 'ready')"
            ),
            {"id": uuid.uuid4(), "uid": NODE, "cluster": cluster},
        )


async def declare(sessions, **kwargs) -> str:
    uid = ids.new_uid(ids.ALERT_THRESHOLD)
    fields = {
        "uid": uid,
        "rule": TEMPERATURE,
        "subject_type": "node",
        "subject_uid": None,
        "dimensions": {},
        "value": 87,
        "enabled": True,
        "reason": "clause 6 of the acceptance standard",
    }
    async with sessions.begin() as db:
        db.add(AlertThreshold(**{**fields, **kwargs}))
    return uid


async def test_a_declared_row_comes_back_as_plain_floats(sessions, thresholds):
    await declare(sessions, value=87.5)
    async with sessions() as db:
        declared = await declared_thresholds(db)
    assert len(declared) == 1
    assert declared[0].value == 87.5
    assert isinstance(declared[0].value, float)
    assert declared[0].dimensions == {}
    assert declared[0].reason


async def test_a_disabled_commitment_is_not_evaluated(sessions, thresholds):
    """Turning one off is not deleting the record that it existed."""
    await declare(sessions, enabled=False)
    async with sessions() as db:
        assert await declared_thresholds(db) == []


async def test_a_row_naming_a_rule_this_plane_does_not_have_is_left_alone(sessions, thresholds):
    await declare(sessions, rule="node.invented")
    async with sessions() as db:
        assert await declared_thresholds(db) == []


async def test_two_fleet_defaults_for_one_rule_cannot_both_exist(sessions, thresholds):
    """What `NULLS NOT DISTINCT` is for.

    Under the default rule two NULLs are distinct, so both rows would be admitted
    and which commitment the reactor read would be whichever the planner returned
    that day.
    """
    await declare(sessions, value=87)
    with pytest.raises(IntegrityError):
        await declare(sessions, value=70)


async def test_a_scoped_row_sits_beside_the_fleet_default(sessions, cluster, thresholds):
    await seed_node(sessions, cluster)
    await declare(sessions, value=87)
    await declare(sessions, value=60, subject_uid=NODE)
    async with sessions() as db:
        declared = await declared_thresholds(db)
    assert rules.resolve_threshold(declared, TEMPERATURE, NODE).value == 60
    assert rules.resolve_threshold(declared, TEMPERATURE, "nod_other").value == 87


async def test_nodes_are_enumerated_with_the_cluster_that_answers_for_them(sessions, cluster):
    await seed_node(sessions, cluster)
    async with sessions() as db:
        subjects = await node_subjects(db)
    assert [subject.uid for subject, _labels in subjects] == [NODE]
    assert subjects[0][0].cluster_name == "alpha"
    assert subjects[0][0].cluster_id == cluster


async def test_the_pass_alerts_on_a_real_subject_against_a_real_row(sessions, cluster, thresholds):
    await seed_node(sessions, cluster)
    await declare(sessions, value=87)
    async with sessions() as db:
        intents = await metric_thresholds(db, StubReader(95.0))
    assert len(intents) == 1
    assert intents[0].rule == TEMPERATURE
    assert intents[0].subject_uid == NODE
    assert intents[0].details["threshold"] == 87.0
    assert intents[0].details["observed"] == 95.0


async def test_a_subject_the_store_cannot_answer_for_is_left_alone(sessions, cluster, thresholds):
    await seed_node(sessions, cluster)
    await declare(sessions, value=87)
    async with sessions() as db:
        assert await metric_thresholds(db, StubReader(None)) == []


async def test_the_pass_writes_nothing(sessions, cluster, thresholds):
    """Evaluation is a read. The reactor's only write is the alert row itself."""
    await seed_node(sessions, cluster)
    await declare(sessions, value=87)
    async with sessions() as db:
        await metric_thresholds(db, StubReader(95.0))
        await db.commit()
    rows = await fetch(sessions, "SELECT count(*) AS n FROM ops_alert")
    assert rows[0].n == 0


async def test_the_whole_evaluation_still_runs_when_the_table_is_missing(sessions, cluster):
    """The migration lag, and the reason it is survivable.

    A tick is one transaction, and in Postgres one failed statement aborts all of
    it — so a plane whose migration has not been applied yet would lose every
    condition and every event it was about to write because of the least important
    query in the pass. The savepoint keeps that from being true.
    """
    await seed_node(sessions, cluster)
    async with sessions() as db:
        intents = await evaluate(db, reader=StubReader(95.0))
        await db.execute(text("SELECT 1"))
    assert [intent.rule for intent in intents if intent.rule == TEMPERATURE] == []
    assert any(intent.rule == "cluster.unreachable" for intent in intents)


async def test_a_threshold_row_survives_the_object_it_was_written_about(sessions, cluster, thresholds):
    """No foreign key on `subject_uid`, deliberately: a commitment outlives its subject."""
    await seed_node(sessions, cluster)
    await declare(sessions, value=60, subject_uid=NODE)
    async with sessions.begin() as db:
        await db.execute(text("DELETE FROM supply_node WHERE uid = :uid"), {"uid": NODE})
    async with sessions() as db:
        declared = await declared_thresholds(db)
    assert [t.subject_uid for t in declared] == [NODE]
    assert isinstance(declared[0], Threshold)


async def test_a_commitment_can_be_written_read_amended_and_retired(sessions, thresholds):
    """The declared half, end to end. The reactor's judgement never enters into it."""
    async with sessions() as db:
        svc = AlertThresholdService(db)
        created = await svc.create(
            AlertThresholdCreateRequest(rule=TEMPERATURE, value=87.0, reason="clause 6", lookback="10m")
        )
        assert created.id.startswith(ids.ALERT_THRESHOLD)
        assert created.subject.type == "node"
        assert created.subject.id is None
        assert created.metric == "gpu.temperature"

        amended = await svc.update(created.id, AlertThresholdUpdateRequest(value=80.0, reason="clause 6, revised"))
        assert amended.value == 80.0
        assert amended.reason == "clause 6, revised"
        assert amended.lookback == "10m"

        listed = await svc.list(rule=TEMPERATURE, fleet_default=True)
        assert [item.id for item in listed.items] == [created.id]

        await svc.delete(created.id)
        with pytest.raises(NotFound):
            await svc.get(created.id)


async def test_the_same_commitment_cannot_be_declared_twice(sessions, thresholds):
    async with sessions() as db:
        svc = AlertThresholdService(db)
        await svc.create(AlertThresholdCreateRequest(rule=TEMPERATURE, value=87.0))
        with pytest.raises(Conflict):
            await svc.create(AlertThresholdCreateRequest(rule=TEMPERATURE, value=70.0))


async def test_a_scoped_commitment_and_the_fleet_default_are_two_rows(sessions, thresholds):
    async with sessions() as db:
        svc = AlertThresholdService(db)
        await svc.create(AlertThresholdCreateRequest(rule=TEMPERATURE, value=87.0))
        await svc.create(AlertThresholdCreateRequest(rule=TEMPERATURE, value=60.0, subject_id=NODE))
        scoped = await svc.list(subject=NODE)
        assert [item.value for item in scoped.items] == [60.0]
        assert (await svc.list(fleet_default=True)).count == 1


async def test_disabling_a_commitment_keeps_it(sessions, thresholds):
    async with sessions() as db:
        svc = AlertThresholdService(db)
        created = await svc.create(AlertThresholdCreateRequest(rule=TEMPERATURE, value=87.0))
        await svc.update(created.id, AlertThresholdUpdateRequest(enabled=False))
        assert (await svc.get(created.id)).enabled is False
        assert (await svc.list(enabled=False)).count == 1
    async with sessions() as db:
        assert await declared_thresholds(db) == []
