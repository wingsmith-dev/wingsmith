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

from app.hub.sink import SinkFrame
from app.core.auth import STATIC_OPERATOR
from app.hub.state import new_id
from app.hub.session import SessionConfig
from app.hub.session import OperatorSession
from app.hub.messages import MESSAGE_TYPES
from app.ingestion.sink import DatabaseSink
from app.ingestion.sink import UnknownCluster
from app.ingestion.desired import stream_desired

from tests.conftest import ROOT
from tests.conftest import CLUSTER
from tests.conftest import fetch
from tests.conftest import seed_cluster
from tests.test_hub_session import FakeConnection
from tests.test_hub_session import frame as wire_frame
from tests.test_hub_session import payload_of


pytestmark = pytest.mark.database


FIXTURES = ROOT.parents[1] / "contract" / "fixtures"

OTHER_CLUSTER = "clu_01j9x2aaaaaaaaaaaaaaaaaaab"
NODE = "nod_01j9x2bbbbbbbbbbbbbbbbbbbb"
DEVICE = "dev_01j9x2cccccccccccccccccccc"
DEVICE_B = "dev_01j9x2cccccccccccccccccccd"
DOMAIN = "tpd_01j9x2dddddddddddddddddddd"

OTHER_NODE = "nod_01j9x2bbbbbbbbbbbbbbbbbbbx"
OTHER_DEVICES = ("dev_01j9x2ccccccccccccccccccex", "dev_01j9x2ccccccccccccccccccey")
OTHER_DOMAIN = "tpd_01j9x2ddddddddddddddddddx"
OPERATION = "opr_01j9x2kkkkkkkkkkkkkkkkkkkk"
COMPONENT = "cmp_01j9x2mmmmmmmmmmmmmmmmmmmm"


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
        sessions, "SELECT reachable, agent_version, connected_at IS NOT NULL, last_error FROM supply_cluster_sync_state"
    )
    assert rows == [(True, "0.4.2", True, None)]

    await sink.mark_disconnected(CLUSTER, "peer went away")

    rows = await fetch(
        sessions,
        "SELECT reachable, agent_version, disconnected_at IS NOT NULL, last_error FROM supply_cluster_sync_state",
    )
    assert rows == [(False, "0.4.2", True, "peer went away")]


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
    assert await fetch(sessions, "SELECT reachable, agent_version FROM supply_cluster_sync_state") == [(True, "0.4.2")]

    await connection.close()
    await asyncio.wait_for(task, timeout=3.0)

    assert await fetch(sessions, "SELECT reachable, disconnected_at IS NOT NULL FROM supply_cluster_sync_state") == [
        (False, True)
    ]
