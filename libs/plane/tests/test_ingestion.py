"""Ingestion, exercised without a database.

There is no Postgres in CI, so the statements the sink builds are compiled and
asserted on instead: which constraint an upsert targets, which columns its
update set is willing to touch, and which payload fields never reach a
parameter at all. The prune guard is unit-tested directly, because it is the
one decision in this package that can delete a cluster's whole inventory.
"""

import json
import uuid
import logging
import datetime as dt

from typing import Any
from pathlib import Path

import pytest

from msgspec import json as msgjson
from msgspec import structs
from sqlalchemy import Delete
from sqlalchemy.dialects import postgresql

from app.core.ids import REPLICA
from app.hub.sink import Sink
from app.hub.sink import SinkFrame
from app.hub.sink import DesiredSource
from app.hub.sink import OperationSource
from app.ingestion import statements
from app.hub.messages import MESSAGE_TYPES
from app.ingestion.sink import DatabaseSink
from app.ingestion.sink import UnknownCluster
from app.ingestion.sink import DatabaseDesiredSource
from app.ingestion.sink import _moved
from app.ingestion.sink import _regressed
from app.ingestion.time import parse_ts
from app.ingestion.resync import NODE
from app.ingestion.resync import DEVICE
from app.ingestion.resync import ResyncEpoch
from app.ingestion.resync import decide
from app.ingestion.desired import format_ts


FIXTURES = Path(__file__).resolve().parents[2] / "packages" / "contract" / "fixtures"

CLUSTER = "clu_01J9X2AAAAAAAAAAAAAAAAAAAA"
CLUSTER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
MOMENT = dt.datetime(2026, 8, 23, 12, 0, tzinfo=dt.UTC)
NODE_UID = "nod_01j9x2bbbbbbbbbbbbbbbbbbbc"


def payload(type_name: str):
    raw = json.loads((FIXTURES / f"{type_name}.json").read_text())["payload"]
    return msgjson.decode(json.dumps(raw).encode(), type=MESSAGE_TYPES[type_name])


def compile_sql(statement) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def scalar_params(params: dict[str, Any]) -> set[Any]:
    return {value for value in params.values() if isinstance(value, (str, int, float, bool, uuid.UUID))}


# --------------------------------------------------------------------------- upserts


def test_node_upsert_targets_the_natural_key() -> None:
    sql, _ = compile_sql(statements.upsert_node(CLUSTER_ID, payload("node.observed")))
    assert "INSERT INTO supply_node" in sql
    assert "ON CONFLICT ON CONSTRAINT uq_supply_node_cluster_name DO UPDATE" in sql


def test_device_upsert_targets_the_natural_key() -> None:
    sql, _ = compile_sql(statements.upsert_device(CLUSTER_ID, payload("device.observed")))
    assert "ON CONFLICT ON CONSTRAINT uq_supply_device_cluster_uuid DO UPDATE" in sql


def test_a_domain_spanning_nodes_stores_every_member() -> None:
    """The domain row carries the fabric; the join table carries the machines."""
    domain = payload("topology.observed")
    assert len(domain.node_ids) > 1 and domain.spans_nodes
    _, params = compile_sql(statements.upsert_topology_domain(CLUSTER_ID, domain))
    assert "node_id" not in params
    assert params["spans_nodes"] is True

    written = [compile_sql(s)[0] for s in statements.replace_domain_nodes(CLUSTER_ID, domain)]
    assert any("INSERT INTO supply_topology_domain_node" in sql for sql in written)
    assert any("DELETE FROM supply_topology_domain_node" in sql for sql in written)


def test_a_domain_on_one_node_is_one_membership_row() -> None:
    domain = structs.replace(
        payload("topology.observed"), node_ids=["nod_01j9x2bbbbbbbbbbbbbbbbbbbb"], spans_nodes=False
    )
    _, params = compile_sql(statements.upsert_topology_domain(CLUSTER_ID, domain))
    assert params["spans_nodes"] is False
    assert len(statements.replace_domain_nodes(CLUSTER_ID, domain)) == 2


def test_a_domain_with_no_named_nodes_only_clears_its_membership() -> None:
    """Nothing to insert is not the same as nothing to remove."""
    domain = structs.replace(payload("topology.observed"), node_ids=[])
    written = statements.replace_domain_nodes(CLUSTER_ID, domain)
    assert len(written) == 1
    assert "DELETE FROM supply_topology_domain_node" in compile_sql(written[0])[0]


def test_membership_insert_never_updates_a_row_that_is_already_there() -> None:
    written = statements.replace_domain_nodes(CLUSTER_ID, payload("topology.observed"))
    sql = compile_sql(written[1])[0]
    assert "ON CONFLICT ON CONSTRAINT uq_supply_topology_domain_node DO NOTHING" in sql


def test_topology_upsert_targets_the_natural_key() -> None:
    sql, _ = compile_sql(statements.upsert_topology_domain(CLUSTER_ID, payload("topology.observed")))
    assert "ON CONFLICT ON CONSTRAINT uq_supply_topology_domain_identity DO UPDATE" in sql


@pytest.mark.parametrize(
    ("builder", "type_name"),
    [
        (statements.upsert_node, "node.observed"),
        (statements.upsert_device, "device.observed"),
        (statements.upsert_topology_domain, "topology.observed"),
    ],
)
def test_uid_is_inserted_but_never_updated(builder, type_name: str) -> None:
    """An id that changes because a cluster reconnected is not an id."""
    sql, params = compile_sql(builder(CLUSTER_ID, payload(type_name)))
    assert "uid" in sql.split("ON CONFLICT")[0]
    assert any(key.startswith("uid") for key in params)
    assert "uid = excluded.uid" not in sql


@pytest.mark.parametrize(
    ("builder", "type_name", "frozen"),
    [
        (statements.upsert_node, "node.observed", ("name", "cluster_id")),
        (statements.upsert_device, "device.observed", ("device_uuid", "cluster_id")),
        (statements.upsert_topology_domain, "topology.observed", ("kind", "identifier", "cluster_id")),
    ],
)
def test_the_natural_key_is_never_in_the_update_set(builder, type_name: str, frozen) -> None:
    sql, _ = compile_sql(builder(CLUSTER_ID, payload(type_name)))
    update_set = sql.split("DO UPDATE SET")[1]
    for column in frozen:
        assert f"{column} = excluded.{column}" not in update_set


def test_observed_uid_stores_the_cluster_minted_id_verbatim() -> None:
    """The cluster names what it observes; the plane does not rename it (ADR 0002)."""
    wire = "nod_01j9x2bbbbbbbbbbbbbbbbbbbb"
    assert statements.observed_uid(wire, "nod_") == wire


@pytest.mark.parametrize(
    "wire",
    [
        "node-7",
        "",
        "nod_01J9X2BBBBBBBBBBBBBBBBBBBB",
        "nod_short",
        "nod_01j9x2bbbbbbbbbbbbbbbbbbbbbb",
    ],
)
def test_a_malformed_id_is_refused_rather_than_replaced(wire: str) -> None:
    """Minting a substitute would hide the defect and split the row's identity."""
    with pytest.raises(statements.MalformedObservation):
        statements.observed_uid(wire, "nod_")


def test_a_wrongly_prefixed_id_is_refused() -> None:
    with pytest.raises(statements.MalformedObservation):
        statements.observed_uid("dev_01j9x2cccccccccccccccccccc", "nod_")


# --------------------------------------------------------------------------- telemetry


TELEMETRY_VALUES = {
    12,  # device.observed ecc.volatile_corrected
}


def test_device_upsert_keeps_state_and_drops_measurements() -> None:
    device = payload("device.observed")
    _, params = compile_sql(statements.upsert_device(CLUSTER_ID, device))
    values = scalar_params(params)
    assert device.memory_total_mb in values
    assert not hasattr(device, "memory_used_mb")
    assert device.ecc.volatile_corrected not in values
    assert device.ecc.volatile_uncorrected in values


def test_no_statement_carries_a_telemetry_column() -> None:
    built = [
        statements.upsert_node(CLUSTER_ID, payload("node.observed")),
        statements.upsert_device(CLUSTER_ID, payload("device.observed")),
        statements.upsert_topology_domain(CLUSTER_ID, payload("topology.observed")),
        statements.update_operation(CLUSTER_ID, payload("operation.observed")),
    ]
    forbidden = ("utilization", "temperature", "throughput", "tokens", "power", "memory_used", "progress")
    for statement in built:
        sql, params = compile_sql(statement)
        for word in forbidden:
            assert word not in sql
        assert not TELEMETRY_VALUES & scalar_params(params)


def test_capacity_observed_is_a_rollup_and_has_no_statement() -> None:
    assert not hasattr(statements, "upsert_capacity")


# --------------------------------------------------------------------------- the demand side

DEPLOYMENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
WORKLOAD_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
PROJECT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
REPLICA_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
ARTIFACT_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
ALLOCATION_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")


def test_deployment_status_is_its_own_row_and_never_the_declared_one() -> None:
    """Declared and observed have different writers, so they cannot share a row."""
    sql, _ = compile_sql(statements.upsert_deployment_status(CLUSTER_ID, DEPLOYMENT_ID, payload("deployment.observed")))
    assert "INSERT INTO demand_deployment_status" in sql
    assert "demand_deployment " not in sql
    assert "min_replicas" not in sql and "max_replicas" not in sql


def test_deployment_status_records_the_generation_it_observed() -> None:
    """Four ready at the old generation and four at the new one are different facts."""
    observed = payload("deployment.observed")
    _, params = compile_sql(statements.upsert_deployment_status(CLUSTER_ID, DEPLOYMENT_ID, observed))
    assert observed.generation in scalar_params(params)


def test_deployment_status_flattens_the_last_error() -> None:
    observed = payload("deployment.observed")
    assert observed.last_error is not None
    _, params = compile_sql(statements.upsert_deployment_status(CLUSTER_ID, DEPLOYMENT_ID, observed))
    values = scalar_params(params)
    assert observed.last_error.code in values
    assert observed.last_error.message in values


def test_admission_lands_on_the_deployment_and_not_the_workload() -> None:
    """`admission_phase` was written once, as `placed`, and never again.

    Every deployment this plane created therefore read `placed` for its whole
    life, serving included, and only an adopted workload ever read `running`.
    It is written per-cluster: one workload has N of these and they disagree, so
    a write aimed at the workload would let the last cluster to report stomp the
    rest.
    """
    observed = structs.replace(payload("deployment.observed"), phase="running")
    statement = statements.advance_admission_phase(DEPLOYMENT_ID, observed)
    assert statement is not None
    sql, params = compile_sql(statement)
    assert "UPDATE demand_deployment" in sql
    assert "UPDATE demand_workload" not in sql
    assert "running" in scalar_params(params)


def test_a_degraded_deployment_is_still_a_running_workload() -> None:
    """Admission says how far through the scheduler it got, not how healthy it is."""
    observed = structs.replace(payload("deployment.observed"), phase="degraded")
    statement = statements.advance_admission_phase(DEPLOYMENT_ID, observed)
    assert statement is not None
    _, params = compile_sql(statement)
    assert "running" in scalar_params(params)


def test_waiting_on_a_dependency_does_not_unplace_a_workload() -> None:
    """Placement already chose a cluster, and cold weights do not un-choose it."""
    observed = structs.replace(payload("deployment.observed"), phase="pending", reason="WeightsNotResident")
    statement = statements.advance_admission_phase(DEPLOYMENT_ID, observed)
    assert statement is not None
    _, params = compile_sql(statement)
    values = scalar_params(params)
    assert "placed" in values and "pending" not in values


def test_a_workload_that_ran_keeps_the_earliest_time_any_cluster_started() -> None:
    """Derived phases go backwards; the first time it ran is a fact that does not.

    The rollup is a `min()` and not a latch, because N clusters report in no
    order and every one of them may only move the moment backwards.
    """
    observed = structs.replace(payload("deployment.observed"), phase="running")
    sql, _ = compile_sql(statements.roll_up_workload_started(WORKLOAD_ID, observed))
    assert "UPDATE demand_workload" in sql
    assert "least" in sql.lower() and "coalesce" in sql.lower()


def test_admission_never_writes_when_it_started() -> None:
    """Phase is per-cluster and `started_at` is global; they are two statements."""
    observed = structs.replace(payload("deployment.observed"), phase="running")
    statement = statements.advance_admission_phase(DEPLOYMENT_ID, observed)
    assert statement is not None
    sql, _ = compile_sql(statement)
    assert "started_at" not in sql


def test_a_terminating_deployment_leaves_the_workload_alone() -> None:
    """Being torn down is between phases, and the delete that follows is what ends it."""
    observed = structs.replace(payload("deployment.observed"), phase="terminating")
    assert statements.advance_admission_phase(DEPLOYMENT_ID, observed) is None


def test_replica_upsert_targets_the_natural_key() -> None:
    sql, _ = compile_sql(statements.upsert_replica(CLUSTER_ID, DEPLOYMENT_ID, payload("replica.observed")))
    assert "ON CONFLICT ON CONSTRAINT uq_demand_replica_cluster_name DO UPDATE" in sql


def test_replica_stores_the_runtime_version_and_not_the_engine() -> None:
    """The engine is the workload's; the version is what lags during a rollout."""
    observed = payload("replica.observed")
    _, params = compile_sql(statements.upsert_replica(CLUSTER_ID, DEPLOYMENT_ID, observed))
    values = scalar_params(params)
    assert observed.engine_runtime_version in values
    assert observed.engine not in values


def test_replica_does_not_write_the_devices_it_holds() -> None:
    """A replica's commitment is an allocation, not a column on the replica."""
    sql, _ = compile_sql(statements.upsert_replica(CLUSTER_ID, DEPLOYMENT_ID, payload("replica.observed")))
    assert "device" not in sql


def _flat(params: dict) -> list:
    """Bound values, with an `IN` expansion's tuple opened out."""
    out: list = []
    for value in params.values():
        out.extend(value) if isinstance(value, (list, tuple, set)) else out.append(value)
    return out


def test_replicas_are_pruned_against_the_ids_the_deployment_frame_names() -> None:
    """Scoped to one cluster and one deployment, because that is what the frame claims."""
    observed = payload("replica.observed")
    sql, params = compile_sql(statements.prune_deployment_replicas(CLUSTER_ID, DEPLOYMENT_ID, [observed.replica_id]))
    assert sql.upper().startswith("DELETE FROM DEMAND_REPLICA")
    assert "cluster_id" in sql and "deployment_id" in sql
    assert observed.replica_id in _flat(params)


def test_pruning_against_an_empty_set_removes_every_replica() -> None:
    """A deployment scaled to zero says it has none, and none is a number."""
    sql, params = compile_sql(statements.prune_deployment_replicas(CLUSTER_ID, DEPLOYMENT_ID, []))
    assert sql.upper().startswith("DELETE FROM DEMAND_REPLICA")
    assert not [v for v in _flat(params) if isinstance(v, str) and v.startswith(REPLICA)]


def test_allocation_uses_the_cluster_minted_id() -> None:
    """An observed row is named by the cluster that discovered it (ADR 0002)."""
    observed = payload("allocation.observed")
    sql, params = compile_sql(
        statements.upsert_allocation(CLUSTER_ID, WORKLOAD_ID, DEPLOYMENT_ID, PROJECT_ID, REPLICA_ID, observed)
    )
    assert observed.allocation_id in scalar_params(params)
    assert "uid = excluded.uid" not in sql


def test_allocation_reads_its_device_class_back_from_the_devices() -> None:
    """Having the cluster restate it would create a second place for it to be wrong."""
    observed = payload("allocation.observed")
    sql, params = compile_sql(
        statements.upsert_allocation(CLUSTER_ID, WORKLOAD_ID, DEPLOYMENT_ID, PROJECT_ID, REPLICA_ID, observed)
    )
    assert "SELECT supply_device.device_class" in sql
    assert len(observed.device_ids) in scalar_params(params)


def test_allocation_records_which_cluster_copy_holds_the_devices() -> None:
    """A workload fanned out across regions holds accelerators in each of them."""
    observed = payload("allocation.observed")
    sql, _ = compile_sql(
        statements.upsert_allocation(CLUSTER_ID, WORKLOAD_ID, DEPLOYMENT_ID, PROJECT_ID, REPLICA_ID, observed)
    )
    assert "deployment_id" in sql
    assert "workload_id" in sql


def test_dropping_a_device_releases_it_rather_than_deleting_the_row() -> None:
    """The partial unique index frees the accelerator; the history stays."""
    sql, _ = compile_sql(
        statements.release_allocation_devices(ALLOCATION_ID, CLUSTER_ID, payload("allocation.observed"))
    )
    assert "UPDATE bind_allocation_device SET" in sql and "released_at=" in sql
    assert "DELETE" not in sql
    assert "bind_allocation_device.device_id NOT IN" in sql


def test_attaching_a_device_takes_back_one_an_earlier_pass_released() -> None:
    """A device named by a live allocation is held by it, whatever an earlier frame said."""
    sql, _ = compile_sql(
        statements.attach_allocation_devices(ALLOCATION_ID, CLUSTER_ID, payload("allocation.observed"))
    )
    assert "ON CONFLICT ON CONSTRAINT uq_bind_allocation_device DO UPDATE" in sql
    assert "SET released_at" in sql
    assert "DO NOTHING" not in sql


def test_an_allocation_holding_nothing_is_stamped_released_not_deleted() -> None:
    """The commitment stays auditable and stops answering "what is held right now"."""
    sql, _ = compile_sql(statements.release_allocation(ALLOCATION_ID, payload("allocation.observed")))
    assert "UPDATE bind_allocation SET" in sql
    assert "released_at=" in sql
    assert "DELETE" not in sql
    assert "device_count" not in sql


def test_cache_residency_is_written_per_node_and_for_the_cluster() -> None:
    """A router has to pick a node that holds the weights, not a cluster that does."""
    observed = payload("cache.observed")
    assert len(observed.nodes) == 2
    built = statements.upsert_cache_entries(CLUSTER_ID, ARTIFACT_ID, observed)
    assert len(built) == 1 + len(observed.nodes)

    _, cluster_params = compile_sql(built[0])
    assert "cluster" in scalar_params(cluster_params)
    assert cluster_params["node_id"] is None
    for statement in built[1:]:
        node_sql, node_params = compile_sql(statement)
        assert "SELECT supply_node.id" in node_sql
        assert "node" in scalar_params(node_params)


def test_cache_entries_upsert_rather_than_append() -> None:
    """`node_id` is null for the cluster row, so the constraint must be NULLS NOT DISTINCT."""
    for statement in statements.upsert_cache_entries(CLUSTER_ID, ARTIFACT_ID, payload("cache.observed")):
        sql, _ = compile_sql(statement)
        assert "ON CONFLICT ON CONSTRAINT uq_ops_cache_entry_scope DO UPDATE" in sql


def test_a_node_mid_fetch_is_not_recorded_as_resident() -> None:
    observed = payload("cache.observed")
    fetching = next(node for node in observed.nodes if node.state == "fetching")
    built = statements.upsert_cache_entries(CLUSTER_ID, ARTIFACT_ID, observed)
    states = [compile_sql(statement)[1]["state"] for statement in built]
    assert fetching.state in states
    assert states.count("resident") == 2


# --------------------------------------------------------------------------- declared state


def test_sync_state_never_writes_the_declared_cluster_row() -> None:
    sql, _ = compile_sql(statements.upsert_sync_state(CLUSTER_ID, {"reachable": True}, bump=True))
    assert "INSERT INTO supply_cluster_sync_state" in sql
    assert "supply_cluster " not in sql
    assert "phase" not in sql
    assert "observed_generation = (supply_cluster_sync_state.observed_generation + " in sql


def test_operation_is_updated_and_never_created() -> None:
    sql, _ = compile_sql(statements.update_operation(CLUSTER_ID, payload("operation.observed")))
    assert sql.startswith("UPDATE ops_operation")
    assert "INSERT" not in sql


def test_node_upsert_omits_columns_the_wire_does_not_carry() -> None:
    """Absent is not the same as null: an upsert must not erase a later frame's work."""
    sql, _ = compile_sql(statements.upsert_node(CLUSTER_ID, payload("node.observed")))
    for column in ("joined_at", "runtime_version"):
        assert column not in sql


def test_node_upsert_writes_the_machine_shape_when_the_frame_carries_it() -> None:
    sql, params = compile_sql(statements.upsert_node(CLUSTER_ID, payload("node.observed")))
    for column in ("instance_type", "cloud", "cpu_cores", "memory_mib", "cpu_arch", "os_image", "taints"):
        assert column in sql
    assert "p5.48xlarge" in params.values()
    assert 192 in params.values()


def test_node_upsert_leaves_the_machine_shape_alone_when_the_frame_omits_it() -> None:
    """An operator too old to describe the machine must not blank what a newer one wrote."""
    older = structs.replace(
        payload("node.observed"),
        instance_type="",
        cloud="",
        zone="",
        region="",
        cpu_cores=0,
        memory_mib=0,
        cpu_arch="",
        os_image="",
        taints=None,
    )
    sql, _ = compile_sql(statements.upsert_node(CLUSTER_ID, older))
    for column in ("instance_type", "cloud", "cpu_cores", "memory_mib", "cpu_arch", "os_image", "taints"):
        assert column not in sql


def test_cluster_observed_lands_on_the_sync_state_and_never_the_registry_row() -> None:
    """Observed cluster attributes have one writer, and it is not the API."""
    sql, params = compile_sql(statements.upsert_cluster_observed(CLUSTER_ID, payload("cluster.observed")))
    assert "INSERT INTO supply_cluster_sync_state" in sql
    assert "supply_cluster " not in sql
    assert "eks" in params.values()
    assert "aws" in params.values()


def test_cluster_observed_carries_whether_the_operator_reconciles() -> None:
    """`read_only` is the operator's posture and only the operator knows it.

    The plane renders `cluster.readOnly` into the install values and then forgets
    it, and that field is the one the header invites an operator to override — so
    what was rendered is not evidence of what is running.
    """
    frame = payload("cluster.observed")
    sql, params = compile_sql(statements.upsert_cluster_observed(CLUSTER_ID, frame))
    assert "read_only" in sql
    assert params["read_only"] is False

    frame.read_only = True
    _, params = compile_sql(statements.upsert_cluster_observed(CLUSTER_ID, frame))
    assert params["read_only"] is True


def test_cluster_observed_carries_whether_a_replica_can_span_machines() -> None:
    """`multinode` is discovered from the cluster's API groups, so it can change
    under a running operator: installing Grove makes a cluster capable without
    anything being redeclared, and the next frame has to say so.
    """
    frame = payload("cluster.observed")
    sql, params = compile_sql(statements.upsert_cluster_observed(CLUSTER_ID, frame))
    assert "multinode" in sql
    assert params["multinode"] is False

    frame.multinode = True
    _, params = compile_sql(statements.upsert_cluster_observed(CLUSTER_ID, frame))
    assert params["multinode"] is True


def test_cluster_observed_does_not_overwrite_the_declared_region() -> None:
    sql, _ = compile_sql(statements.upsert_cluster_observed(CLUSTER_ID, payload("cluster.observed")))
    assert "observed_region" in sql


# --------------------------------------------------------------------------- events


def test_event_insert_is_guarded_against_replay() -> None:
    """Enforced by the index, not by reading first: two sessions can both read nothing."""
    sql, _ = compile_sql(statements.insert_node_event(CLUSTER_ID, payload("node.event")))
    assert "INSERT INTO ops_event" in sql
    assert "ON CONFLICT (cluster_id, type, subject_type, occurred_at, md5(message)) DO NOTHING" in sql
    assert "EXISTS" not in sql


def test_event_insert_mints_a_public_id() -> None:
    sql, params = compile_sql(statements.insert_node_event(CLUSTER_ID, payload("node.event")))
    assert "uid" in sql.split("SELECT")[0]
    assert any(isinstance(v, str) and v.startswith("evt_") for v in params.values())


def test_device_event_resolves_its_subject() -> None:
    sql, _ = compile_sql(statements.insert_device_event(CLUSTER_ID, payload("device.event")))
    assert "SELECT supply_device.id" in sql


def test_a_departed_node_stops_claiming_to_be_ready() -> None:
    """A `left` event has to change the node, not just be filed next to it.

    Until this existed the plane recorded the departure and left the row saying
    the node was ready, so it stayed a placement candidate until a completed
    resync happened to prune it — which, on a session that never dropped, was
    never.
    """
    sql, params = compile_sql(statements.mark_node_left(CLUSTER_ID, NODE_UID, MOMENT))
    assert "UPDATE supply_node SET" in sql
    assert "condition" in sql
    assert "ready" in sql
    assert "left" in params.values()
    assert False in params.values()


def test_a_departed_node_takes_its_devices_health_with_it() -> None:
    """A device is only observable through its node; once the node is gone, nothing
    can still vouch for the device."""
    sql, params = compile_sql(statements.orphan_devices(CLUSTER_ID, NODE_UID, MOMENT))
    assert "UPDATE supply_device SET" in sql
    assert "unknown" in params.values()
    assert False in params.values()
    assert "SELECT supply_node.id" in sql


def test_prune_statements_are_scoped_to_one_cluster() -> None:
    for builder in (statements.prune_nodes, statements.prune_devices, statements.prune_topology_domains):
        sql, params = compile_sql(builder(CLUSTER_ID, {"nod_1"}))
        assert "DELETE FROM" in sql
        assert "cluster_id = " in sql
        assert CLUSTER_ID in scalar_params(params)


# --------------------------------------------------------------------------- prune guard


def epoch(**kwargs: Any) -> ResyncEpoch:
    return ResyncEpoch(cluster_uid=CLUSTER, scope="supply", request_id="req", **kwargs)


def test_no_epoch_never_prunes() -> None:
    assert decide(None, 0, True).prune is False


def test_incomplete_snapshot_never_prunes() -> None:
    open_epoch = epoch(received=3)
    assert decide(open_epoch, 3, False).prune is False


def test_count_mismatch_never_prunes() -> None:
    open_epoch = epoch(received=2)
    decision = decide(open_epoch, 3, True)
    assert decision.prune is False
    assert "truncated" in decision.reason


def test_a_failed_apply_poisons_the_epoch() -> None:
    open_epoch = epoch(received=3)
    open_epoch.poison()
    assert decide(open_epoch, 3, True).prune is False


def test_an_empty_but_whole_snapshot_prunes() -> None:
    assert decide(epoch(), 0, True).prune is True


def test_a_whole_snapshot_prunes() -> None:
    open_epoch = epoch()
    open_epoch.record(NODE, "nod_a")
    open_epoch.record(DEVICE, "dev_a")
    assert decide(open_epoch, 2, True).prune is True


# --------------------------------------------------------------------------- sink behaviour


class FakeResult:
    """Answers the two shapes the sink reads: a scalar lookup and a row set.

    `all()` returns nothing, which is the honest stand-in here — this fake records
    statements rather than executing them, so a prune it never ran has no rows to
    have deleted and therefore no removal events to raise.
    """

    def __init__(self, scalar: Any = None) -> None:
        self._scalar = scalar
        self.rowcount = 0

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def all(self) -> list:
        return []


class FakeSession:
    """Records statements and answers the one lookup the sink makes."""

    def __init__(self, owner: "FakeSessions") -> None:
        self.owner = owner

    async def execute(self, statement) -> FakeResult:
        self.owner.executed.append(statement)
        if self.owner.fail:
            raise RuntimeError("write failed")
        return FakeResult(self.owner.cluster_id)

    async def scalar(self, statement) -> Any:
        self.owner.executed.append(statement)
        return self.owner.cluster_uid


class FakeTransaction:
    def __init__(self, owner: "FakeSessions") -> None:
        self.owner = owner

    async def __aenter__(self) -> FakeSession:
        return FakeSession(self.owner)

    async def __aexit__(self, *exc: object) -> bool:
        if exc[0] is None:
            self.owner.commits += 1
        return False


class FakeSessions:
    def __init__(self, cluster_id: Any = CLUSTER_ID, fail: bool = False, cluster_uid: str = CLUSTER) -> None:
        self.executed: list[Any] = []
        self.commits = 0
        self.cluster_id = cluster_id
        self.cluster_uid = cluster_uid
        self.fail = fail

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)

    def __call__(self) -> FakeTransaction:
        return FakeTransaction(self)


def observed_frame(type_name: str, reply_to: str | None = None) -> SinkFrame:
    return SinkFrame(cluster_id=CLUSTER, type=type_name, payload=payload(type_name), seq=1, reply_to=reply_to)


def malformed_frame(reply_to: str | None = None) -> SinkFrame:
    """A `node.observed` whose id the plane can never store."""
    frame = observed_frame("node.observed", reply_to=reply_to)
    broken = structs.replace(frame.payload, node_id="node-7")
    return structs.replace(frame, payload=broken)


def test_the_database_sink_satisfies_the_protocols() -> None:
    assert isinstance(DatabaseSink(), Sink)
    assert isinstance(DatabaseDesiredSource(), DesiredSource)
    assert isinstance(DatabaseDesiredSource(), OperationSource)


async def test_applying_one_frame_commits_once() -> None:
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed_frame("node.observed"))
    assert sessions.commits == 1


async def test_a_malformed_id_is_acknowledged_and_dropped_not_replayed() -> None:
    """A poison frame would come back forever; the stream behind it must advance.

    The contrast with the unknown-cluster case below is the whole point: that one
    raises, because a cluster the plane has not enrolled yet is a condition a
    later retry can resolve.
    """
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)

    await sink.apply_observed(malformed_frame())

    assert sessions.commits == 0


async def test_a_dropped_frame_still_poisons_the_open_epoch() -> None:
    """Absence from local state is not evidence when a frame did not land."""
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.begin_resync(CLUSTER, "supply", "req-1")
    await sink.apply_observed(malformed_frame(reply_to="req-1"))
    await sink.end_resync(CLUSTER, "req-1", count=1, complete=True)

    assert not [stmt for stmt in sessions.executed if isinstance(stmt, Delete)]


async def test_an_unknown_cluster_raises_rather_than_acknowledging() -> None:
    sink = DatabaseSink(FakeSessions(cluster_id=None))
    with pytest.raises(UnknownCluster):
        await sink.apply_observed(observed_frame("node.observed"))


async def test_a_failed_write_raises_and_poisons_the_open_epoch() -> None:
    sessions = FakeSessions(fail=True)
    sink = DatabaseSink(sessions)
    await sink.begin_resync(CLUSTER, "supply", "req-1")
    with pytest.raises(RuntimeError):
        await sink.apply_observed(observed_frame("node.observed", reply_to="req-1"))
    assert sink._epochs["req-1"].poisoned is True
    assert sessions.commits == 0


async def test_a_poisoned_epoch_closes_without_pruning() -> None:
    sessions = FakeSessions(fail=True)
    sink = DatabaseSink(sessions)
    await sink.begin_resync(CLUSTER, "supply", "req-1")
    with pytest.raises(RuntimeError):
        await sink.apply_observed(observed_frame("node.observed", reply_to="req-1"))
    await sink.end_resync(CLUSTER, "req-1", 1, True)
    assert not [s for s in sessions.executed if "DELETE" in str(s).upper()]


async def test_an_untracked_type_still_counts_toward_the_snapshot() -> None:
    """A type ingestion does not write yet still arrived; the count has to agree."""
    sink = DatabaseSink(FakeSessions())
    await sink.begin_resync(CLUSTER, "supply", "req-2")
    await sink.apply_observed(observed_frame("capacity.observed", reply_to="req-2"))
    assert sink._epochs["req-2"].received == 1
    assert sink._epochs["req-2"].present[NODE] == set()


async def test_a_whole_snapshot_issues_one_scoped_delete_per_pruned_kind() -> None:
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.begin_resync(CLUSTER, "supply", "req-3")
    await sink.apply_observed(observed_frame("node.observed", reply_to="req-3"))
    await sink.end_resync(CLUSTER, "req-3", 1, True)
    deletes = [s for s in sessions.executed if str(s).upper().startswith("DELETE")]
    assert len(deletes) == 4


async def test_a_truncated_snapshot_deletes_nothing() -> None:
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.begin_resync(CLUSTER, "supply", "req-4")
    await sink.apply_observed(observed_frame("node.observed", reply_to="req-4"))
    await sink.end_resync(CLUSTER, "req-4", 9, True)
    assert not [s for s in sessions.executed if str(s).upper().startswith("DELETE")]


async def test_a_demand_resync_prunes_the_replicas_and_allocations_it_did_not_name(caplog) -> None:
    """The gap this closes: demand state that nothing would ever collect again.

    It used to be reaped only by `deployment.observed.replica_ids` — a claim
    carried per deployment, on a frame that stops arriving the moment the
    deployment is torn down. A paused workload's replicas stayed `ready` and the
    cards under them stayed held, for ever. A complete demand snapshot is now
    evidence in its own right, exactly as the supply one is.
    """
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.begin_resync(CLUSTER, "demand", "req-6")
    with caplog.at_level(logging.INFO):
        await sink.end_resync(CLUSTER, "req-6", 0, True)

    deletes = [str(s) for s in sessions.executed if str(s).upper().startswith("DELETE")]
    assert any("demand_replica" in d for d in deletes)
    assert any("bind_allocation" in d for d in deletes)
    assert any("demand_pruned" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_an_incomplete_demand_resync_prunes_nothing(caplog) -> None:
    """The guard is the same one the supply side has, and it is the whole safety story.

    A snapshot that did not arrive whole is indistinguishable from a cluster that
    has stopped running everything, and deleting on that reading is the one
    outcome this code exists to prevent.
    """
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.begin_resync(CLUSTER, "demand", "req-9")
    with caplog.at_level(logging.WARNING):
        await sink.end_resync(CLUSTER, "req-9", 3, False)

    assert not [s for s in sessions.executed if str(s).upper().startswith("DELETE")]
    assert any("no_prune" in r.getMessage() for r in caplog.records)


async def test_a_supply_only_resync_leaves_demand_state_alone(caplog) -> None:
    """A snapshot may only be evidence about what it claimed to carry."""
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.begin_resync(CLUSTER, "supply", "req-7")
    with caplog.at_level(logging.INFO):
        await sink.end_resync(CLUSTER, "req-7", 0, True)

    deletes = [str(s) for s in sessions.executed if str(s).upper().startswith("DELETE")]
    assert not any("demand_replica" in d or "bind_allocation" in d for d in deletes)


async def test_a_full_resync_prunes_both_halves(caplog) -> None:
    """`all` carries both scopes, so both prunes belong to it."""
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.begin_resync(CLUSTER, "all", "req-8")
    await sink.apply_observed(observed_frame("node.observed", reply_to="req-8"))
    with caplog.at_level(logging.INFO):
        await sink.end_resync(CLUSTER, "req-8", 1, True)

    deletes = [str(s) for s in sessions.executed if str(s).upper().startswith("DELETE")]
    assert any("supply_node" in d for d in deletes)
    assert any("demand_replica" in d for d in deletes)
    assert any("bind_allocation" in d for d in deletes)


async def test_frames_outside_an_epoch_are_not_counted() -> None:
    sink = DatabaseSink(FakeSessions())
    await sink.begin_resync(CLUSTER, "supply", "req-5")
    await sink.apply_observed(observed_frame("node.observed"))
    assert sink._epochs["req-5"].received == 0


async def test_marking_a_cluster_connected_writes_only_sync_state() -> None:
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.mark_connected(CLUSTER, "0.4.2")
    written = " ".join(str(s) for s in sessions.executed)
    assert "supply_cluster_sync_state" in written
    assert "UPDATE supply_cluster " not in written


# --------------------------------------------------------------------------- time


def test_wire_timestamps_round_trip_to_the_millisecond() -> None:
    parsed = parse_ts("2026-08-19T14:02:11.400Z")
    assert parsed is not None
    assert format_ts(parsed) == "2026-08-19T14:02:11.400Z"


def test_an_unparseable_timestamp_is_dropped_not_raised() -> None:
    assert parse_ts("not a time") is None
    assert parse_ts(None) is None


# --------------------------------------------------------------- deployment history


def test_a_reconcile_conclusion_is_filed_as_an_event() -> None:
    """The status row is last-writer-wins, so it is the only record of how a deployment got there."""
    observed = payload("deployment.observed")
    sql, params = compile_sql(statements.deployment_progressed(CLUSTER_ID, DEPLOYMENT_ID, observed))
    assert "INSERT INTO ops_event" in sql
    values = scalar_params(params)
    assert f"deployment.{observed.phase}" in values
    assert DEPLOYMENT_ID in values


def test_a_deployment_event_survives_being_replayed() -> None:
    """The stream is at-least-once, so a repeated frame must land on the row it already wrote."""
    sql, _ = compile_sql(statements.deployment_progressed(CLUSTER_ID, DEPLOYMENT_ID, payload("deployment.observed")))
    assert "ON CONFLICT (cluster_id, type, subject_type, occurred_at, md5(message)) DO NOTHING" in sql


def test_a_capacity_refusal_outranks_the_severity_of_its_phase() -> None:
    """Waiting for a download and having nowhere to put the work are both `pending`."""
    observed = structs.replace(payload("deployment.observed"), phase="pending", reason="NoSchedulableCapacity")
    _, params = compile_sql(statements.deployment_progressed(CLUSTER_ID, DEPLOYMENT_ID, observed))
    assert "warning" in scalar_params(params)

    waiting = structs.replace(payload("deployment.observed"), phase="pending", reason="WeightsNotResident")
    _, params = compile_sql(statements.deployment_progressed(CLUSTER_ID, DEPLOYMENT_ID, waiting))
    assert "warning" not in scalar_params(params)


def test_a_frame_that_restates_the_stored_row_is_not_filed_again() -> None:
    """A hub failover replays the current state of every deployment; none of it is news."""
    observed = payload("deployment.observed")
    same = _Stored(
        observed.phase,
        observed.reason,
        observed.message,
        observed.ready_replicas,
        last_error_code=observed.last_error.code if observed.last_error else None,
    )
    assert not _moved(same, observed)


def test_a_rollout_that_only_moves_replica_counts_is_still_news() -> None:
    """Two of four ready becoming four of four is the transition somebody is waiting on."""
    observed = payload("deployment.observed")
    before = _Stored(observed.phase, observed.reason, observed.message, observed.ready_replicas - 1)
    assert _moved(before, observed)


def test_the_first_frame_for_a_deployment_is_always_news() -> None:
    assert _moved(None, payload("deployment.observed"))


def test_a_replayed_stage_does_not_drag_a_serving_deployment_back_to_pending() -> None:
    """The operator's stage memory is per process, so a restart or a fresh hub session replays
    `Accepted` for a deployment that has been serving for a week. The status row is
    last-writer-wins, so applying it overwrote `running 1/1` and the console showed a healthy
    deployment as still coming up until the next pass put it back."""
    observed = payload("deployment.observed")
    stage = structs.replace(observed, phase="pending", reason="Accepted", ready_replicas=0)
    serving = _Stored("running", "Serving", "1 of 1 replicas ready", 1, stage.generation)
    assert _regressed(serving, stage)


def test_a_stage_for_a_new_generation_is_not_stale() -> None:
    """A redeploy walks the stages again, and every one of them is news."""
    observed = payload("deployment.observed")
    stage = structs.replace(observed, phase="pending", reason="Accepted")
    serving = _Stored("running", "Serving", "ready", 1, stage.generation - 1)
    assert not _regressed(serving, stage)


def test_a_deployment_that_genuinely_fails_is_never_refused() -> None:
    """Only `pending` is refused. A failure is not a stage and must always land."""
    observed = payload("deployment.observed")
    failed = structs.replace(observed, phase="failed", reason="RenderFailed")
    serving = _Stored("running", "Serving", "ready", 1, failed.generation)
    assert not _regressed(serving, failed)


def test_a_deployment_still_coming_up_accepts_its_next_stage() -> None:
    """Pending to pending is the normal walk; nothing has been placed yet to be dragged back."""
    observed = payload("deployment.observed")
    stage = structs.replace(observed, phase="pending", reason="Rendering")
    waiting = _Stored("pending", "Accepted", "reconciling", 0, stage.generation)
    assert not _regressed(waiting, stage)


def test_the_first_frame_for_a_deployment_is_never_stale() -> None:
    assert not _regressed(None, payload("deployment.observed"))


class _Stored:
    """The columns `deployment_status_before` reads back."""

    def __init__(
        self,
        phase: str,
        reason: str | None,
        message: str | None,
        ready_replicas: int,
        observed_generation: int = 0,
        last_error_code: str | None = None,
    ) -> None:
        self.last_error_code = last_error_code
        self.phase = phase
        self.reason = reason
        self.message = message
        self.ready_replicas = ready_replicas
        self.observed_generation = observed_generation


def test_cache_observed_files_an_event():
    """Residency had a row and no timeline; a stuck download left no trace."""
    frame = structs.replace(payload("cache.observed"), state="absent", reason="NoRuntimeImage")
    text = str(statements.cache_progressed(uuid.uuid4(), uuid.uuid4(), frame))
    assert "INSERT INTO ops_event" in text


def test_a_cache_event_is_filed_against_the_artifact_not_the_deployment():
    """One download serves every deployment of those weights on the cluster."""
    stmt = statements.cache_progressed(uuid.uuid4(), uuid.uuid4(), payload("cache.observed"))
    assert "artifact" in str(stmt.compile().params.values())


def test_a_cache_event_replay_lands_on_the_row_it_already_wrote():
    text = str(statements.cache_progressed(uuid.uuid4(), uuid.uuid4(), payload("cache.observed")))
    assert "ON CONFLICT" in text


def test_a_diagnosis_arriving_on_an_unchanged_shortfall_is_news() -> None:
    """The frame that finally says why a deployment is stuck carries the phase, reason and counts
    the deployment already had — it is the `last_error` that is new. Compared without it, the one
    frame anybody was waiting for is the one suppressed as a repeat."""
    observed = payload("deployment.observed")
    before = _Stored(observed.phase, observed.reason, observed.message, observed.ready_replicas)
    assert observed.last_error is not None, "the fixture carries no error to notice"
    assert _moved(before, observed)


# --- the served model name on the wire ---------------------------------------


def _workload(**kwargs):
    """A workload carrying the serving configuration a recipe used to hold.

    The name is still the fit — `kimi-k3-mxfp4-tp8-1024k` — because that is the
    string these tests exist to keep off the wire. It is a workload's name now
    rather than a recipe's, and it is no more a model id than it was.
    """
    from app.db.models import Workload

    base = {
        "uid": "wkl_01j9x2ffffffffffffffffffff",
        "name": "kimi-k3-mxfp4-tp8-1024k",
        "engine": "vllm",
        "engine_runtime_version": "0.6.0",
        "engine_runtime_image": None,
        "frontend_image": None,
        "engine_args": {},
        "quantization": None,
        "dtype": None,
        "tensor_parallel": 8,
        "pipeline_parallel": 1,
        "expert_parallel": 1,
        "context_length": 1048576,
        "max_batch_tokens": None,
        "kv_cache_dtype": None,
        "kv_cache_fraction": None,
        "prefix_cache_enabled": True,
        "prefix_cache_config": {},
        "device_class": "h200",
        "device_count": 8,
        "device_memory_mib_min": 0,
        "cpu_cores": None,
        "memory_mib": None,
        "disk_gib": None,
        "labels": {},
        "overlay": {},
        "serving": {},
        "serving_digest": "svg_" + "0" * 64,
        "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        "updated_at": None,
    }
    return Workload(**(base | kwargs))


def _kimi_artifact():
    from app.db.models import Artifact

    return Artifact(name="moonshotai/Kimi-K3", digest="sha256:beef", slug="moonshotai/kimi-k3")


def _wire_serving(**kwargs):
    """The `ServingSpec` inlined on `deployment.desired`, built the way the sender builds it."""
    from app.ingestion.desired import _serving

    return _serving(_workload(**kwargs), _kimi_artifact())


def _wire_served_model(**kwargs):
    """The `served_model_name` the sender puts on the frame beside that spec.

    It is resolved on the deployment rather than inside the serving block: the
    block is what renders, and the name is what a caller asks for.
    """
    from app.core.serving import declared_served_model

    return declared_served_model(_workload(**kwargs), _kimi_artifact())


def test_the_wire_carries_the_artifact_slug_when_no_one_declared_a_name() -> None:
    """The name that describes the fit must not reach a caller."""
    assert _wire_served_model() == "moonshotai/kimi-k3"


def test_the_workload_cannot_declare_a_name_over_the_wire() -> None:
    """The frame carries the artifact's slug and there is no second field to disagree with it.

    A workload used to be able to declare its own `served_model_name`, which the sender preferred
    over the artifact. Two writeable spellings of one public identifier is how the id a client
    sends and the id a frontend registered come apart, and that failure is invisible from both
    ends: a healthy deployment behind an open tunnel answering `model_not_found` from inside the
    cluster.
    """
    from app.db.models import Workload

    assert "served_model_name" not in Workload.__table__.columns


def test_the_wire_carries_the_workloads_own_frontend_image() -> None:
    """A model that needs its own frontend build says so on the workload, and the
    frame is where the operator reads it: the plane declares the override and the
    cluster renders it, the same way it does for the engine image."""
    pinned = "ghcr.io/hangar-dev/vllm-runtime:1.4.0-patched.1-hgr2"
    assert _wire_serving(frontend_image=pinned).frontend_image == pinned


def test_a_workload_with_no_frontend_image_sends_nothing_not_the_runtime_image() -> None:
    """Empty is a real answer and it means the cluster decides.

    Kimi K3 is why the field is not one field: the tokenizer fix is compiled into
    the frontend binary and the worker correctly stays on the base image, so a
    plane that filled an absent frontend image in with the engine's would be
    stating the exact thing that broke — and on any cluster whose frontends are
    not published from the engine's repository it would be an ImagePullBackOff.
    """
    engine = "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.0-patched.1"
    wire = _wire_serving(engine_runtime_image=engine)
    assert wire.engine_runtime_image == engine
    assert wire.frontend_image is None


def test_the_engine_args_hatch_is_left_off_the_wire() -> None:
    """The operator lays engine_args over the derived flags itself.

    Resolving it here as well would put the same value on the frame twice, and two
    copies of one fact are two things that can disagree.
    """
    args = {"served-model-name": "acme/kimi-hotfix"}
    assert _wire_served_model(engine_args=args) == "moonshotai/kimi-k3"
    assert _wire_serving(engine_args=args).engine_args["served-model-name"] == "acme/kimi-hotfix"


def test_the_serving_block_carries_the_digest_that_names_the_configuration() -> None:
    """The frame that replaced `recipe.desired` still names what it is, by content.

    The cluster does not act on it. It is carried so that a replica reporting
    what it runs, a benchmark run recording what it measured, and this frame all
    spell the configuration the same way — which the recipe id could not, being a
    handle on a row somebody could edit underneath all three.
    """
    assert _wire_serving().digest == "svg_" + "0" * 64


def test_the_plane_multiplies_groups_by_weights_and_the_operator_never_does():
    """`role_replicas` is absolute, and this is the test that says the arithmetic happens here.

    `Replicas()` in the operator states its own contract — it "renders exactly the number it is
    sent, and must never sum, scale, or re-derive it against anything else". That invariant is
    absolute today and cheap to check, and an operator multiplying groups by weights would be its
    first exception. So the plane multiplies, and the wire carries the product.
    """
    from types import SimpleNamespace

    from app.ingestion.desired import _role_replicas

    roles = [
        SimpleNamespace(role="prefill", weight=1),
        SimpleNamespace(role="decode", weight=2),
    ]
    # Three groups of a 1:2 ratio is three prefill workers and six decode workers.
    assert _role_replicas(SimpleNamespace(min_replicas=3), roles) == {"prefill": 3, "decode": 6}
    # One group is the ratio itself.
    assert _role_replicas(SimpleNamespace(min_replicas=1), roles) == {"prefill": 1, "decode": 2}
    # Parked at zero is zero of each, not one of each.
    assert _role_replicas(SimpleNamespace(min_replicas=0), roles) == {"prefill": 0, "decode": 0}
    # An aggregated deployment sends nothing, and empty is what says so: min_replicas is the
    # whole answer and a map with a single entry would make it a degenerate ratio.
    assert _role_replicas(SimpleNamespace(min_replicas=4), []) == {}
