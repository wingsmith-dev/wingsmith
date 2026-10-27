"""The fleet summary and the batched artifact usage read, against a real Postgres.

Both are aggregate statements, and the ways an aggregate goes wrong are joins: a
held device counted free, a workload counted twice because it names an artifact
both ways, an endpoint target pointing at a deployment with no ready replica
summed as null. Each test builds the rows by hand and checks the counts.
"""

import uuid
import datetime as dt

import pytest

from sqlalchemy import text

from app.core import ids
from app.modules.fleet.services import FleetService
from app.modules.artifacts.usage import ArtifactUsageReader


pytestmark = pytest.mark.database

NOW = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.UTC)


async def insert(db, sql: str, **params) -> uuid.UUID:
    return (await db.execute(text(sql + " returning id"), params)).scalar_one()


async def seed_demand(db) -> dict:
    tenant = await insert(
        db,
        "insert into bind_tenant (uid, slug, name) values (:uid, 'zz-fleet', 'ZZ Fleet')",
        uid=ids.new_uid(ids.TENANT),
    )
    priority = await insert(
        db,
        "insert into bind_priority_class (uid, name, value, is_default, preempts)"
        " values (:uid, 'zz-production', 100, true, false)",
        uid=ids.new_uid(ids.PRIORITY_CLASS),
    )
    project = await insert(
        db,
        "insert into bind_project (uid, tenant_id, slug, name, default_priority_class_id)"
        " values (:uid, :tenant, 'zz-core', 'ZZ Core', :priority)",
        uid=ids.new_uid(ids.PROJECT),
        tenant=tenant,
        priority=priority,
    )
    return {"project": project, "priority": priority}


async def seed_artifact(db, name: str, base: uuid.UUID | None = None) -> tuple[uuid.UUID, str]:
    uid = ids.new_uid(ids.ARTIFACT)
    row = await insert(
        db,
        "insert into demand_artifact (uid, digest, name, kind, base_artifact_id)"
        " values (:uid, :digest, :name, 'weights', :base)",
        uid=uid,
        digest=f"sha256:{uuid.uuid4().hex}",
        name=name,
        base=base,
    )
    return row, uid


async def seed_workload(
    db, demand: dict, artifact: uuid.UUID, adapters: list[uuid.UUID] = (), paused: bool = False
) -> uuid.UUID:
    return await insert(
        db,
        "insert into demand_workload"
        " (uid, name, kind, project_id, priority_class_id, artifact_id, serving_digest, engine,"
        "  engine_runtime_version, context_length, device_class, device_count, adapter_artifact_ids, paused)"
        " values (:uid, :name, 'service', :project, :priority, :artifact, :digest, 'vllm', '0.9.1', 8192,"
        "  'h100-80gb-sxm', 1, CAST(:adapters AS uuid[]), :paused)",
        uid=ids.new_uid(ids.WORKLOAD),
        name=f"zz-{uuid.uuid4().hex[:8]}",
        project=demand["project"],
        priority=demand["priority"],
        artifact=artifact,
        digest="svg_" + uuid.uuid4().hex * 2,
        adapters="{" + ",".join(str(a) for a in adapters) + "}",
        paused=paused,
    )


async def seed_cluster(db, name: str, phase: str = "active", reachable: bool = True) -> uuid.UUID:
    cluster = await insert(
        db,
        "insert into supply_cluster (uid, name, region, phase) values (:uid, :name, 'zz-region', :phase)",
        uid=ids.new_uid(ids.CLUSTER),
        name=name,
        phase=phase,
    )
    await db.execute(
        text("insert into supply_cluster_sync_state (cluster_id, reachable) values (:cluster, :reachable)"),
        {"cluster": cluster, "reachable": reachable},
    )
    return cluster


async def seed_node(db, cluster: uuid.UUID, ready: bool = True, condition: str = "ready", cordoned: bool = False):
    return await insert(
        db,
        "insert into supply_node (uid, cluster_id, name, ready, condition, cordoned)"
        " values (:uid, :cluster, :name, :ready, :condition, :cordoned)",
        uid=ids.new_uid(ids.NODE),
        cluster=cluster,
        name=f"zz-node-{uuid.uuid4().hex[:6]}",
        ready=ready,
        condition=condition,
        cordoned=cordoned,
    )


async def seed_device(db, cluster, node, cls: str, health: str = "healthy", allocatable: bool = True):
    return await insert(
        db,
        "insert into supply_device (uid, cluster_id, node_id, device_uuid, device_index, device_class,"
        " memory_mib, health, allocatable)"
        " values (:uid, :cluster, :node, :duid, 0, :cls, 81920, :health, :allocatable)",
        uid=ids.new_uid(ids.DEVICE),
        cluster=cluster,
        node=node,
        duid=f"GPU-zz-{uuid.uuid4().hex}",
        cls=cls,
        health=health,
        allocatable=allocatable,
    )


async def hold(db, demand, cluster, workload, device, released: bool = False) -> None:
    allocation = await insert(
        db,
        "insert into bind_allocation (uid, cluster_id, workload_id, project_id, device_class, device_count)"
        " values (:uid, :cluster, :workload, :project, 'h100-80gb-sxm', 1)",
        uid=ids.new_uid(ids.ALLOCATION),
        cluster=cluster,
        workload=workload,
        project=demand["project"],
    )
    await db.execute(
        text(
            "insert into bind_allocation_device (allocation_id, device_id, released_at)"
            " values (:a, :d, CASE WHEN :released THEN now() END)"
        ),
        {"a": allocation, "d": device, "released": released},
    )


async def seed_deployment(
    db, demand, artifact, cluster, phase: str, desired: int, ready: int, placed_at: dt.datetime | None = None
) -> uuid.UUID:
    """A deployment of a workload of its own, since a workload has one deployment per cluster."""
    workload = await seed_workload(db, demand, artifact)
    deployment = await insert(
        db,
        "insert into demand_deployment (uid, workload_id, cluster_id, name, desired_replicas, admission_phase,"
        " placed_at) values (:uid, :workload, :cluster, :name, :desired, :phase, :placed_at)",
        uid=ids.new_uid(ids.DEPLOYMENT),
        workload=workload,
        cluster=cluster,
        name=f"zz-{uuid.uuid4().hex[:8]}",
        desired=desired,
        phase=phase,
        placed_at=placed_at,
    )
    for index in range(desired):
        await db.execute(
            text(
                "insert into demand_replica (uid, deployment_id, cluster_id, name, phase, ready)"
                " values (:uid, :deployment, :cluster, :name, :phase, :ready)"
            ),
            {
                "uid": ids.new_uid(ids.REPLICA),
                "deployment": deployment,
                "cluster": cluster,
                "name": f"zz-replica-{deployment.hex[:8]}-{index}",
                "phase": "ready" if index < ready else "pending",
                "ready": index < ready,
            },
        )
    return deployment


async def seed_endpoint(db, demand, targets: list[tuple[uuid.UUID, bool]], enabled: bool = True) -> None:
    endpoint = await insert(
        db,
        "insert into demand_endpoint (uid, project_id, name, enabled) values (:uid, :project, :name, :enabled)",
        uid=ids.new_uid(ids.ENDPOINT),
        project=demand["project"],
        name=f"zz-{uuid.uuid4().hex[:8]}",
        enabled=enabled,
    )
    for deployment, target_enabled in targets:
        await db.execute(
            text(
                "insert into demand_endpoint_target (endpoint_id, deployment_id, enabled)"
                " values (:endpoint, :deployment, :enabled)"
            ),
            {"endpoint": endpoint, "deployment": deployment, "enabled": target_enabled},
        )


async def test_an_empty_fleet_is_all_zeros(db):
    summary = await FleetService(db).summary(now=NOW)
    assert summary.clusters.total == 0
    assert summary.devices.by_class == []
    assert summary.deployments.unschedulable_replicas == 0
    assert summary.endpoints.total == 0
    assert summary.generated_at == NOW


async def test_the_fleet_summary_counts_every_collection(db):
    demand = await seed_demand(db)
    weights, _ = await seed_artifact(db, "zz-weights")
    holder = await seed_workload(db, demand, weights)
    await seed_workload(db, demand, weights, paused=True)

    east = await seed_cluster(db, "zz-east")
    west = await seed_cluster(db, "zz-west", phase="draining", reachable=False)
    east_node = await seed_node(db, east)
    west_node = await seed_node(db, west, ready=False, condition="not_ready", cordoned=True)

    held = await seed_device(db, east, east_node, "h100-80gb-sxm")
    released = await seed_device(db, east, east_node, "h100-80gb-sxm")
    await seed_device(db, east, east_node, "h100-80gb-sxm", allocatable=False)
    await seed_device(db, west, west_node, "b200", health="unhealthy")
    await hold(db, demand, east, holder, held)
    await hold(db, demand, east, holder, released, released=True)

    long_ago = NOW - dt.timedelta(hours=1)
    just_now = NOW - dt.timedelta(minutes=1)
    full = await seed_deployment(db, demand, weights, east, "running", desired=2, ready=2, placed_at=long_ago)
    short = await seed_deployment(db, demand, weights, west, "running", desired=3, ready=1, placed_at=long_ago)
    dark = await seed_deployment(db, demand, weights, west, "running", desired=1, ready=0, placed_at=long_ago)
    await seed_deployment(db, demand, weights, east, "pending", desired=4, ready=1, placed_at=long_ago)
    await seed_deployment(db, demand, weights, east, "pending", desired=5, ready=0, placed_at=just_now)
    await seed_deployment(db, demand, weights, east, "queued", desired=2, ready=0)

    await seed_endpoint(db, demand, [(full, True)])
    await seed_endpoint(db, demand, [(full, True), (short, True)])
    await seed_endpoint(db, demand, [(dark, True), (full, False)])
    await seed_endpoint(db, demand, [(full, False)])
    await seed_endpoint(db, demand, [])
    await seed_endpoint(db, demand, [(full, True)], enabled=False)
    await db.commit()

    summary = await FleetService(db).summary(now=NOW)

    assert summary.clusters.total == 2
    assert summary.clusters.reachable == 1
    assert summary.clusters.by_phase == {"active": 1, "draining": 1}

    assert (summary.nodes.total, summary.nodes.ready, summary.nodes.cordoned) == (2, 1, 1)
    assert summary.nodes.by_condition == {"ready": 1, "not_ready": 1}

    # Free is claimable and unheld: the held card and the unclaimable one are allocated, the
    # released hold is free again.
    devices = summary.devices
    assert (devices.total, devices.free, devices.allocated) == (4, 2, 2)
    assert devices.by_health == {"healthy": 3, "unhealthy": 1}
    assert [(c.device_class, c.total, c.free, c.allocated) for c in devices.by_class] == [
        ("b200", 1, 1, 0),
        ("h100-80gb-sxm", 3, 1, 2),
    ]

    deployments = summary.deployments
    assert deployments.total == 6
    assert deployments.by_phase == {"running": 3, "pending": 2, "queued": 1}
    assert deployments.replicas_ready == 4
    assert deployments.clusters_serving == 2
    # The pending deployment placed an hour ago owes 3 and the unstamped queued one owes 2;
    # the one placed a minute ago is still settling.
    assert deployments.unschedulable_replicas == 5
    assert deployments.stuck_after_seconds == 600

    endpoints = summary.endpoints
    assert endpoints.total == 6
    assert endpoints.disabled == 1
    assert (endpoints.serving, endpoints.degraded, endpoints.down, endpoints.idle) == (1, 1, 1, 2)

    assert (summary.workloads.total, summary.workloads.paused) == (8, 1)


async def test_artifact_usage_answers_many_artifacts_in_the_order_asked(db):
    demand = await seed_demand(db)
    base, base_uid = await seed_artifact(db, "zz-base")
    _, quantized_uid = await seed_artifact(db, "zz-base-fp8", base=base)
    await seed_artifact(db, "zz-base-int4", base=base)
    adapter, adapter_uid = await seed_artifact(db, "zz-adapter")
    _, unused_uid = await seed_artifact(db, "zz-unused")

    # One workload names the base both ways and counts once; another reaches it only
    # through its adapters, which is also how the adapter is used.
    await seed_workload(db, demand, base, adapters=[base, adapter])
    await seed_workload(db, demand, base)
    await db.commit()

    missing = ids.new_uid(ids.ARTIFACT)
    usage = await ArtifactUsageReader(db).usage([unused_uid, missing, adapter_uid, base_uid, quantized_uid])

    assert [(u.artifact_id, u.workload_count, u.downstream_count) for u in usage] == [
        (unused_uid, 0, 0),
        (adapter_uid, 1, 0),
        (base_uid, 2, 2),
        (quantized_uid, 0, 0),
    ]
