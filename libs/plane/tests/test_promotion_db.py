"""Promotion against a real Postgres, because this is the path that can restart a model.

Everything else in the discovered-workload module is inert by construction. Promotion is the
one call that writes declared state, so the guards on it are worth exercising against a
database rather than against a compiled statement: a refusal that does not fire is a rollout.
"""

import uuid

import pytest

from sqlalchemy import text

from app.core import ids
from app.ingestion.sink import DatabaseSink
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.ingestion.desired import stream_desired
from app.modules.discovered.schemas import PromoteRequest
from app.modules.discovered.services import DiscoveredWorkloadService

from tests.conftest import CLUSTER
from tests.conftest import fetch
from tests.test_ingestion_db import observed


pytestmark = pytest.mark.database


FINGERPRINT = "sha256:9f2c1b7d4e6a08"


async def seed_demand(sessions) -> None:
    """A tenant, project, priority class, artifact and recipe — the minimum a deployment needs.

    Truncated first: the shared `sessions` fixture resets the supply side, and the
    binding and demand tables outlive it.
    """
    async with sessions.begin() as db:
        await db.execute(
            text("TRUNCATE bind_tenant, bind_priority_class, demand_artifact, demand_recipe RESTART IDENTITY CASCADE")
        )
        tenant = uuid.uuid4()
        project = uuid.uuid4()
        priority = uuid.uuid4()
        artifact = uuid.uuid4()
        await db.execute(
            text("insert into bind_tenant (id, uid, slug, name) values (:id, :uid, 'acme', 'Acme')"),
            {"id": tenant, "uid": ids.new_uid(ids.TENANT)},
        )
        await db.execute(
            text(
                "insert into bind_priority_class (id, uid, name, value, is_default)"
                " values (:id, :uid, 'production', 100, true)"
            ),
            {"id": priority, "uid": ids.new_uid(ids.PRIORITY_CLASS)},
        )
        await db.execute(
            text(
                "insert into bind_project (id, uid, tenant_id, slug, name, default_priority_class_id)"
                " values (:id, :uid, :tenant, 'core', 'Core', :priority)"
            ),
            {"id": project, "uid": ids.new_uid(ids.PROJECT), "tenant": tenant, "priority": priority},
        )
        await db.execute(
            text(
                "insert into demand_artifact (id, uid, digest, name, kind)"
                " values (:id, :uid, 'sha256:abc', 'llama-70b', 'weights')"
            ),
            {"id": artifact, "uid": ids.new_uid(ids.ARTIFACT)},
        )
        await db.execute(
            text(
                "insert into demand_recipe"
                " (id, uid, artifact_id, name, version, engine, engine_version, context_length, device_class)"
                " values (:id, :uid, :artifact, 'llama-70b-vllm', 1, 'vllm', '0.9.1', 8192, 'h100-80gb-sxm')"
            ),
            {"id": uuid.uuid4(), "uid": ids.new_uid(ids.RECIPE), "artifact": artifact},
        )


async def sighted(sessions) -> DatabaseSink:
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("workload.discovered", spec_fingerprint=FINGERPRINT))
    return sink


def request(**overrides) -> PromoteRequest:
    fields = {
        "recipe": "llama-70b-vllm",
        "project": "core",
        "fingerprint": FINGERPRINT,
    }
    fields.update(overrides)
    return PromoteRequest(**fields)


async def test_promotion_creates_a_deployment_pinned_to_what_is_running(sessions, cluster) -> None:
    await seed_demand(sessions)
    await sighted(sessions)

    async with sessions() as db:
        deployment = await DiscoveredWorkloadService(db, principal="admin").promote("llama-70b-vllm", request())

    assert deployment.name == "llama-70b-vllm"
    rows = await fetch(sessions, "select adopted_workload_uid, adopted_fingerprint, generation from demand_deployment")
    assert rows == [("dsw_01j9x2ffffffffffffffffffff", FINGERPRINT, 1)]


async def test_promotion_adopts_the_replica_count_it_found(sessions, cluster) -> None:
    """The fixture reports two replicas. Promoting must not ask for a different number.

    A promotion that defaulted to one replica would scale a live two-replica
    workload down as a side effect of being catalogued.
    """
    await seed_demand(sessions)
    await sighted(sessions)

    async with sessions() as db:
        deployment = await DiscoveredWorkloadService(db).promote("llama-70b-vllm", request())

    assert (deployment.min_replicas, deployment.desired_replicas, deployment.max_replicas) == (2, 2, 2)


async def test_promotion_is_refused_when_the_workload_changed(sessions, cluster) -> None:
    """The gate. A digest the caller did not see is a spec nobody looked at."""
    await seed_demand(sessions)
    await sighted(sessions)

    async with sessions() as db:
        with pytest.raises(Conflict):
            await DiscoveredWorkloadService(db).promote("llama-70b-vllm", request(fingerprint="sha256:something-else"))

    assert await fetch(sessions, "select count(*) from demand_deployment") == [(0,)]


async def test_promotion_is_refused_without_a_digest_to_pin_to(sessions, cluster) -> None:
    """An operator too old to publish a digest can be listed but not promoted.

    Adopting with nothing to compare against would leave a deployment the cluster
    can never verify, and an unverifiable adoption is one a reconciler would
    eventually apply over.
    """
    await seed_demand(sessions)
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("workload.discovered", spec_fingerprint=""))

    async with sessions() as db:
        with pytest.raises(Invalid):
            await DiscoveredWorkloadService(db).promote("llama-70b-vllm", request())


async def test_a_workload_is_promoted_once(sessions, cluster) -> None:
    await seed_demand(sessions)
    await sighted(sessions)

    async with sessions() as db:
        await DiscoveredWorkloadService(db).promote("llama-70b-vllm", request())
    async with sessions() as db:
        with pytest.raises(Conflict):
            await DiscoveredWorkloadService(db).promote("llama-70b-vllm", request(name="llama-again"))

    assert await fetch(sessions, "select count(*) from demand_deployment") == [(1,)]


async def test_a_promoted_deployment_streams_down_as_an_adoption(sessions, cluster) -> None:
    """The half that reaches the cluster.

    Without the adopt block the operator sees an ordinary deployment, renders it,
    and applies the render over a workload that is already serving.
    """
    await seed_demand(sessions)
    await sighted(sessions)
    async with sessions() as db:
        await DiscoveredWorkloadService(db).promote("llama-70b-vllm", request())

    async with sessions() as db:
        frames = [item async for item in stream_desired(db, CLUSTER, "demand")]
    desired = [payload for kind, payload in frames if kind == "deployment.desired"]
    assert len(desired) == 1
    adopt = desired[0].adopt
    assert adopt is not None
    assert (adopt.namespace, adopt.kind, adopt.name) == ("serving", "deployment", "llama-70b-vllm")
    assert adopt.spec_fingerprint == FINGERPRINT


async def test_an_ordinary_deployment_carries_no_adoption(sessions, cluster) -> None:
    await seed_demand(sessions)
    await sighted(sessions)
    async with sessions() as db:
        await DiscoveredWorkloadService(db).promote("llama-70b-vllm", request())
        await db.execute(text("update demand_deployment set adopted_workload_uid = null"))
        await db.commit()

    async with sessions() as db:
        frames = [item async for item in stream_desired(db, CLUSTER, "demand")]
    desired = [payload for kind, payload in frames if kind == "deployment.desired"]
    assert desired and desired[0].adopt is None
