"""Placement against a real Postgres, because the snapshot is the part that can be wrong.

The engine is pure and already covered without a database (`test_placement.py`). What that
suite cannot see is the read that feeds it: whether a held device really comes back held,
whether a quota's draw is counted from live allocation rows, whether an unreachable cluster
reaches the engine as unreachable. Every one of those is a join, and a join that quietly
returns nothing makes an empty cluster look full or a full one look empty.

So this suite builds supply by hand, asks placement, and then asks it again through the one
caller that writes: creating a deployment with no cluster named.
"""

import uuid

import pytest

from sqlalchemy import text

from app.core import ids
from app.core.exceptions import Conflict
from app.modules.placement.schemas import PlacementPreviewRequest
from app.modules.placement.services import PlacementService
from app.modules.deployments.schemas import DeploymentCreateRequest
from app.modules.deployments.services import DeploymentService

from tests.conftest import fetch


pytestmark = pytest.mark.database


DEVICE_CLASS = "h100-80gb-sxm"


async def seed_demand(sessions) -> None:
    """A tenant, project, priority class, artifact and recipe asking for four devices."""
    async with sessions.begin() as db:
        await db.execute(
            text(
                "TRUNCATE bind_tenant, bind_priority_class, bind_policy, demand_artifact, demand_recipe"
                " RESTART IDENTITY CASCADE"
            )
        )
        tenant, project, priority, artifact = (uuid.uuid4() for _ in range(4))
        await db.execute(
            text("insert into bind_tenant (id, uid, slug, name) values (:id, :uid, 'acme', 'Acme')"),
            {"id": tenant, "uid": ids.new_uid(ids.TENANT)},
        )
        await db.execute(
            text(
                "insert into bind_priority_class (id, uid, name, value, is_default, preempts)"
                " values (:id, :uid, 'production', 100, true, false)"
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
                " (id, uid, artifact_id, name, version, engine, engine_version, context_length,"
                "  device_class, device_count)"
                " values (:id, :uid, :artifact, 'llama-70b-vllm', 1, 'vllm', '0.9.1', 8192, :cls, 4)"
            ),
            {"id": uuid.uuid4(), "uid": ids.new_uid(ids.RECIPE), "artifact": artifact, "cls": DEVICE_CLASS},
        )


async def seed_supply(sessions, name: str, devices: int, phase: str = "active", reachable: bool = True) -> uuid.UUID:
    """One cluster with a node and some free, healthy accelerators."""
    async with sessions.begin() as db:
        cluster = (
            await db.execute(
                text(
                    "insert into supply_cluster (uid, name, region, phase)"
                    " values (:uid, :name, 'eu-west-1', :phase) returning id"
                ),
                {"uid": ids.new_uid(ids.CLUSTER), "name": name, "phase": phase},
            )
        ).scalar_one()
        await db.execute(
            text("insert into supply_cluster_sync_state (cluster_id, reachable) values (:cluster, :reachable)"),
            {"cluster": cluster, "reachable": reachable},
        )
        node = (
            await db.execute(
                text(
                    "insert into supply_node (uid, cluster_id, name, ready, condition, cordoned) "
                    "values (:uid, :cluster, :name, true, 'ready', false) returning id"
                ),
                {"uid": ids.new_uid(ids.NODE), "cluster": cluster, "name": f"{name}-node-0"},
            )
        ).scalar_one()
        for index in range(devices):
            await db.execute(
                text(
                    "insert into supply_device"
                    " (uid, cluster_id, node_id, device_uuid, device_index, device_class, memory_mib,"
                    "  health, allocatable)"
                    " values (:uid, :cluster, :node, :duid, :index, :cls, 81920, 'healthy', true)"
                ),
                {
                    "uid": ids.new_uid(ids.DEVICE),
                    "cluster": cluster,
                    "node": node,
                    "duid": f"GPU-{name}-{index}",
                    "index": index,
                    "cls": DEVICE_CLASS,
                },
            )
        return cluster


def create(**overrides) -> DeploymentCreateRequest:
    fields = {"recipe": "llama-70b-vllm", "project": "core", "name": "llama-prod"}
    fields.update(overrides)
    return DeploymentCreateRequest(**fields)


async def test_placement_takes_the_tightest_fit_of_the_clusters_that_work(sessions) -> None:
    """Best fit, not most free — the roomiest cluster is only the tiebreak.

    Filling the four-device cluster leaves the eight-device one whole, and a large
    contiguous block kept whole is what the next big request needs.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "small", devices=4)
    await seed_supply(sessions, "large", devices=8)

    async with sessions() as db:
        decision = await PlacementService(db).preview(PlacementPreviewRequest(recipe="llama-70b-vllm", project="core"))

    assert decision.placed
    assert decision.cluster_name == "small"
    assert {v.cluster_name for v in decision.verdicts} == {"small", "large"}


async def test_a_deployment_with_no_cluster_lands_where_placement_put_it(sessions) -> None:
    await seed_demand(sessions)
    await seed_supply(sessions, "alpha", devices=8)

    async with sessions() as db:
        deployment = await DeploymentService(db).create(create())

    assert deployment.cluster_id is not None
    assert deployment.workload.phase == "placed"
    assert deployment.workload.placed_at is not None
    assert "alpha" in (deployment.workload.reason or "")
    rows = await fetch(sessions, "select name from supply_cluster where uid = :uid", {"uid": deployment.cluster_id})
    assert rows == [("alpha",)]


async def test_a_cluster_with_no_live_session_is_not_a_placement_target(sessions) -> None:
    await seed_demand(sessions)
    await seed_supply(sessions, "dark", devices=8, reachable=False)

    async with sessions() as db:
        with pytest.raises(Conflict) as raised:
            await DeploymentService(db).create(create())

    codes = {r["code"] for v in raised.value.detail["verdicts"] for r in v["reasons"]}
    assert "cluster_unreachable" in codes
    assert await fetch(sessions, "select uid from demand_deployment") == []


async def test_the_reason_a_cluster_lost_survives_into_the_refusal(sessions) -> None:
    """A rejection carries the numbers, not just a verdict.

    Four devices are asked for and two exist; an operator reading the error should
    be able to see that without opening the console.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "tight", devices=2)

    async with sessions() as db:
        with pytest.raises(Conflict) as raised:
            await DeploymentService(db).create(create())

    verdict = raised.value.detail["verdicts"][0]
    assert verdict["free_devices"] == 2
    assert raised.value.detail["request"]["device_count"] == 4
    assert any(r["code"] == "insufficient_capacity" for r in verdict["reasons"])


async def test_naming_a_cluster_skips_placement_entirely(sessions) -> None:
    """The override is an override. An unreachable, empty cluster is still accepted."""
    await seed_demand(sessions)
    await seed_supply(sessions, "manual", devices=0, phase="draining", reachable=False)

    async with sessions() as db:
        deployment = await DeploymentService(db).create(create(cluster="manual"))

    assert deployment.workload.reason == "cluster manual was named by the caller"


async def test_replicas_multiply_the_device_shape(sessions) -> None:
    """Two replicas of a four-device recipe need eight in one cluster, not four."""
    await seed_demand(sessions)
    await seed_supply(sessions, "six", devices=6)

    async with sessions() as db:
        with pytest.raises(Conflict):
            await DeploymentService(db).create(create(min_replicas=2, desired_replicas=2, max_replicas=2))

    async with sessions() as db:
        deployment = await DeploymentService(db).create(create(name="single"))

    assert deployment.name == "single"


async def test_a_device_held_by_another_workload_is_not_free(sessions) -> None:
    """The hold has to come back from the join, or a full cluster reads as empty."""
    await seed_demand(sessions)
    cluster = await seed_supply(sessions, "busy", devices=4)
    async with sessions.begin() as db:
        project = (await db.execute(text("select id from bind_project"))).scalar_one()
        priority = (await db.execute(text("select id from bind_priority_class"))).scalar_one()
        workload = (
            await db.execute(
                text(
                    "insert into demand_workload (uid, kind, project_id, cluster_id, priority_class_id)"
                    " values (:uid, 'service', :project, :cluster, :priority) returning id"
                ),
                {
                    "uid": ids.new_uid(ids.WORKLOAD),
                    "project": project,
                    "cluster": cluster,
                    "priority": priority,
                },
            )
        ).scalar_one()
        allocation = (
            await db.execute(
                text(
                    "insert into bind_allocation"
                    " (uid, cluster_id, workload_id, project_id, device_class, device_count)"
                    " values (:uid, :cluster, :workload, :project, :cls, 1) returning id"
                ),
                {
                    "uid": ids.new_uid(ids.ALLOCATION),
                    "cluster": cluster,
                    "workload": workload,
                    "project": project,
                    "cls": DEVICE_CLASS,
                },
            )
        ).scalar_one()
        device = (await db.execute(text("select id from supply_device limit 1"))).scalar_one()
        await db.execute(
            text("insert into bind_allocation_device (allocation_id, device_id) values (:a, :d)"),
            {"a": allocation, "d": device},
        )

    async with sessions() as db:
        decision = await PlacementService(db).preview(PlacementPreviewRequest(recipe="llama-70b-vllm", project="core"))

    assert not decision.placed
    assert decision.verdicts[0].free_devices == 3


async def test_a_quota_ceiling_rules_out_a_cluster_that_has_the_devices(sessions) -> None:
    await seed_demand(sessions)
    await seed_supply(sessions, "roomy", devices=8)
    async with sessions.begin() as db:
        project = (await db.execute(text("select id from bind_project"))).scalar_one()
        await db.execute(
            text(
                "insert into bind_quota (uid, project_id, device_class, guaranteed_devices, limit_devices)"
                " values (:uid, :project, :cls, 2, 2)"
            ),
            {"uid": ids.new_uid(ids.QUOTA), "project": project, "cls": DEVICE_CLASS},
        )

    async with sessions() as db:
        with pytest.raises(Conflict) as raised:
            await DeploymentService(db).create(create())

    codes = {r["code"] for v in raised.value.detail["verdicts"] for r in v["reasons"]}
    assert "quota_exhausted" in codes


async def test_a_cordoned_node_offers_no_capacity(sessions) -> None:
    """The cluster will not schedule onto a cordoned node, so neither may the plane.

    The failure this guards is quiet: an autoscaler cordons an instance moments
    before terminating it, and until placement read the flag those devices stayed
    candidates. The plane would pick the cluster on capacity the cluster had
    already withdrawn, and the workload would bounce off the local scheduler.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "cordoned", devices=8)
    async with sessions.begin() as db:
        await db.execute(text("update supply_node set cordoned = true"))

    async with sessions() as db:
        decision = await PlacementService(db).preview(
            PlacementPreviewRequest(recipe="llama-70b-vllm", project="core")
        )

    assert not decision.placed
    assert decision.verdicts[0].free_devices == 0


async def test_a_not_ready_node_offers_no_capacity(sessions) -> None:
    """Cordon and readiness are different facts, and either one is disqualifying."""
    await seed_demand(sessions)
    await seed_supply(sessions, "sick", devices=8)
    async with sessions.begin() as db:
        await db.execute(text("update supply_node set ready = false, condition = 'not_ready'"))

    async with sessions() as db:
        decision = await PlacementService(db).preview(
            PlacementPreviewRequest(recipe="llama-70b-vllm", project="core")
        )

    assert not decision.placed
    assert decision.verdicts[0].free_devices == 0
