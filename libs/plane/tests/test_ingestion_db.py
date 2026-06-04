"""Ingestion against a real Postgres, because compiled SQL is not evidence that SQL runs.

`test_ingestion.py` asserts on statements without executing them, which cannot see a constraint
that does not exist, an upsert that erases the identity it claims to keep, or a prune that deletes
the wrong cluster's rows. Everything here goes through `DatabaseSink` onto a migrated database.

The suite is marked `database` and deselected by default (`addopts`), so `uv run pytest` stays
fast and needs nothing running. `just test-db` selects it; with no reachable server every test in
the file skips rather than fails.
"""

import json
import asyncio

from typing import Any

import pytest

from msgspec import json as msgjson
from msgspec import structs
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth import STATIC_OPERATOR
from app.hub.sink import SinkFrame
from app.hub.state import new_id
from app.hub.session import SessionConfig
from app.hub.session import OperatorSession
from app.hub.messages import MESSAGE_TYPES
from app.ingestion.sink import DatabaseSink
from app.ingestion.sink import UnknownCluster
from app.ingestion.desired import stream_desired
from app.modules.events.repository import EventRepository

from tests.conftest import ROOT
from tests.conftest import CLUSTER
from tests.conftest import fetch
from tests.conftest import seed_cluster
from tests.test_hub_session import FakeConnection
from tests.test_hub_session import frame as wire_frame
from tests.test_hub_session import payload_of


pytestmark = pytest.mark.database


FIXTURES = ROOT.parents[0] / "packages" / "contract" / "fixtures"

OTHER_CLUSTER = "clu_01j9x2aaaaaaaaaaaaaaaaaaab"
NODE = "nod_01j9x2bbbbbbbbbbbbbbbbbbbb"
DEVICE = "dev_01j9x2cccccccccccccccccccc"
DEVICE_B = "dev_01j9x2cccccccccccccccccccd"
DOMAIN = "tpd_01j9x2dddddddddddddddddddd"

OTHER_NODE = "nod_01j9x2bbbbbbbbbbbbbbbbbbbx"
OTHER_DEVICES = ("dev_01j9x2ccccccccccccccccccex", "dev_01j9x2ccccccccccccccccccey")
OTHER_DOMAIN = "tpd_01j9x2ddddddddddddddddddx"
OPERATION = "opr_01j9x2kkkkkkkkkkkkkkkkkkkk"
BENCHMARK = "bmk_01j9x2mmmmmmmmmmmmmmmmmmmm"
DEPLOYMENT = "dep_01j9x2ccccccccccccccccccc1"
COMPONENT = "cmp_01j9x2mmmmmmmmmmmmmmmmmmmm"
SEEDED_WORKLOAD = "wkl_01j9x2wwwwwwwwwwwwwwwwwwww"


# --------------------------------------------------------------------------- payloads


def payload(type_name: str, **overrides: Any):
    raw = json.loads((FIXTURES / f"{type_name}.json").read_text())["payload"]
    decoded = msgjson.decode(json.dumps(raw).encode(), type=MESSAGE_TYPES[type_name])
    return structs.replace(decoded, **overrides) if overrides else decoded


def observed(type_name: str, cluster_uid: str = CLUSTER, reply_to: str | None = None, **overrides: Any) -> SinkFrame:
    return SinkFrame(
        cluster_id=cluster_uid,
        type=type_name,
        payload=payload(type_name, **overrides),
        seq=1,
        id=new_id(),
        reply_to=reply_to,
    )


async def apply_supply(
    sink: DatabaseSink,
    cluster_uid: str = CLUSTER,
    reply_to: str | None = None,
    node: str = NODE,
    devices: tuple[str, str] = (DEVICE, DEVICE_B),
    domain: str = DOMAIN,
) -> None:
    """Node, then its two devices, then the domain that spans them.

    Ids are parameters because `uid` is unique across the whole table, not per cluster: two
    clusters observing the same identifier is not a case the schema can represent.
    """
    await sink.apply_observed(observed("node.observed", cluster_uid, reply_to, node_id=node))
    await sink.apply_observed(
        observed(
            "device.observed", cluster_uid, reply_to, device_id=devices[0], node_id=node, topology_domain_id=domain
        )
    )
    await sink.apply_observed(
        observed(
            "device.observed",
            cluster_uid,
            reply_to,
            device_id=devices[1],
            node_id=node,
            index=1,
            uuid="GPU-b",
            topology_domain_id=domain,
        )
    )
    await sink.apply_observed(
        observed(
            "topology.observed",
            cluster_uid,
            reply_to,
            domain_id=domain,
            node_ids=[node],
            spans_nodes=False,
            device_ids=list(devices),
        )
    )


# --------------------------------------------------------------------------- observed state


async def test_a_node_is_inserted_and_keeps_its_uid_when_re_observed(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("node.observed"))

    rows = await fetch(sessions, "SELECT uid, name, ready, condition FROM supply_node")
    assert rows == [(NODE, "gpu-h100-a-07", True, "ready")]

    await sink.apply_observed(observed("node.observed", node_id="nod_01j9x2bbbbbbbbbbbbbbbbbbbz", ready=False))

    rows = await fetch(sessions, "SELECT uid, ready, condition FROM supply_node")
    assert rows == [(NODE, False, "not_ready")], "a rediscovered node must keep the id it was first given"


async def test_a_device_frame_resolves_its_node(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("node.observed"))
    await sink.apply_observed(observed("device.observed"))

    rows = await fetch(
        sessions,
        "SELECT d.uid, n.uid, d.device_uuid, d.memory_mib, d.health FROM supply_device d JOIN supply_node n ON n.id = d.node_id",
    )
    assert rows == [(DEVICE, NODE, "GPU-4c1f9a2e-7b30-4d55-9f21-0a8e6c3b1d47", 81559, "healthy")]


async def test_a_topology_frame_links_the_devices_it_names(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await apply_supply(sink)

    rows = await fetch(
        sessions,
        "SELECT d.uid FROM supply_device d JOIN supply_topology_domain t ON t.id = d.topology_domain_id"
        " WHERE t.uid = :domain ORDER BY d.uid",
        {"domain": DOMAIN},
    )
    assert [row[0] for row in rows] == [DEVICE, DEVICE_B]

    domains = await fetch(sessions, "SELECT uid, kind, device_count, spans_nodes FROM supply_topology_domain")
    assert domains == [(DOMAIN, "nvlink", 2, False)]


async def test_a_topology_frame_records_every_node_it_spans(sessions, cluster) -> None:
    """The wire has always carried `node_ids`; now the database keeps all of them."""
    sink = DatabaseSink(sessions)
    second = "nod_01j9x2bbbbbbbbbbbbbbbbbbbq"
    await apply_supply(sink)
    await sink.apply_observed(observed("node.observed", node_id=second, name="gpu-h100-a-08"))
    await sink.apply_observed(
        observed(
            "topology.observed",
            domain_id=DOMAIN,
            node_ids=[NODE, second],
            spans_nodes=True,
            device_ids=[DEVICE, DEVICE_B],
        )
    )

    rows = await fetch(
        sessions,
        "SELECT n.uid FROM supply_topology_domain_node m"
        " JOIN supply_node n ON n.id = m.node_id JOIN supply_topology_domain t ON t.id = m.domain_id"
        " WHERE t.uid = :domain ORDER BY n.uid",
        {"domain": DOMAIN},
    )
    assert [row[0] for row in rows] == sorted([NODE, second])


async def test_a_node_dropping_out_of_a_domain_drops_out_of_its_membership(sessions, cluster) -> None:
    """Last-writer-wins: the frame is the whole membership, not an addition to it."""
    sink = DatabaseSink(sessions)
    await apply_supply(sink)
    await sink.apply_observed(
        observed("topology.observed", domain_id=DOMAIN, node_ids=[], spans_nodes=True, device_ids=[DEVICE])
    )

    rows = await fetch(sessions, "SELECT count(*) FROM supply_topology_domain_node")
    assert rows == [(0,)]


async def test_re_observing_a_domain_does_not_duplicate_its_membership(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await apply_supply(sink)
    await apply_supply(sink)

    rows = await fetch(sessions, "SELECT count(*) FROM supply_topology_domain_node")
    assert rows == [(1,)]


async def test_re_observing_a_device_updates_it_in_place(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("node.observed"))
    await sink.apply_observed(observed("device.observed"))
    await sink.apply_observed(
        observed("device.observed", device_id="dev_01j9x2ccccccccccccccccccce", health="degraded")
    )

    rows = await fetch(sessions, "SELECT uid, health FROM supply_device")
    assert rows == [(DEVICE, "degraded")]


async def test_a_frame_for_an_unregistered_cluster_is_refused(sessions) -> None:
    sink = DatabaseSink(sessions)
    with pytest.raises(UnknownCluster):
        await sink.apply_observed(observed("node.observed"))


async def test_a_frame_keyed_by_cluster_name_is_applied(sessions, cluster) -> None:
    """An operator is configured before anyone has read back the uid the plane minted."""
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("node.observed", cluster_uid="alpha"))

    rows = await fetch(sessions, "SELECT cluster_id FROM supply_node")
    assert rows == [(cluster,)]


async def test_a_frame_keyed_by_an_unknown_name_is_still_refused(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    with pytest.raises(UnknownCluster):
        await sink.apply_observed(observed("node.observed", cluster_uid="not-a-cluster"))


async def test_applying_one_frame_twice_concurrently_is_idempotent(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("node.observed"))

    await asyncio.gather(
        sink.apply_observed(observed("device.observed")),
        sink.apply_observed(observed("device.observed")),
    )

    rows = await fetch(sessions, "SELECT count(*) FROM supply_device")
    assert rows == [(1,)]


# --------------------------------------------------------------------------- poison frames


async def test_a_malformed_id_is_dropped_and_acked(sessions, cluster) -> None:
    """PROTOCOL 6b: a frame that can never apply is accepted, not replayed forever."""
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("node.observed", node_id="not-an-id"))

    assert await fetch(sessions, "SELECT count(*) FROM supply_node") == [(0,)]


async def test_a_malformed_id_poisons_the_open_epoch(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    request = new_id()
    await sink.begin_resync(CLUSTER, "supply", request)
    await sink.apply_observed(observed("node.observed", CLUSTER, request, node_id="not-an-id"))
    assert sink._epochs[request].poisoned is True


# --------------------------------------------------------------------------- events


async def test_a_node_event_lands_once(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("node.observed"))
    event = SinkFrame(
        cluster_id=CLUSTER, type="node.event", payload=payload("node.event", node_id=NODE), seq=2, id=new_id()
    )
    await sink.record_event(event)
    await sink.record_event(event)

    rows = await fetch(
        sessions,
        "SELECT e.type, e.severity, e.subject_type, n.uid FROM ops_event e JOIN supply_node n ON n.id = e.subject_id",
    )
    assert rows == [("node.degraded", "warning", "node", NODE)]


async def test_two_sessions_replaying_one_event_still_write_one_row(sessions, cluster) -> None:
    """The race a `NOT EXISTS` could not close: both read nothing, both insert."""
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("node.observed"))
    event = SinkFrame(
        cluster_id=CLUSTER, type="node.event", payload=payload("node.event", node_id=NODE), seq=2, id=new_id()
    )
    await asyncio.gather(sink.record_event(event), sink.record_event(event))

    assert await fetch(sessions, "SELECT count(*) FROM ops_event") == [(1,)]


async def test_the_constraint_holds_when_the_cluster_is_null(sessions, cluster) -> None:
    """`NULLS NOT DISTINCT`: without it, exactly the plane's own events would slip through."""
    async with sessions() as db:
        for _ in range(2):
            await db.execute(
                text(
                    "INSERT INTO ops_event (uid, cluster_id, type, severity, subject_type, message, occurred_at)"
                    " VALUES (:uid, NULL, 'plane.started', 'info', 'plane', 'control plane started', now())"
                    " ON CONFLICT (cluster_id, type, subject_type, occurred_at, md5(message)) DO NOTHING"
                ),
                {"uid": "evt_01j9x2e0000000000000000009"},
            )
        await db.commit()

    assert await fetch(sessions, "SELECT count(*) FROM ops_event") == [(1,)]


async def test_a_device_event_resolves_its_device(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("node.observed"))
    await sink.apply_observed(observed("device.observed", device_id=DEVICE_B))
    await sink.record_event(
        SinkFrame(
            cluster_id=CLUSTER,
            type="device.event",
            payload=payload("device.event", device_id=DEVICE_B, node_id=NODE),
            seq=2,
            id=new_id(),
        )
    )

    rows = await fetch(
        sessions,
        "SELECT e.type, e.severity, d.uid, e.details->>'reason' FROM ops_event e"
        " JOIN supply_device d ON d.id = e.subject_id",
    )
    assert rows == [("device.fell_off_bus", "error", DEVICE_B, "gpu_lost")]


async def test_a_replica_event_lands_with_no_subject(sessions, cluster) -> None:
    """The one event whose subject is not a row: `subject_id` is a literal NULL of uuid type."""
    sink = DatabaseSink(sessions)
    await sink.record_event(
        SinkFrame(cluster_id=CLUSTER, type="replica.event", payload=payload("replica.event"), seq=1, id=new_id())
    )

    rows = await fetch(
        sessions, "SELECT type, severity, subject_type, subject_id, details->>'replica_id' FROM ops_event"
    )
    assert rows == [("replica.oom", "error", "replica", None, "rep_01j9x2hhhhhhhhhhhhhhhhhhhh")]


# --------------------------------------------------------------------------- benchmarks


async def seed_deployment(sessions, cluster) -> None:
    """The smallest chain that makes a deployment row legal, for tests about what hangs off one.

    Every insert is an idempotent upsert on `uid`. Only rows reachable from
    `supply_cluster` are truncated between tests, so the tenant, project,
    priority class, artifact and workload above the deployment survive into the
    next one and a plain insert would collide on the second call.
    """
    async with sessions.begin() as db:
        await db.execute(
            text(
                """
                WITH t AS (
                  INSERT INTO bind_tenant (uid, slug, name) VALUES ('ten_bench', 'bench', 'Bench')
                  ON CONFLICT (uid) DO UPDATE SET name = excluded.name RETURNING id
                ), p AS (
                  INSERT INTO bind_project (uid, tenant_id, slug, name)
                  SELECT 'prj_bench', t.id, 'bench', 'Bench' FROM t
                  ON CONFLICT (uid) DO UPDATE SET name = excluded.name RETURNING id
                ), c AS (
                  INSERT INTO bind_priority_class (uid, name, value) VALUES ('prc_bench', 'bench', 41337)
                  ON CONFLICT (uid) DO UPDATE SET name = excluded.name RETURNING id
                ), a AS (
                  INSERT INTO demand_artifact (uid, digest, name)
                  VALUES ('art_bench', 'sha256:bench', 'llama-3-70b')
                  ON CONFLICT (uid) DO UPDATE SET name = excluded.name RETURNING id
                ), w AS (
                  INSERT INTO demand_workload (uid, name, kind, project_id, priority_class_id,
                                               artifact_id, serving_digest, engine, engine_runtime_version,
                                               context_length, device_class, engine_args)
                  SELECT :workload, 'llama-70b-prod', 'service', p.id, c.id, a.id,
                         'svg_00000000000000000000000000000000000000000000000000000000000000bc',
                         'vllm', '0.8.4', 8192, 'h100-80gb-sxm',
                         '{"served_model_name": "llama-70b"}'::jsonb
                  FROM p, c, a
                  ON CONFLICT (uid) DO UPDATE SET kind = excluded.kind RETURNING id
                )
                INSERT INTO demand_deployment (uid, workload_id, cluster_id, name, generation)
                SELECT :deployment, w.id, :cluster, 'llama-70b-prod', 7 FROM w
                ON CONFLICT (uid) DO UPDATE SET cluster_id = excluded.cluster_id
                """
            ),
            {"deployment": DEPLOYMENT, "cluster": cluster, "workload": SEEDED_WORKLOAD},
        )


async def seed_benchmark(sessions, cluster, phase: str = "pending") -> None:
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO ops_benchmark_run (uid, deployment_id, cluster_id, harness, requested_by,"
                " model, deployment_generation, phase)"
                " SELECT :uid, d.id, :cluster, 'aiperf', 'staff', 'llama-70b', 7, :phase"
                " FROM demand_deployment d WHERE d.uid = :deployment"
            ),
            {"uid": BENCHMARK, "cluster": cluster, "deployment": DEPLOYMENT, "phase": phase},
        )


async def test_a_benchmark_frame_lands_the_harness_summary_verbatim(sessions, cluster) -> None:
    """The result is stored as the harness emitted it, not parsed into columns."""
    await seed_deployment(sessions, cluster)
    await seed_benchmark(sessions, cluster)

    sink = DatabaseSink(sessions)
    await sink.apply_observed(
        SinkFrame(
            cluster_id=CLUSTER,
            type="benchmark.observed",
            payload=payload("benchmark.observed", benchmark_id=BENCHMARK),
            seq=1,
            id=new_id(),
        )
    )

    rows = await fetch(
        sessions,
        "SELECT phase, harness_version, summary, artifacts_uri, error IS NOT NULL,"
        " started_at IS NOT NULL, finished_at IS NOT NULL FROM ops_benchmark_run",
    )
    assert len(rows) == 1
    phase, version, summary, artifacts, has_error, started, finished = rows[0]
    assert (phase, version, has_error, started, finished) == ("failed", "0.4.1", True, True, True)
    assert artifacts == payload("benchmark.observed").artifacts_uri
    assert summary == payload("benchmark.observed").summary


async def test_a_benchmark_frame_for_an_undeclared_run_writes_nothing(sessions, cluster) -> None:
    """Ingestion reports on runs. A run is asked for by somebody; it never invents one."""
    sink = DatabaseSink(sessions)
    await sink.apply_observed(
        SinkFrame(cluster_id=CLUSTER, type="benchmark.observed", payload=payload("benchmark.observed"), id=new_id())
    )

    assert await fetch(sessions, "SELECT count(*) FROM ops_benchmark_run") == [(0,)]


async def test_a_failed_run_clears_a_summary_from_an_earlier_report(sessions, cluster) -> None:
    """A stale summary beside a failed phase would read as a measurement that was not taken."""
    await seed_deployment(sessions, cluster)
    await seed_benchmark(sessions, cluster)

    sink = DatabaseSink(sessions)
    frame = lambda p: SinkFrame(cluster_id=CLUSTER, type="benchmark.observed", payload=p, seq=1, id=new_id())
    await sink.apply_observed(frame(payload("benchmark.observed", benchmark_id=BENCHMARK)))
    await sink.apply_observed(
        frame(payload("benchmark.observed", benchmark_id=BENCHMARK, summary=None, artifacts_uri=None))
    )

    assert await fetch(sessions, "SELECT summary, artifacts_uri FROM ops_benchmark_run") == [(None, None)]


async def test_a_cancelled_operation_closes_the_run_it_was_carrying(sessions, cluster) -> None:
    """The bug this exists for: a run whose operation ended without one reporting.

    A benchmark's phase is written from `benchmark.observed`, and everything that
    fails before the executor reaches the harness sends none — a refused kind, an
    operator too old to know it, a cancellation while still pending. The run sat
    `pending` for ever with nothing on it saying why.
    """
    await seed_deployment(sessions, cluster)
    await seed_benchmark(sessions, cluster)
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO ops_operation (uid, cluster_id, type, target_type, target_id, requested_by)"
                " SELECT :uid, :cluster, 'deployment.benchmark', 'deployment', d.id, 'staff'"
                " FROM demand_deployment d WHERE d.uid = :deployment"
            ),
            {"uid": OPERATION, "cluster": cluster, "deployment": DEPLOYMENT},
        )
        await db.execute(
            text("UPDATE ops_benchmark_run SET operation_id = (SELECT id FROM ops_operation WHERE uid = :uid)"),
            {"uid": OPERATION},
        )

    sink = DatabaseSink(sessions)
    await sink.record_operation(
        SinkFrame(
            cluster_id=CLUSTER,
            type="operation.observed",
            payload=payload("operation.observed", operation_id=OPERATION),
            seq=1,
            id=new_id(),
        )
    )

    rows = await fetch(sessions, "SELECT phase, error, finished_at IS NOT NULL FROM ops_benchmark_run")
    assert rows == [("failed", payload("operation.observed").failure.message, True)]


async def test_a_run_that_already_reported_outlives_its_operation_frame(sessions, cluster) -> None:
    """The run was written by the thing that did the work. It is the authority."""
    await seed_deployment(sessions, cluster)
    await seed_benchmark(sessions, cluster, phase="succeeded")
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO ops_operation (uid, cluster_id, type, target_type, target_id, requested_by)"
                " SELECT :uid, :cluster, 'deployment.benchmark', 'deployment', d.id, 'staff'"
                " FROM demand_deployment d WHERE d.uid = :deployment"
            ),
            {"uid": OPERATION, "cluster": cluster, "deployment": DEPLOYMENT},
        )
        await db.execute(
            text("UPDATE ops_benchmark_run SET operation_id = (SELECT id FROM ops_operation WHERE uid = :uid)"),
            {"uid": OPERATION},
        )

    sink = DatabaseSink(sessions)
    await sink.record_operation(
        SinkFrame(
            cluster_id=CLUSTER,
            type="operation.observed",
            payload=payload("operation.observed", operation_id=OPERATION),
            seq=1,
            id=new_id(),
        )
    )

    assert await fetch(sessions, "SELECT phase, error FROM ops_benchmark_run") == [("succeeded", None)]


async def test_an_operation_carrying_no_run_closes_nothing(sessions, cluster) -> None:
    """Most operations have no run. The join must simply match nothing."""
    await seed_deployment(sessions, cluster)
    await seed_benchmark(sessions, cluster)

    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO ops_operation (uid, cluster_id, type, target_type, target_id, requested_by)"
                " VALUES (:uid, :cluster, 'node.drain', 'node', gen_random_uuid(), 'staff')"
            ),
            {"uid": OPERATION, "cluster": cluster},
        )

    sink = DatabaseSink(sessions)
    await sink.record_operation(
        SinkFrame(
            cluster_id=CLUSTER,
            type="operation.observed",
            payload=payload("operation.observed", operation_id=OPERATION),
            seq=1,
            id=new_id(),
        )
    )

    assert await fetch(sessions, "SELECT phase FROM ops_benchmark_run") == [("pending",)]


# --------------------------------------------------------------------------- operations and components


async def test_an_operation_frame_updates_a_declared_operation(sessions, cluster) -> None:
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO ops_operation (uid, cluster_id, type, target_type, target_id, requested_by)"
                " VALUES (:uid, :cluster, 'drain', 'node', gen_random_uuid(), 'staff')"
            ),
            {"uid": OPERATION, "cluster": cluster},
        )

    sink = DatabaseSink(sessions)
    await sink.record_operation(
        SinkFrame(
            cluster_id=CLUSTER,
            type="operation.observed",
            payload=payload("operation.observed", operation_id=OPERATION),
            seq=1,
            id=new_id(),
        )
    )

    rows = await fetch(sessions, "SELECT phase, error, finished_at IS NOT NULL FROM ops_operation")
    assert rows == [("failed", payload("operation.observed").failure.message, True)]


async def test_an_operation_frame_for_an_undeclared_operation_writes_nothing(sessions, cluster) -> None:
    """Ingestion reports on operations; it never brings one into existence."""
    sink = DatabaseSink(sessions)
    await sink.record_operation(
        SinkFrame(cluster_id=CLUSTER, type="operation.observed", payload=payload("operation.observed"), id=new_id())
    )

    assert await fetch(sessions, "SELECT count(*) FROM ops_operation") == [(0,)]


async def test_a_component_frame_writes_the_observed_half(sessions, cluster) -> None:
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO ops_component (uid, cluster_id, name, kind, desired_version)"
                " VALUES (:uid, :cluster, 'serving-runtime', 'runtime', '0.9.3')"
            ),
            {"uid": COMPONENT, "cluster": cluster},
        )

    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("component.observed"))
    await sink.apply_observed(observed("component.observed", phase="healthy", running_version="0.9.3"))

    rows = await fetch(sessions, "SELECT observed_version, rollout_phase, healthy FROM ops_component_status")
    assert rows == [("0.9.3", "healthy", True)]


async def test_a_component_frame_for_an_undeclared_component_is_refused(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    with pytest.raises(IntegrityError):
        await sink.apply_observed(observed("component.observed"))


# --------------------------------------------------------------------------- discovered

WORKLOAD = "dsw_01j9x2ffffffffffffffffffff"


async def test_a_discovered_workload_is_recorded_without_a_deployment(sessions, cluster) -> None:
    """The sighting lands and nothing declared appears beside it.

    A row in `demand_deployment` would be a declaration, and the operator would be
    handed a difference between it and what is running — a rollout of a model that
    was already serving.
    """
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("workload.discovered"))

    rows = await fetch(sessions, "select uid, name, devices_total, model from demand_discovered_workload")
    assert rows == [(WORKLOAD, "llama-70b-vllm", 8, "meta-llama/Llama-3.1-70B-Instruct")]
    assert await fetch(sessions, "select count(*) from demand_deployment") == [(0,)]


async def test_re_observing_a_workload_keeps_when_we_first_saw_it(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("workload.discovered"))
    first = (await fetch(sessions, "select first_seen_at from demand_discovered_workload"))[0][0]

    await sink.apply_observed(observed("workload.discovered", replicas_ready=1, observed_at="2026-08-19T15:00:00.000Z"))
    rows = await fetch(sessions, "select uid, replicas_ready, first_seen_at from demand_discovered_workload")
    assert rows == [(WORKLOAD, 1, first)]


async def test_a_decision_survives_the_sighting_being_pruned(sessions, cluster) -> None:
    """The reason adoptions are keyed by uid and not by a foreign key.

    A cluster that reconnects and streams a snapshot without this workload takes
    the sighting away. If the decision went with it, an operator would re-adopt the
    same models every time a resync raced a rolling restart.
    """
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("workload.discovered"))
    cluster_id = (await fetch(sessions, "select cluster_id from demand_discovered_workload"))[0][0]

    async with sessions.begin() as db:
        await db.execute(
            text(
                "insert into demand_workload_adoption (cluster_id, workload_uid, decision)"
                " values (:cluster_id, :uid, 'tracked')"
            ),
            {"cluster_id": cluster_id, "uid": WORKLOAD},
        )

    request = new_id()
    await sink.begin_resync(CLUSTER, "supply", request)
    await sink.end_resync(CLUSTER, request, 0, True)

    assert await fetch(sessions, "select count(*) from demand_discovered_workload") == [(0,)]
    assert await fetch(sessions, "select decision from demand_workload_adoption") == [("tracked",)]


async def test_a_tracked_workload_is_not_streamed_as_desired_state(sessions, cluster) -> None:
    """The guarantee, checked where it actually lives.

    Tracking writes a decision. If it leaked into `stream_desired`, the cluster
    would be handed a deployment for a workload it is already running and would
    reconcile the difference by restarting it.
    """
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("workload.discovered"))
    cluster_id = (await fetch(sessions, "select cluster_id from demand_discovered_workload"))[0][0]
    async with sessions.begin() as db:
        await db.execute(
            text(
                "insert into demand_workload_adoption (cluster_id, workload_uid, decision)"
                " values (:cluster_id, :uid, 'tracked')"
            ),
            {"cluster_id": cluster_id, "uid": WORKLOAD},
        )

    async with sessions() as db:
        frames = [item async for item in stream_desired(db, CLUSTER, "demand")]
    assert not [f for f in frames if f[0] == "deployment.desired"]


# --------------------------------------------------------------------------- resync


async def test_a_complete_snapshot_prunes_exactly_the_absent_rows(sessions, cluster) -> None:
    other = await seed_cluster(sessions, OTHER_CLUSTER, "beta")
    sink = DatabaseSink(sessions)
    await apply_supply(sink)
    await apply_supply(sink, OTHER_CLUSTER, node=OTHER_NODE, devices=OTHER_DEVICES, domain=OTHER_DOMAIN)
    assert await fetch(sessions, "SELECT count(*) FROM supply_device") == [(2 * 2,)]

    request = new_id()
    await sink.begin_resync(CLUSTER, "supply", request)
    await sink.apply_observed(observed("node.observed", CLUSTER, request))
    await sink.apply_observed(observed("device.observed", CLUSTER, request))
    await sink.apply_observed(observed("topology.observed", CLUSTER, request, device_ids=[DEVICE]))
    await sink.end_resync(CLUSTER, request, 3, True)

    kept = await fetch(sessions, "SELECT uid FROM supply_device WHERE cluster_id = :id", {"id": cluster})
    assert [row[0] for row in kept] == [DEVICE], "the device absent from the snapshot must be gone"

    untouched = await fetch(sessions, "SELECT count(*) FROM supply_device WHERE cluster_id = :id", {"id": other})
    assert untouched == [(2,)], "another cluster's inventory is not evidence about this one"


async def test_a_complete_empty_snapshot_prunes_the_whole_inventory(sessions, cluster) -> None:
    """The dangerous case, and the one `keep or {""}` exists for: an empty keep set."""
    other = await seed_cluster(sessions, OTHER_CLUSTER, "beta")
    sink = DatabaseSink(sessions)
    await apply_supply(sink)
    await apply_supply(sink, OTHER_CLUSTER, node=OTHER_NODE, devices=OTHER_DEVICES, domain=OTHER_DOMAIN)

    request = new_id()
    await sink.begin_resync(CLUSTER, "supply", request)
    await sink.end_resync(CLUSTER, request, 0, True)

    assert await fetch(sessions, "SELECT count(*) FROM supply_node WHERE cluster_id = :id", {"id": cluster}) == [(0,)]
    assert await fetch(sessions, "SELECT count(*) FROM supply_device WHERE cluster_id = :id", {"id": cluster}) == [(0,)]
    assert await fetch(sessions, "SELECT count(*) FROM supply_node WHERE cluster_id = :id", {"id": other}) == [(1,)]
    assert await fetch(sessions, "SELECT count(*) FROM supply_device WHERE cluster_id = :id", {"id": other}) == [(2,)]


async def test_a_pruned_node_leaves_an_event_behind(sessions, cluster) -> None:
    """Deleting the row must not delete the fact. The spot-instance case depends on this.

    A node that vanishes from a complete snapshot used to be removed in silence,
    which meant the single most informative moment in an outage — the only GPU node
    going away — produced nothing anyone could read afterwards. `subject_id` stays
    null because the subject is being deleted in the same transaction; the uid is in
    `details`, where nothing can cascade it away.
    """
    sink = DatabaseSink(sessions)
    await apply_supply(sink)

    request = new_id()
    await sink.begin_resync(CLUSTER, "supply", request)
    await sink.end_resync(CLUSTER, request, 0, True)

    events = await fetch(
        sessions,
        "SELECT type, severity, message, details FROM ops_event WHERE type = 'node.removed'",
    )
    assert len(events) == 1
    assert events[0][1] == "warning"
    assert events[0][3]["reason"] == "absent_from_snapshot"
    assert events[0][3]["node_id"] == NODE


async def test_an_incomplete_snapshot_leaves_no_removal_event_either(sessions, cluster) -> None:
    """No prune, no event. A truncated snapshot must not be reported as things leaving."""
    sink = DatabaseSink(sessions)
    await apply_supply(sink)

    request = new_id()
    await sink.begin_resync(CLUSTER, "supply", request)
    await sink.end_resync(CLUSTER, request, 1, False)

    assert await fetch(sessions, "SELECT count(*) FROM ops_event WHERE type = 'node.removed'") == [(0,)]


async def test_an_incomplete_snapshot_prunes_nothing(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await apply_supply(sink)

    request = new_id()
    await sink.begin_resync(CLUSTER, "supply", request)
    await sink.apply_observed(observed("node.observed", CLUSTER, request))
    await sink.end_resync(CLUSTER, request, 1, False)

    assert await fetch(sessions, "SELECT count(*) FROM supply_device") == [(2,)]


async def test_a_count_mismatch_prunes_nothing(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await apply_supply(sink)

    request = new_id()
    await sink.begin_resync(CLUSTER, "supply", request)
    await sink.apply_observed(observed("node.observed", CLUSTER, request))
    await sink.end_resync(CLUSTER, request, 9, True)

    assert await fetch(sessions, "SELECT count(*) FROM supply_device") == [(2,)]
    assert await fetch(sessions, "SELECT count(*) FROM supply_node") == [(1,)]


# --------------------------------------------------------------------------- sync state


async def test_connect_and_disconnect_are_written_to_sync_state(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await sink.mark_connected(CLUSTER, "0.4.2")

    rows = await fetch(
        sessions,
        "SELECT reachable, operator_version, connected_at IS NOT NULL, last_error FROM supply_cluster_sync_state",
    )
    assert rows == [(True, "0.4.2", True, None)]

    await sink.mark_disconnected(CLUSTER, "peer went away")

    rows = await fetch(
        sessions,
        "SELECT reachable, operator_version, disconnected_at IS NOT NULL, last_error FROM supply_cluster_sync_state",
    )
    assert rows == [(False, "0.4.2", True, "peer went away")]


async def test_connecting_by_name_reports_back_the_canonical_uid(sessions, cluster) -> None:
    """What live dispatch is keyed by, for an operator that only knows its own name.

    Writers announce a change by uid, so a session registered under the name the
    operator was configured with is unreachable until it learns this.
    """
    sink = DatabaseSink(sessions)

    assert await sink.mark_connected("alpha", "0.4.2") == CLUSTER


async def test_applying_frames_bumps_the_observed_generation(sessions, cluster) -> None:
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed("node.observed"))
    await sink.apply_observed(observed("node.observed"))

    rows = await fetch(sessions, "SELECT observed_generation, last_sync_at IS NOT NULL FROM supply_cluster_sync_state")
    assert rows == [(2, True)]


# --------------------------------------------------------------------------- end to end


async def test_a_live_session_writes_state_and_marks_the_cluster(sessions, cluster) -> None:
    """The wiring of part one: a real session, a real sink, real rows."""
    sink = DatabaseSink(sessions)
    connection = FakeConnection()
    session = OperatorSession(
        connection,
        STATIC_OPERATOR,
        sink=sink,
        config=SessionConfig(ping_interval=60.0, liveness_timeout=120.0),
    )
    task = asyncio.create_task(session.run())

    connection.push(wire_frame("hello", {**payload_of("hello"), "cluster_id": CLUSTER, "lossy": True}))
    await connection.next_of_type("welcome")
    await connection.next_of_type("stream.end")

    connection.push(wire_frame("node.observed", payload_of("node.observed"), seq=1))
    await connection.next_of_type("ack", timeout=3.0)

    assert await fetch(sessions, "SELECT uid FROM supply_node") == [(NODE,)]
    assert await fetch(sessions, "SELECT reachable, operator_version FROM supply_cluster_sync_state") == [
        (True, "0.4.2")
    ]

    await connection.close()
    await asyncio.wait_for(task, timeout=3.0)

    assert await fetch(sessions, "SELECT reachable, disconnected_at IS NOT NULL FROM supply_cluster_sync_state") == [
        (False, True)
    ]


# --------------------------------------------------------------------------- weights residency


async def seed_warm_artifact(sessions, cluster) -> None:
    """The artifact `seed_deployment` already hangs a workload off, ready to be reported on."""
    await seed_deployment(sessions, cluster)


async def test_a_residency_frame_lands_a_row_and_an_event(sessions, cluster) -> None:
    """Residency used to land a row and nothing else.

    The row answers "are the weights there now"; a stuck deployment is asking what
    has been happening to them, and there was no answer anywhere. Both halves are
    written from the one frame now.
    """
    await seed_warm_artifact(sessions, cluster)
    sink = DatabaseSink(sessions)

    await sink.apply_observed(
        observed("cache.observed", artifact_id="art_bench", state="absent", resident=False, nodes=[])
    )

    rows = await fetch(sessions, "SELECT state FROM ops_cache_entry WHERE cluster_id = :c", {"c": cluster})
    assert [r.state for r in rows] == ["absent"]
    events = await fetch(sessions, "SELECT type, subject_type FROM ops_event WHERE cluster_id = :c", {"c": cluster})
    assert ("cache.absent", "artifact") in [(e.type, e.subject_type) for e in events]


async def test_the_reason_a_download_is_stalled_reaches_the_plane(sessions, cluster) -> None:
    """The whole point of the field: `absent` alone is not an answer anybody can act on."""
    await seed_warm_artifact(sessions, cluster)
    sink = DatabaseSink(sessions)

    await sink.apply_observed(
        observed(
            "cache.observed",
            artifact_id="art_bench",
            state="absent",
            resident=False,
            nodes=[],
            reason="NoRuntimeImage",
            message="no workload on this cluster names an engine image",
        )
    )

    rows = await fetch(
        sessions,
        "SELECT message, details->>'reason' AS reason FROM ops_event WHERE type = 'cache.absent'",
    )
    assert rows[0].reason == "NoRuntimeImage"
    assert "engine image" in rows[0].message


async def test_an_operator_that_sends_no_reason_still_files_the_event(sessions, cluster) -> None:
    """Free text with an empty default, so neither side has to ship first."""
    await seed_warm_artifact(sessions, cluster)
    sink = DatabaseSink(sessions)

    await sink.apply_observed(
        observed("cache.observed", artifact_id="art_bench", state="fetching", resident=False, nodes=[], reason="")
    )

    rows = await fetch(sessions, "SELECT message FROM ops_event WHERE type = 'cache.fetching'")
    assert rows and rows[0].message


async def test_replaying_one_residency_frame_writes_one_event(sessions, cluster) -> None:
    """A resync replays frames, and a timeline that grows on replay is not a timeline."""
    await seed_warm_artifact(sessions, cluster)
    sink = DatabaseSink(sessions)
    frame = observed("cache.observed", artifact_id="art_bench", state="resident", nodes=[])

    await sink.apply_observed(frame)
    await sink.apply_observed(frame)

    rows = await fetch(sessions, "SELECT count(*) AS n FROM ops_event WHERE type = 'cache.resident'")
    assert rows[0].n == 1


async def test_a_workloads_timeline_reaches_the_download_it_is_blocked_on(sessions, cluster) -> None:
    """Hop four, end to end: what the operator said, read back the way the page reads it.

    The event is filed against the artifact, and the page asks by workload. Without
    the subject expansion this returns nothing, which is precisely what the Logs
    tab showed while a workload sat waiting on weights that were never fetched.
    """
    await seed_warm_artifact(sessions, cluster)
    sink = DatabaseSink(sessions)
    await sink.apply_observed(
        observed(
            "cache.observed",
            artifact_id="art_bench",
            state="absent",
            resident=False,
            nodes=[],
            reason="NoRuntimeImage",
        )
    )

    async with sessions() as db:
        repo = EventRepository(db)
        result = await db.execute(repo.list_query(limit=50, subject=SEEDED_WORKLOAD))
        kinds = [row[0].type for row in result.all()]

    assert "cache.absent" in kinds, "the workload's timeline does not reach the weights it waits on"


async def test_a_workloads_timeline_reaches_its_deployments(sessions, cluster) -> None:
    """The other child, and the one every deployment event is already written against."""
    await seed_warm_artifact(sessions, cluster)
    sink = DatabaseSink(sessions)
    await sink.apply_observed(
        observed("deployment.observed", deployment_id=DEPLOYMENT, phase="pending", reason="WeightsNotResident")
    )

    async with sessions() as db:
        repo = EventRepository(db)
        result = await db.execute(repo.list_query(limit=50, subject=SEEDED_WORKLOAD))
        rows = [row[0] for row in result.all()]

    assert any(r.type.startswith("deployment.") for r in rows)
    assert any(r.details.get("reason") == "WeightsNotResident" for r in rows)


async def test_another_workloads_download_stays_out_of_this_ones_timeline(sessions, cluster) -> None:
    """Shared weights are shared; a download on a cluster this workload never reached is not its story."""
    await seed_warm_artifact(sessions, cluster)
    await seed_cluster(sessions, OTHER_CLUSTER, "other")
    sink = DatabaseSink(sessions)

    await sink.apply_observed(
        observed("cache.observed", cluster_uid=OTHER_CLUSTER, artifact_id="art_bench", state="absent", nodes=[])
    )

    async with sessions() as db:
        repo = EventRepository(db)
        result = await db.execute(repo.list_query(limit=50, subject=SEEDED_WORKLOAD))
        kinds = [row[0].type for row in result.all()]

    assert "cache.absent" not in kinds
