"""Promotion against a real Postgres, because this is the path that can restart a model.

Everything else in the discovered-workload module is inert by construction. Promotion is the
one call that writes declared state, so the guards on it are worth exercising against a
database, not against a compiled statement: a refusal that does not fire is a rollout.
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
from app.modules.discovered.schemas import DecisionRequest
from app.modules.deployments.schemas import DeploymentAuthorityRequest
from app.modules.discovered.services import DiscoveredWorkloadService
from app.modules.deployments.services import DeploymentService

from tests.conftest import CLUSTER
from tests.conftest import fetch
from tests.test_ingestion_db import observed
from tests.test_ingestion_db import apply_supply


pytestmark = pytest.mark.database


FINGERPRINT = "sha256:9f2c1b7d4e6a08"


async def seed_demand(sessions) -> None:
    """A tenant, project, priority class and artifact: the minimum a deployment needs.

    The artifact is the only part of the serving configuration a promotion is given.
    Everything else is read off the sighting, so there is no second row to seed here
    and no ordering between them to get wrong.

    Truncated first: the shared `sessions` fixture resets the supply side, and the
    binding and demand tables outlive it.
    """
    async with sessions.begin() as db:
        await db.execute(text("TRUNCATE bind_tenant, bind_priority_class, demand_artifact RESTART IDENTITY CASCADE"))
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
                " values (:id, :uid, 'sha256:abc', 'gpt-oss-120b', 'weights')"
            ),
            {"id": artifact, "uid": ids.new_uid(ids.ARTIFACT)},
        )


async def sighted(sessions) -> DatabaseSink:
    """The sighting, over the supply it is running on.

    The nodes come first because the serving block a promotion declares is read off
    the sighting, and the device class in it is read off the devices of the nodes the
    workload holds: the cluster is the only authority on what its own hardware is.
    A sighting with no supply under it reads as a workload nobody can say enough
    about to declare.
    """
    sink = DatabaseSink(sessions)
    await apply_supply(sink)
    await sink.apply_observed(observed("workload.discovered", spec_fingerprint=FINGERPRINT))
    return sink


def request(**overrides) -> PromoteRequest:
    fields = {
        "artifact": "gpt-oss-120b",
        "project": "core",
        "fingerprint": FINGERPRINT,
    }
    fields.update(overrides)
    return PromoteRequest(**fields)


async def test_promotion_creates_a_workload_pinned_to_what_is_running(sessions, cluster) -> None:
    """One Workload and its one Deployment, minted together and pinned to the cluster.

    Capped at one cluster because an adopted object records something that already
    exists in one place; expanding it elsewhere would start a second copy.
    """
    await seed_demand(sessions)
    await sighted(sessions)

    async with sessions() as db:
        workload = await DiscoveredWorkloadService(db, principal="admin").promote("gpt-oss-120b-vllm", request())

    assert workload.name == "gpt-oss-120b-vllm"
    assert workload.max_clusters == 1
    assert workload.pinned_cluster_id == CLUSTER
    assert [d.name for d in workload.deployments] == ["gpt-oss-120b-vllm"]
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
        workload = await DiscoveredWorkloadService(db).promote("gpt-oss-120b-vllm", request())

    assert (workload.scale.min, workload.scale.desired, workload.scale.max) == (2, 2, 2)
    share = workload.deployments[0]
    assert (share.min_replicas, share.desired_replicas, share.max_replicas) == (2, 2, 2)


async def test_promotion_is_refused_when_the_workload_changed(sessions, cluster) -> None:
    """The gate. A digest the caller did not see is a spec nobody looked at."""
    await seed_demand(sessions)
    await sighted(sessions)

    async with sessions() as db:
        with pytest.raises(Conflict):
            await DiscoveredWorkloadService(db).promote(
                "gpt-oss-120b-vllm", request(fingerprint="sha256:something-else")
            )

    assert await fetch(sessions, "select count(*) from demand_deployment") == [(0,)]


async def test_promotion_is_refused_without_a_digest_to_pin_to(sessions, cluster) -> None:
    """An operator too old to publish a digest can be listed but not promoted.

    Adopting with nothing to compare against would leave a deployment the cluster
    can never verify, and an unverifiable adoption is one a reconciler would
    eventually apply over.
    """
    await seed_demand(sessions)
    sink = DatabaseSink(sessions)
    await apply_supply(sink)
    await sink.apply_observed(observed("workload.discovered", spec_fingerprint=""))

    async with sessions() as db:
        with pytest.raises(Invalid):
            await DiscoveredWorkloadService(db).promote("gpt-oss-120b-vllm", request())


async def test_a_workload_is_promoted_once(sessions, cluster) -> None:
    await seed_demand(sessions)
    await sighted(sessions)

    async with sessions() as db:
        await DiscoveredWorkloadService(db).promote("gpt-oss-120b-vllm", request())
    async with sessions() as db:
        with pytest.raises(Conflict):
            await DiscoveredWorkloadService(db).promote("gpt-oss-120b-vllm", request(name="gpt-oss-again"))

    assert await fetch(sessions, "select count(*) from demand_deployment") == [(1,)]


async def test_a_promoted_deployment_streams_down_as_an_adoption(sessions, cluster) -> None:
    """The half that reaches the cluster.

    Without the adopt block the operator sees an ordinary deployment, renders it,
    and applies the render over a workload that is already serving.
    """
    await seed_demand(sessions)
    await sighted(sessions)
    async with sessions() as db:
        await DiscoveredWorkloadService(db).promote("gpt-oss-120b-vllm", request())

    async with sessions() as db:
        frames = [item async for item in stream_desired(db, CLUSTER, "demand")]
    desired = [payload for kind, payload in frames if kind == "deployment.desired"]
    assert len(desired) == 1
    adopt = desired[0].adopt
    assert adopt is not None
    assert (adopt.namespace, adopt.kind, adopt.name) == ("serving", "deployment", "gpt-oss-120b-vllm")
    assert adopt.spec_fingerprint == FINGERPRINT


async def test_an_adoption_survives_its_sighting_being_pruned(sessions, cluster) -> None:
    """The sighting is observed state and the adoption is not.

    A supply snapshot that stops mentioning a workload prunes the sighting row, and
    for as long as the wire's adopt block was joined onto that row every frame
    rendered inside the window went down without one. An operator receiving one
    stops recognising the deployment as adopted and never reads a rollout for it
    again. Nothing re-dispatches when the sighting returns, so a serving
    workload reads `placed · 0/1` until somebody writes to it by hand.
    """
    await seed_demand(sessions)
    await sighted(sessions)
    async with sessions() as db:
        await DiscoveredWorkloadService(db).promote("gpt-oss-120b-vllm", request(name="renamed-on-purpose"))
        await db.execute(text("delete from demand_discovered_workload"))
        await db.commit()

    async with sessions() as db:
        frames = [item async for item in stream_desired(db, CLUSTER, "demand")]
    desired = [payload for kind, payload in frames if kind == "deployment.desired"]
    assert len(desired) == 1
    adopt = desired[0].adopt
    assert adopt is not None
    assert (adopt.namespace, adopt.kind) == ("serving", "deployment")
    assert adopt.spec_fingerprint == FINGERPRINT
    # The live object's name, not the deployment's. Promotion accepts a rename and the
    # operator still has to be pointed at the object that actually exists.
    assert adopt.name == "gpt-oss-120b-vllm"
    assert desired[0].name == "renamed-on-purpose"


async def test_an_ordinary_deployment_carries_no_adoption(sessions, cluster) -> None:
    await seed_demand(sessions)
    await sighted(sessions)
    async with sessions() as db:
        await DiscoveredWorkloadService(db).promote("gpt-oss-120b-vllm", request())
        await db.execute(text("update demand_deployment set adopted_workload_uid = null"))
        await db.commit()

    async with sessions() as db:
        frames = [item async for item in stream_desired(db, CLUSTER, "demand")]
    desired = [payload for kind, payload in frames if kind == "deployment.desired"]
    assert desired and desired[0].adopt is None


async def test_a_promoted_deployment_is_observed_not_managed(sessions, cluster) -> None:
    """Promotion catalogues. It does not hand the plane the wheel (ADR 0009)."""
    await seed_demand(sessions)
    await sighted(sessions)

    async with sessions() as db:
        workload = await DiscoveredWorkloadService(db).promote("gpt-oss-120b-vllm", request())

    assert [d.authority for d in workload.deployments] == ["observed"]


async def test_taking_management_needs_the_digest_of_what_is_running(sessions, cluster) -> None:
    """The same gate promotion uses, at the moment it starts to matter.

    Promotion is free; taking management is the act that can render over a live
    pod template, so it re-asks the question instead of trusting an answer given
    when the deployment was catalogued.
    """
    await seed_demand(sessions)
    await sighted(sessions)

    async with sessions() as db:
        await DiscoveredWorkloadService(db).promote("gpt-oss-120b-vllm", request())

    async with sessions() as db:
        svc = DeploymentService(db)
        with pytest.raises(Invalid):
            await svc.set_authority("gpt-oss-120b-vllm", DeploymentAuthorityRequest(authority="managed"))
        with pytest.raises(Conflict):
            await svc.set_authority(
                "gpt-oss-120b-vllm",
                DeploymentAuthorityRequest(authority="managed", fingerprint="sha256:not-what-is-running"),
            )

    assert await fetch(sessions, "select authority from demand_deployment") == [("observed",)]


async def test_management_is_taken_and_given_back(sessions, cluster) -> None:
    await seed_demand(sessions)
    await sighted(sessions)

    async with sessions() as db:
        await DiscoveredWorkloadService(db).promote("gpt-oss-120b-vllm", request())

    async with sessions() as db:
        taken = await DeploymentService(db).set_authority(
            "gpt-oss-120b-vllm", DeploymentAuthorityRequest(authority="managed", fingerprint=FINGERPRINT)
        )
    assert taken.authority == "managed"

    async with sessions() as db:
        released = await DeploymentService(db).set_authority(
            "gpt-oss-120b-vllm", DeploymentAuthorityRequest(authority="observed")
        )
    assert released.authority == "observed", "letting go writes nothing and needs nothing"


async def test_authority_reaches_the_cluster_on_the_desired_stream(sessions, cluster) -> None:
    """The operator gates on this field, so a transition nobody streams is a no-op."""
    await seed_demand(sessions)
    await sighted(sessions)

    async with sessions() as db:
        await DiscoveredWorkloadService(db).promote("gpt-oss-120b-vllm", request())
    async with sessions() as db:
        await DeploymentService(db).set_authority(
            "gpt-oss-120b-vllm", DeploymentAuthorityRequest(authority="managed", fingerprint=FINGERPRINT)
        )

    async with sessions() as db:
        frames = [
            payload async for kind, payload in stream_desired(db, CLUSTER, "demand") if kind == "deployment.desired"
        ]

    assert [f.authority for f in frames] == ["managed"]
    assert frames[0].adopt is not None, "provenance survives taking management"


async def test_a_dismissed_sighting_leaves_the_list_and_survives_the_next_observation(sessions, cluster) -> None:
    """The row stays and the entry goes. Ingestion is still the only thing writing sightings."""
    sink = await sighted(sessions)

    async with sessions() as db:
        await DiscoveredWorkloadService(db, principal="admin").dismiss("gpt-oss-120b-vllm", DecisionRequest())

    async with sessions() as db:
        assert (await DiscoveredWorkloadService(db).list()).items == []

    await sink.apply_observed(observed("workload.discovered", spec_fingerprint=FINGERPRINT))
    async with sessions() as db:
        svc = DiscoveredWorkloadService(db)
        assert (await svc.list()).items == [], "an observation does not undo a dismissal"
        listed = (await svc.list(status="dismissed")).items
    assert [w.name for w in listed] == ["gpt-oss-120b-vllm"]
    assert (await fetch(sessions, "select count(*) from demand_discovered_workload"))[0][0] == 1


async def test_restoring_puts_it_back_with_no_decision_on_it(sessions, cluster) -> None:
    await sighted(sessions)

    async with sessions() as db:
        await DiscoveredWorkloadService(db).dismiss("gpt-oss-120b-vllm", DecisionRequest())
    async with sessions() as db:
        restored = await DiscoveredWorkloadService(db).restore("gpt-oss-120b-vllm")
    assert restored.status == "new"

    async with sessions() as db:
        assert [w.name for w in (await DiscoveredWorkloadService(db).list()).items] == ["gpt-oss-120b-vllm"]


async def test_a_promoted_sighting_cannot_be_dismissed(sessions, cluster) -> None:
    await seed_demand(sessions)
    await sighted(sessions)

    async with sessions() as db:
        await DiscoveredWorkloadService(db).promote("gpt-oss-120b-vllm", request())

    async with sessions() as db:
        with pytest.raises(Conflict):
            await DiscoveredWorkloadService(db).dismiss("gpt-oss-120b-vllm", DecisionRequest())
