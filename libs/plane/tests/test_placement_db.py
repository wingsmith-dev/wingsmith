"""Placement against a real Postgres, because the snapshot is the part that can be wrong.

The engine is pure and already covered without a database (`test_placement.py`). What that
suite cannot see is the read that feeds it: whether a held device really comes back held,
whether a quota's draw is counted from live allocation rows, whether an unreachable cluster
reaches the engine as unreachable. Every one of those is a join, and a join that quietly
returns nothing makes an empty cluster look full or a full one look empty.

So this suite builds supply by hand, asks placement, and then asks it again through the one
caller that writes: creating a workload and letting placement expand it into deployments.
"""

import uuid

import pytest

from sqlalchemy import text

from app.core import ids
from app.core.exceptions import Invalid
from app.modules.placement.schemas import PlacementPreviewRequest
from app.modules.workloads.schemas import WorkloadCreateRequest
from app.modules.operations.schemas import OperationCreateRequest
from app.modules.placement.services import PlacementService
from app.modules.workloads.services import WorkloadService
from app.modules.operations.services import OperationService

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


def create(**overrides) -> WorkloadCreateRequest:
    fields = {"recipe": "llama-70b-vllm", "project": "core", "name": "llama-prod"}
    fields.update(overrides)
    return WorkloadCreateRequest(**fields)


def scale(total: int) -> dict:
    """The three totals set to the same number, which is how a fixed-size workload asks."""
    return {"min_scale": total, "desired_scale": total, "max_scale": total}


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


async def test_a_workload_with_no_cluster_is_expanded_onto_the_one_placement_picked(sessions) -> None:
    await seed_demand(sessions)
    await seed_supply(sessions, "alpha", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create())

    assert [d.cluster_name for d in workload.deployments] == ["alpha"]
    assert workload.phase == "placed"
    assert workload.deployments[0].placed_at is not None
    assert workload.scale.placed == 1
    rows = await fetch(
        sessions,
        "select name from supply_cluster where uid = :uid",
        {"uid": workload.deployments[0].cluster_id},
    )
    assert rows == [("alpha",)]


async def test_pack_is_the_default_and_fills_one_cluster_before_opening_another(sessions) -> None:
    """Three replicas of a four-device recipe over two clusters that hold two each.

    Pack is the default so a workload that used to be one deployment stays one; it
    spills into a second cluster only when the first cannot hold the rest.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "first", devices=8)
    await seed_supply(sessions, "second", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(**scale(3)))

    shares = {d.cluster_name: d.desired_replicas for d in workload.deployments}
    assert sum(shares.values()) == 3
    assert sorted(shares.values()) == [1, 2]
    assert workload.scale.placed == 3
    assert workload.scale.unit == "replicas"


async def test_a_share_is_this_clusters_count_and_never_the_total(sessions) -> None:
    """Every deployment's three numbers are its own share, equal to each other."""
    await seed_demand(sessions)
    await seed_supply(sessions, "first", devices=8)
    await seed_supply(sessions, "second", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(**scale(3)))

    for deployment in workload.deployments:
        assert deployment.min_replicas == deployment.desired_replicas == deployment.max_replicas
        assert deployment.desired_replicas < workload.scale.desired


async def test_a_gang_cannot_straddle_clusters(sessions) -> None:
    """Two clusters that could each take half a gang add up to nothing.

    Its ranks are co-scheduled, so a split gang is broken rather than degraded.
    This is a rejection, not a lower-ranked answer.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "half-a", devices=4)
    await seed_supply(sessions, "half-b", devices=4)

    async with sessions() as db:
        with pytest.raises(Invalid):
            await WorkloadService(db).create(create(kind="gang", gang_size=2, **scale(2)))

    assert await fetch(sessions, "select uid from demand_workload") == []
    assert await fetch(sessions, "select uid from demand_deployment") == []


async def test_a_gang_that_fits_whole_is_one_deployment(sessions) -> None:
    await seed_demand(sessions)
    await seed_supply(sessions, "whole", devices=8)
    await seed_supply(sessions, "half", devices=4)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(kind="gang", gang_size=2, **scale(2)))

    assert [d.cluster_name for d in workload.deployments] == ["whole"]
    assert workload.deployments[0].desired_replicas == 2
    assert workload.scale.unit == "ranks"


async def test_a_cluster_with_no_live_session_is_not_a_placement_target(sessions) -> None:
    """Nowhere to put it is a shortfall, not a refusal — the declaration survives."""
    await seed_demand(sessions)
    await seed_supply(sessions, "dark", devices=8, reachable=False)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create())

    assert workload.deployments == []
    assert workload.scale.placed == 0
    assert workload.phase == "pending"
    assert await fetch(sessions, "select uid from demand_deployment") == []


async def test_the_reason_a_cluster_lost_survives_into_the_expansion(sessions) -> None:
    """A shortfall carries the numbers, not just a verdict.

    Four devices are asked for and two exist; an operator reading the expansion
    should be able to see that without opening the console.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "tight", devices=2)

    async with sessions() as db:
        decision = await PlacementService(db).preview(PlacementPreviewRequest(recipe="llama-70b-vllm", project="core"))

    assert not decision.placed
    verdict = decision.verdicts[0]
    assert verdict.free_devices == 2
    shortage = next(r for r in verdict.reasons if r.code == "insufficient_capacity")
    assert shortage.detail["requested_devices"] == 4


async def test_naming_a_cluster_skips_placement_entirely(sessions) -> None:
    """The override is an override. An unreachable, empty cluster is still accepted."""
    await seed_demand(sessions)
    await seed_supply(sessions, "manual", devices=0, phase="draining", reachable=False)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(cluster="manual"))

    assert [d.cluster_name for d in workload.deployments] == ["manual"]
    assert workload.pinned_cluster_id is not None


async def test_replicas_multiply_the_device_shape(sessions) -> None:
    """Two replicas of a four-device recipe need eight devices, not four."""
    await seed_demand(sessions)
    await seed_supply(sessions, "six", devices=6)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(**scale(2)))

    assert workload.scale.placed == 1
    assert workload.scale.desired == 2


async def test_a_device_held_by_another_workload_is_not_free(sessions) -> None:
    """The hold has to come back from the join, or a full cluster reads as empty."""
    await seed_demand(sessions)
    cluster = await seed_supply(sessions, "busy", devices=4)
    async with sessions.begin() as db:
        project = (await db.execute(text("select id from bind_project"))).scalar_one()
        priority = (await db.execute(text("select id from bind_priority_class"))).scalar_one()
        recipe = (await db.execute(text("select id from demand_recipe"))).scalar_one()
        workload = (
            await db.execute(
                text(
                    "insert into demand_workload (uid, name, kind, project_id, priority_class_id, recipe_id)"
                    " values (:uid, 'holder', 'service', :project, :priority, :recipe) returning id"
                ),
                {
                    "uid": ids.new_uid(ids.WORKLOAD),
                    "project": project,
                    "priority": priority,
                    "recipe": recipe,
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
        workload = await WorkloadService(db).create(create())
        decision = await PlacementService(db).preview(PlacementPreviewRequest(recipe="llama-70b-vllm", project="core"))

    assert workload.deployments == []
    codes = {r.code for v in decision.verdicts for r in v.reasons}
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
        decision = await PlacementService(db).preview(PlacementPreviewRequest(recipe="llama-70b-vllm", project="core"))

    assert not decision.placed
    assert decision.verdicts[0].free_devices == 0


async def test_a_not_ready_node_offers_no_capacity(sessions) -> None:
    """Cordon and readiness are different facts, and either one is disqualifying."""
    await seed_demand(sessions)
    await seed_supply(sessions, "sick", devices=8)
    async with sessions.begin() as db:
        await db.execute(text("update supply_node set ready = false, condition = 'not_ready'"))

    async with sessions() as db:
        decision = await PlacementService(db).preview(PlacementPreviewRequest(recipe="llama-70b-vllm", project="core"))

    assert not decision.placed
    assert decision.verdicts[0].free_devices == 0


# ------------------------------------------------------------------ operations


async def _scale_op(db, workload_uid: str, **parameters):
    """Runs a workload operation the way the API does, through OperationService."""
    return await OperationService(db, actor="test").create(
        OperationCreateRequest(type="workload.scale", target=workload_uid, parameters=parameters)
    )


async def test_scaling_a_workload_moves_the_shares(sessions) -> None:
    """The operation the console and CLI call actually re-expands the workload."""
    await seed_demand(sessions)
    await seed_supply(sessions, "first", devices=8)
    await seed_supply(sessions, "second", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(**scale(2)))
    assert workload.scale.placed == 2

    async with sessions() as db:
        operation = await _scale_op(db, workload.id, min=4, desired=4, max=4)
    assert operation.phase == "succeeded"

    async with sessions() as db:
        after = await WorkloadService(db).get(workload.id)
    assert after.scale.desired == 4
    assert sum(d.desired_replicas for d in after.deployments) == 4


async def test_a_workload_operation_needs_no_cluster(sessions) -> None:
    """A workload spans clusters, so the plane executes these itself.

    The guard that every other operation type relies on — a target belonging to
    one cluster — would reject these outright, which is what made the console's
    scale button a 400 before they were plane-executed.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "first", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(**scale(1)))
    async with sessions() as db:
        operation = await _scale_op(db, workload.id, desired=1)
    assert operation.phase == "succeeded"
    assert operation.target.type == "workload"


async def test_a_service_scaled_past_capacity_places_what_fits(sessions) -> None:
    """A partial expansion is not a failure for a splittable kind.

    The clusters that could take some of it took some of it, and the shortfall is
    visible in the rollup rather than costing the caller the capacity that was
    found. Only a gang, which has no partial answer, refuses outright.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "first", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(**scale(1)))
    async with sessions() as db:
        operation = await _scale_op(db, workload.id, min=0, desired=512, max=512)
    assert operation.phase == "succeeded"

    async with sessions() as db:
        after = await WorkloadService(db).get(workload.id)
    assert after.scale.desired == 512
    assert after.scale.placed < 512


async def test_a_refused_operation_is_failed_with_a_reason(sessions) -> None:
    """A refusal is the operation failing, not the request being malformed."""
    await seed_demand(sessions)
    await seed_supply(sessions, "first", devices=8)
    await seed_supply(sessions, "second", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(**scale(1)))
    async with sessions() as db:
        operation = await OperationService(db, actor="test").create(
            OperationCreateRequest(
                type="workload.drain",
                target=workload.id,
                parameters={"cluster": "second"},
            )
        )
    assert operation.phase == "failed"
    assert operation.error


async def test_pausing_disables_every_cluster_copy(sessions) -> None:
    """Pause parks the shares rather than destroying them."""
    await seed_demand(sessions)
    await seed_supply(sessions, "first", devices=8)
    await seed_supply(sessions, "second", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(**scale(2)))
    async with sessions() as db:
        operation = await OperationService(db, actor="test").create(
            OperationCreateRequest(type="workload.pause", target=workload.id, parameters={"paused": True})
        )
    assert operation.phase == "succeeded"

    async with sessions() as db:
        after = await WorkloadService(db).get(workload.id)
    assert after.paused is True
    assert after.deployments
    assert not any(d.enabled for d in after.deployments)


async def test_draining_takes_a_workload_off_one_cluster(sessions) -> None:
    """The totals are untouched; the share is offered to whoever is left."""
    await seed_demand(sessions)
    await seed_supply(sessions, "first", devices=8)
    await seed_supply(sessions, "second", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(**scale(2)))
    victim = workload.deployments[0].cluster_name

    async with sessions() as db:
        operation = await OperationService(db, actor="test").create(
            OperationCreateRequest(type="workload.drain", target=workload.id, parameters={"cluster": victim})
        )
    assert operation.phase == "succeeded"

    async with sessions() as db:
        after = await WorkloadService(db).get(workload.id)
    assert victim not in {d.cluster_name for d in after.deployments}
    assert after.scale.desired == 2
