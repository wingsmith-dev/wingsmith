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
from app.modules.clusters.services import ClusterService
from app.modules.placement.schemas import PlacementShapeRequest
from app.modules.placement.schemas import PlacementPreviewRequest
from app.modules.workloads.schemas import ShapeSchema
from app.modules.workloads.schemas import WorkloadCreateRequest
from app.modules.workloads.schemas import WorkloadServingRequest
from app.modules.operations.schemas import OperationCreateRequest
from app.modules.placement.services import PREVIEW_UID
from app.modules.placement.services import PlacementService
from app.modules.workloads.services import WorkloadService
from app.modules.operations.services import OperationService

from tests.conftest import fetch


pytestmark = pytest.mark.database


DEVICE_CLASS = "h100-80gb-sxm"


async def seed_demand(sessions) -> None:
    """A tenant, project, priority class and the artifact everything here serves.

    The four-device shape used to be a `Recipe` seeded alongside them, which is
    what a preview named. Nothing is seeded in its place: a preview states the
    shape it is asking about, which is the same question a recipe made askable and
    the one a create form has. `seed_workload` exists for the tests that need a
    workload row to hang something off.
    """
    async with sessions.begin() as db:
        await db.execute(
            text("TRUNCATE bind_tenant, bind_priority_class, bind_policy, demand_artifact RESTART IDENTITY CASCADE")
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


async def seed_workload(db, name: str, digest_seed: str) -> uuid.UUID:
    """One workload row carrying the serving configuration, written directly.

    Not through `WorkloadService`, deliberately: creating a workload expands it
    onto a cluster, and a subject seeded that way would be holding the very
    capacity the test is about to ask after.
    """
    artifact = (await db.execute(text("select id from demand_artifact"))).scalar_one()
    project = (await db.execute(text("select id from bind_project"))).scalar_one()
    priority = (await db.execute(text("select id from bind_priority_class"))).scalar_one()
    return (
        await db.execute(
            text(
                "insert into demand_workload"
                " (uid, name, kind, project_id, priority_class_id, artifact_id, serving_digest,"
                "  engine, engine_runtime_version, context_length, device_class, device_count)"
                " values (:uid, :name, 'service', :project, :priority, :artifact, :digest,"
                "  'vllm', '0.9.1', 8192, :cls, 4) returning id"
            ),
            {
                "uid": ids.new_uid(ids.WORKLOAD),
                "name": name,
                "project": project,
                "priority": priority,
                "artifact": artifact,
                "digest": "svg_" + digest_seed * 64,
                "cls": DEVICE_CLASS,
            },
        )
    ).scalar_one()


async def seed_supply(
    sessions,
    name: str,
    devices: int,
    phase: str = "active",
    reachable: bool = True,
    region: str = "eu-west-1",
) -> uuid.UUID:
    """One cluster with a node and some free, healthy accelerators."""
    async with sessions.begin() as db:
        cluster = (
            await db.execute(
                text(
                    "insert into supply_cluster (uid, name, region, phase)"
                    " values (:uid, :name, :region, :phase) returning id"
                ),
                {"uid": ids.new_uid(ids.CLUSTER), "name": name, "phase": phase, "region": region},
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


async def hold_one_device(sessions, cluster: uuid.UUID) -> None:
    """Gives one of the cluster's devices to a workload nobody asked about.

    A live `bind_allocation_device` row is the only record that an accelerator is
    spoken for — `supply_device.allocatable` stays true, because the card is still
    claimable hardware. Anything that reports free devices has to read this.
    """
    async with sessions.begin() as db:
        project = (await db.execute(text("select id from bind_project"))).scalar_one()
        workload = await seed_workload(db, "holder", "b")
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


def four_devices(**overrides) -> PlacementShapeRequest:
    """The shape a preview asks about, stated rather than named.

    A recipe was a declared object you could ask about before any workload
    referenced it, and this is what asking looks like without one. Field for field
    the workload columns `serving()` sets, because a preview that described a
    shape differently from the object it previews would answer about something the
    create then refuses.
    """
    fields = {"device_class": DEVICE_CLASS, "device_count": 4}
    fields.update(overrides)
    return PlacementShapeRequest(**fields)


def serving(**overrides) -> WorkloadServingRequest:
    """The four-device vLLM configuration every workload here asks for.

    Stated on the create rather than named, because there is no shared serving
    object to point at any more. Two of these produce the same `serving_digest`,
    which is the whole of what naming a recipe used to buy.
    """
    fields = {
        "artifact": "llama-70b",
        "engine": "vllm",
        "engine_runtime_version": "0.9.1",
        "context_length": 8192,
        "shape": ShapeSchema(device_class=DEVICE_CLASS, device_count=4),
    }
    fields.update(overrides)
    return WorkloadServingRequest(**fields)


def create(**overrides) -> WorkloadCreateRequest:
    fields = {"serving": serving(), "project": "core", "name": "llama-prod"}
    fields.update(overrides)
    return WorkloadCreateRequest(**fields)


def scale(total: int) -> dict:
    """The three totals set to the same number, which is how a fixed-size workload asks."""
    return {"min_scale": total, "desired_scale": total, "max_scale": total}


async def test_placement_takes_the_roomiest_of_the_clusters_that_work(sessions) -> None:
    """The roomiest, and every cluster that could have held it says why it did not win.

    Tightness is scoped to a topology domain and nothing else — `_rank` in the
    engine, and the alternation tests beside it in `test_placement.py`. Best-fit
    packing across whole clusters drains one before touching the next, which sends
    every small workload to the same cluster until it is full; an aggregate fit
    therefore ranks on room and a gang still takes the smallest fabric that holds
    it. This suite asserted the old rule and the engine changed under it.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "small", devices=4)
    await seed_supply(sessions, "large", devices=8)

    async with sessions() as db:
        decision = await PlacementService(db).preview(PlacementPreviewRequest(shape=four_devices(), project="core"))

    assert decision.placed
    assert decision.cluster_name == "large"
    assert {v.cluster_name for v in decision.verdicts} == {"small", "large"}


async def test_a_preview_answers_about_a_shape_no_workload_declares(sessions) -> None:
    """The question a create form asks, and the one a recipe used to make askable.

    A recipe was a declared object you could ask about before anything referenced
    it. Deleting it briefly left the endpoint able to answer only about workloads
    that already exist, which is the wrong half of the question: somebody choosing
    a fit wants the verdict BEFORE they commit, not after.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "alpha", devices=8)

    async with sessions() as db:
        decision = await PlacementService(db).preview(PlacementPreviewRequest(shape=four_devices(), project="core"))

    assert await fetch(sessions, "select uid from demand_workload") == []
    assert decision.placed
    assert decision.request == PREVIEW_UID, "a decision must not name a workload that will never exist"


async def test_a_preview_names_exactly_one_of_a_workload_and_a_shape(sessions) -> None:
    """Both is two questions and neither is none; either way there is nothing to place."""
    await seed_demand(sessions)
    async with sessions.begin() as db:
        await seed_workload(db, "llama-70b-vllm", "a")

    async with sessions() as db:
        with pytest.raises(Invalid):
            await PlacementService(db).preview(PlacementPreviewRequest(project="core"))
        with pytest.raises(Invalid):
            await PlacementService(db).preview(
                PlacementPreviewRequest(workload="llama-70b-vllm", shape=four_devices(), project="core")
            )


async def test_a_shape_and_the_workload_that_carries_it_get_the_same_answer(sessions) -> None:
    """One scheduler, asked two ways. This is what stops the two forms drifting apart.

    The shape form exists so a fit can be previewed before it is created; if it
    answered differently from the workload it becomes, the preview would be
    advertising a placement the create does not make. Everything but the id the
    decision is filed under has to match — same cluster, same verdicts, same
    reasons, in the same order.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "small", devices=4)
    await seed_supply(sessions, "large", devices=8)
    async with sessions.begin() as db:
        await seed_workload(db, "llama-70b-vllm", "a")

    async with sessions() as db:
        svc = PlacementService(db)
        by_shape = await svc.preview(PlacementPreviewRequest(shape=four_devices(), project="core"))
        by_workload = await svc.preview(PlacementPreviewRequest(workload="llama-70b-vllm", project="core"))

    assert by_shape.request != by_workload.request, "the two forms differ in what the decision is filed under"
    assert (by_shape.placed, by_shape.cluster_name) == (by_workload.placed, by_workload.cluster_name)
    assert [(v.cluster_name, [r.code for r in v.reasons]) for v in by_shape.verdicts] == [
        (v.cluster_name, [r.code for r in v.reasons]) for v in by_workload.verdicts
    ]
    assert by_shape.summary.removeprefix(by_shape.request) == by_workload.summary.removeprefix(by_workload.request)


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
        decision = await PlacementService(db).preview(PlacementPreviewRequest(shape=four_devices(), project="core"))

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


async def test_a_region_allow_list_narrows_placement_without_choosing_for_it(sessions) -> None:
    """A region allow-list is a constraint, not a pin.

    The roomier cluster is the one that loses, which is the whole point: placement still
    ranks and still picks, it just cannot leave the region it was given.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "euro", devices=8, region="eu-west-1")
    await seed_supply(sessions, "asia", devices=4, region="ap-southeast-1")

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(allowed_regions=["ap-southeast-1"]))

    assert [d.cluster_name for d in workload.deployments] == ["asia"]
    assert workload.pinned_cluster_id is None


async def test_a_region_allow_list_becomes_a_policy_attached_to_the_workload(sessions) -> None:
    """The constraint is discoverable afterwards, not a field nothing else can see.

    §6.3 says placement constraints are Policy. Declaring one on the create request is a
    shortcut past `POST /policies`, not a second mechanism beside it — so what lands is an
    ordinary policy row with an ordinary attachment.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "asia", devices=8, region="ap-southeast-1")

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(allowed_regions=["ap-southeast-1"]))

    rows = await fetch(
        sessions,
        "select p.name, p.allowed_regions, a.subject_type"
        " from bind_policy p join bind_policy_attachment a on a.policy_id = p.id"
        " join demand_workload w on w.id = a.subject_id"
        " where w.uid = :uid",
        {"uid": workload.id},
    )
    assert rows == [("llama-prod-placement", ["ap-southeast-1"], "workload")]


async def test_the_constraint_a_workload_declared_survives_into_later_expansions(sessions) -> None:
    """Scaling up must not escape the region the workload was created under.

    This is the half that a create-time-only constraint would get wrong. The policy is
    read back through its attachment on every expansion, so a workload that outgrows its
    region takes a shortfall rather than quietly spilling into another one.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "asia", devices=8, region="ap-southeast-1")
    await seed_supply(sessions, "euro", devices=32, region="eu-west-1")

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(allowed_regions=["ap-southeast-1"]))
    assert [d.cluster_name for d in workload.deployments] == ["asia"]

    async with sessions() as db:
        operation = await _scale_op(db, workload.id, min=0, desired=4, max=4)
    assert operation.phase == "succeeded"

    async with sessions() as db:
        after = await WorkloadService(db).get(workload.id)
    assert {d.cluster_name for d in after.deployments} == {"asia"}
    assert after.scale.desired == 4
    assert sum(d.desired_replicas for d in after.deployments) == 2


async def test_a_cluster_allow_list_still_checks_capacity_unlike_a_pin(sessions) -> None:
    """The difference between `nodeSelector` and `nodeName`, in one assertion.

    A pin at an empty cluster is accepted (`test_naming_a_cluster_skips_placement_entirely`).
    An allow-list naming the same cluster is not: placement ran, found nothing, and said so.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "roomy", devices=8)
    await seed_supply(sessions, "empty", devices=0)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(allowed_clusters=["empty"]))

    assert workload.deployments == []
    assert workload.phase == "pending"


async def test_a_pin_and_an_allow_list_together_are_refused(sessions) -> None:
    """They are contradictory instructions, so the plane refuses rather than ranking them.

    A pin says placement does not decide; an allow-list says it decides inside a smaller
    set. Silently letting one win would make the other a field that sometimes does nothing.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "alpha", devices=8)

    async with sessions() as db:
        with pytest.raises(Invalid):
            await WorkloadService(db).create(create(cluster="alpha", allowed_regions=["eu-west-1"]))


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
    await hold_one_device(sessions, cluster)

    async with sessions() as db:
        decision = await PlacementService(db).preview(PlacementPreviewRequest(shape=four_devices(), project="core"))

    assert not decision.placed
    assert decision.verdicts[0].free_devices == 3


async def test_the_cluster_rollup_reports_a_held_device_as_allocated(sessions) -> None:
    """The console's `0/4 GPUs` bug: the rollup read `allocatable` and nothing else.

    Placement and the rollup have to agree about what is free, so the same hold
    that costs placement a device has to cost the rollup one.
    """
    await seed_demand(sessions)
    cluster = await seed_supply(sessions, "busy", devices=4)
    await hold_one_device(sessions, cluster)

    async with sessions() as db:
        rollup = await ClusterService(db).rollup("busy")

    assert rollup.devices_total == 4
    assert rollup.devices_allocatable == 3
    assert rollup.devices_allocated == 1
    assert rollup.headroom.free_devices == 3
    assert rollup.headroom.whole_nodes_free == 0


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
        decision = await PlacementService(db).preview(PlacementPreviewRequest(shape=four_devices(), project="core"))

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
        decision = await PlacementService(db).preview(PlacementPreviewRequest(shape=four_devices(), project="core"))

    assert not decision.placed
    assert decision.verdicts[0].free_devices == 0


async def test_a_not_ready_node_offers_no_capacity(sessions) -> None:
    """Cordon and readiness are different facts, and either one is disqualifying."""
    await seed_demand(sessions)
    await seed_supply(sessions, "sick", devices=8)
    async with sessions.begin() as db:
        await db.execute(text("update supply_node set ready = false, condition = 'not_ready'"))

    async with sessions() as db:
        decision = await PlacementService(db).preview(PlacementPreviewRequest(shape=four_devices(), project="core"))

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


async def test_scaling_down_removes_a_clusters_share_rather_than_parking_it(sessions) -> None:
    """A share that falls to zero is deleted, not left at zero.

    Three replicas over two clusters that hold two each spill into both; scaling
    back to two fits the first alone, and the second's deployment goes away. The
    invariant is that "which clusters is this on" stays answerable by reading the
    rows — a deployment parked at zero replicas would answer it wrongly.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "first", devices=8)
    await seed_supply(sessions, "second", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(**scale(3)))
    assert len(workload.deployments) == 2

    async with sessions() as db:
        operation = await _scale_op(db, workload.id, min=2, desired=2, max=2)
    assert operation.phase == "succeeded"

    async with sessions() as db:
        after = await WorkloadService(db).get(workload.id)
    assert [d.cluster_name for d in after.deployments] == ["first"]
    assert after.scale.placed == 2
    assert await fetch(sessions, "select count(*) from demand_deployment") == [(1,)]


async def test_re_expanding_onto_the_same_split_does_not_bump_a_generation(sessions) -> None:
    """Re-placement that does not move must not restart the clusters.

    Anything that touches a workload re-derives its placement, and a bumped
    generation is a restart signal to the agent. Landing on the same split and
    bumping anyway would have replicas bounce to arrive exactly where they were.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "first", devices=8)
    await seed_supply(sessions, "second", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(**scale(3)))
    generations = {d.cluster_name: d.generation for d in workload.deployments}

    async with sessions() as db:
        operation = await _scale_op(db, workload.id, min=3, desired=3, max=3)
    assert operation.phase == "succeeded"

    async with sessions() as db:
        after = await WorkloadService(db).get(workload.id)
    assert {d.cluster_name: d.generation for d in after.deployments} == generations


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


# ---------------------------------------------------------------------------
# Reserved capacity: what a cluster owes before it has said what it bound
# ---------------------------------------------------------------------------


async def bind_devices(sessions, cluster: uuid.UUID, deployment_uid: str, count: int) -> None:
    """Reports, as the cluster would, that a deployment now holds `count` devices.

    The half of the story observed state tells. Everything before this row exists is
    a promise the plane made to itself.
    """
    async with sessions.begin() as db:
        deployment, workload = (
            await db.execute(
                text("select id, workload_id from demand_deployment where uid = :uid"),
                {"uid": deployment_uid},
            )
        ).one()
        project = (await db.execute(text("select id from bind_project"))).scalar_one()
        allocation = (
            await db.execute(
                text(
                    "insert into bind_allocation"
                    " (uid, cluster_id, workload_id, deployment_id, project_id, device_class, device_count)"
                    " values (:uid, :cluster, :workload, :deployment, :project, :cls, :count) returning id"
                ),
                {
                    "uid": ids.new_uid(ids.ALLOCATION),
                    "cluster": cluster,
                    "workload": workload,
                    "deployment": deployment,
                    "project": project,
                    "cls": DEVICE_CLASS,
                    "count": count,
                },
            )
        ).scalar_one()
        devices = (
            (
                await db.execute(
                    text("select id from supply_device where cluster_id = :c order by device_index limit :n"),
                    {"c": cluster, "n": count},
                )
            )
            .scalars()
            .all()
        )
        for device in devices:
            await db.execute(
                text("insert into bind_allocation_device (allocation_id, device_id) values (:a, :d)"),
                {"a": allocation, "d": device},
            )


async def test_a_second_workload_does_not_land_on_capacity_the_first_was_already_given(sessions) -> None:
    """The accumulation bug, driven through the caller that actually writes.

    Two clusters of eight, a recipe wanting four, and two workloads created one
    after the other at two replicas each. The first fills a cluster exactly. Nothing
    has bound a device yet — allocations come from the cluster, and the cluster has
    not spoken — so the only record that the capacity is gone is the deployment
    itself. Read from observed state alone, both clusters look empty both times and
    both workloads pile onto the same one.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "alpha", devices=8)
    await seed_supply(sessions, "beta", devices=8)

    async with sessions() as db:
        first = await WorkloadService(db).create(create(name="first", **scale(2)))
    async with sessions() as db:
        second = await WorkloadService(db).create(create(name="second", **scale(2)))

    assert {w.deployments[0].cluster_name for w in (first, second)} == {"alpha", "beta"}
    assert first.scale.placed == 2
    assert second.scale.placed == 2


async def test_the_next_workload_is_told_the_devices_are_promised_and_not_missing(sessions) -> None:
    await seed_demand(sessions)
    await seed_supply(sessions, "alpha", devices=8)

    async with sessions() as db:
        await WorkloadService(db).create(create(name="first", **scale(2)))
    async with sessions() as db:
        decision = await PlacementService(db).preview(PlacementPreviewRequest(shape=four_devices(), project="core"))

    assert decision.placed is False
    verdict = next(v for v in decision.verdicts if v.cluster_name == "alpha")
    assert verdict.free_devices == 8
    assert verdict.reserved_devices == 8
    assert verdict.available_devices == 0
    assert "8 reserved by deployments not yet bound" in verdict.reasons[0].message


async def test_a_reservation_shrinks_as_the_cluster_reports_what_it_bound(sessions) -> None:
    """Declared minus observed, which is what makes the promise self-extinguishing.

    Binding four of the eight leaves four still owed. Nothing was released and
    nothing expired; the same subtraction just returns a smaller number, and it
    reaches zero on its own once the cluster has reported the whole share.
    """
    await seed_demand(sessions)
    cluster = await seed_supply(sessions, "alpha", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(name="first", **scale(2)))
    await bind_devices(sessions, cluster, workload.deployments[0].id, count=4)

    async with sessions() as db:
        decision = await PlacementService(db).preview(PlacementPreviewRequest(shape=four_devices(), project="core"))

    verdict = next(v for v in decision.verdicts if v.cluster_name == "alpha")
    assert verdict.free_devices == 4
    assert verdict.reserved_devices == 4
    assert verdict.available_devices == 0


async def test_a_reservation_is_gone_once_the_whole_share_is_bound(sessions) -> None:
    await seed_demand(sessions)
    cluster = await seed_supply(sessions, "alpha", devices=8)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(name="first", **scale(2)))
    await bind_devices(sessions, cluster, workload.deployments[0].id, count=8)

    async with sessions() as db:
        decision = await PlacementService(db).preview(PlacementPreviewRequest(shape=four_devices(), project="core"))

    verdict = next(v for v in decision.verdicts if v.cluster_name == "alpha")
    assert verdict.free_devices == 0
    assert verdict.reserved_devices == 0


async def test_scaling_up_does_not_move_a_workload_off_its_own_reservation(sessions) -> None:
    """A workload never competes with itself for capacity it has already been given.

    Without the exclusion the re-expansion reads its own pending replicas as somebody
    else's, finds its home cluster full, and moves the share to a cluster that is
    merely emptier — reshuffling a service in the middle of coming up.
    """
    await seed_demand(sessions)
    await seed_supply(sessions, "first", devices=12)
    await seed_supply(sessions, "second", devices=12)

    async with sessions() as db:
        workload = await WorkloadService(db).create(create(**scale(2)))
    assert [d.cluster_name for d in workload.deployments] == ["first"]

    async with sessions() as db:
        operation = await _scale_op(db, workload.id, min=3, desired=3, max=3)
    assert operation.phase == "succeeded"

    async with sessions() as db:
        after = await WorkloadService(db).get(workload.id)
    assert [d.cluster_name for d in after.deployments] == ["first"]
    assert after.scale.placed == 3
