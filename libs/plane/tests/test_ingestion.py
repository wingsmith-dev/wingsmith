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

from typing import Any
from pathlib import Path

import pytest

from msgspec import json as msgjson
from msgspec import structs
from sqlalchemy import Delete
from sqlalchemy.dialects import postgresql

from app.hub.sink import Sink
from app.hub.sink import SinkFrame
from app.hub.sink import DesiredSource
from app.hub.sink import OperationSource
from app.ingestion import statements
from app.hub.messages import MESSAGE_TYPES
from app.ingestion.sink import DatabaseSink
from app.ingestion.sink import UnknownCluster
from app.ingestion.sink import DatabaseDesiredSource
from app.ingestion.time import parse_ts
from app.ingestion.resync import NODE
from app.ingestion.resync import DEVICE
from app.ingestion.resync import ResyncEpoch
from app.ingestion.resync import decide
from app.ingestion.desired import format_ts


FIXTURES = Path(__file__).resolve().parents[3] / "contract" / "fixtures"

CLUSTER = "clu_01J9X2AAAAAAAAAAAAAAAAAAAA"
CLUSTER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


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


def test_replica_upsert_targets_the_natural_key() -> None:
    sql, _ = compile_sql(statements.upsert_replica(CLUSTER_ID, DEPLOYMENT_ID, payload("replica.observed")))
    assert "ON CONFLICT ON CONSTRAINT uq_demand_replica_cluster_name DO UPDATE" in sql


def test_replica_stores_the_engine_version_and_not_the_engine() -> None:
    """The engine is the recipe's; the version is what lags during a rollout."""
    observed = payload("replica.observed")
    _, params = compile_sql(statements.upsert_replica(CLUSTER_ID, DEPLOYMENT_ID, observed))
    values = scalar_params(params)
    assert observed.engine_version in values
    assert observed.engine not in values


def test_replica_does_not_write_the_devices_it_holds() -> None:
    """A replica's commitment is an allocation, not a column on the replica."""
    sql, _ = compile_sql(statements.upsert_replica(CLUSTER_ID, DEPLOYMENT_ID, payload("replica.observed")))
    assert "device" not in sql


def test_allocation_uses_the_cluster_minted_id() -> None:
    """An observed row is named by the cluster that discovered it (ADR 0002)."""
    observed = payload("allocation.observed")
    sql, params = compile_sql(statements.upsert_allocation(CLUSTER_ID, WORKLOAD_ID, PROJECT_ID, REPLICA_ID, observed))
    assert observed.allocation_id in scalar_params(params)
    assert "uid = excluded.uid" not in sql


def test_allocation_reads_its_device_class_back_from_the_devices() -> None:
    """Having the cluster restate it would create a second place for it to be wrong."""
    observed = payload("allocation.observed")
    sql, params = compile_sql(statements.upsert_allocation(CLUSTER_ID, WORKLOAD_ID, PROJECT_ID, REPLICA_ID, observed))
    assert "SELECT supply_device.device_class" in sql
    assert len(observed.device_ids) in scalar_params(params)


def test_dropping_a_device_releases_it_rather_than_deleting_the_row() -> None:
    """The partial unique index frees the accelerator; the history stays."""
    sql, _ = compile_sql(
        statements.release_allocation_devices(ALLOCATION_ID, CLUSTER_ID, payload("allocation.observed"))
    )
    assert "UPDATE bind_allocation_device SET" in sql and "released_at=" in sql
    assert "DELETE" not in sql
    assert "bind_allocation_device.device_id NOT IN" in sql


def test_attaching_devices_leaves_an_existing_membership_alone() -> None:
    """Membership is fixed when the allocation is granted; re-inserting would move released_at."""
    sql, _ = compile_sql(
        statements.attach_allocation_devices(ALLOCATION_ID, CLUSTER_ID, payload("allocation.observed"))
    )
    assert "ON CONFLICT ON CONSTRAINT uq_bind_allocation_device DO NOTHING" in sql


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
    for column in ("cpu_cores", "memory_mib", "os_image", "taints", "joined_at", "runtime_version"):
        assert column not in sql


# --------------------------------------------------------------------------- events


def test_event_insert_is_guarded_against_replay() -> None:
    sql, _ = compile_sql(statements.insert_node_event(CLUSTER_ID, payload("node.event")))
    assert "INSERT INTO ops_event" in sql
    assert "NOT (EXISTS" in sql


def test_event_insert_mints_a_public_id() -> None:
    sql, params = compile_sql(statements.insert_node_event(CLUSTER_ID, payload("node.event")))
    assert "uid" in sql.split("SELECT")[0]
    assert any(isinstance(v, str) and v.startswith("evt_") for v in params.values())


def test_device_event_resolves_its_subject() -> None:
    sql, _ = compile_sql(statements.insert_device_event(CLUSTER_ID, payload("device.event")))
    assert "SELECT supply_device.id" in sql


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
    def __init__(self, scalar: Any = None) -> None:
        self._scalar = scalar
        self.rowcount = 0

    def scalar_one_or_none(self) -> Any:
        return self._scalar


class FakeSession:
    """Records statements and answers the one lookup the sink makes."""

    def __init__(self, owner: "FakeSessions") -> None:
        self.owner = owner

    async def execute(self, statement) -> FakeResult:
        self.owner.executed.append(statement)
        if self.owner.fail:
            raise RuntimeError("write failed")
        return FakeResult(self.owner.cluster_id)


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
    def __init__(self, cluster_id: Any = CLUSTER_ID, fail: bool = False) -> None:
        self.executed: list[Any] = []
        self.commits = 0
        self.cluster_id = cluster_id
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


async def test_a_demand_resync_warns_that_nothing_on_that_side_was_pruned(caplog) -> None:
    """Demand-side pruning does not exist yet, and silence would read as success.

    A `demand` snapshot closes cleanly and deletes nothing, so a replica the
    cluster stopped reporting stays in the database forever. Until there is a
    real prune, the log is the only thing that says so.
    """
    sink = DatabaseSink(FakeSessions())
    await sink.begin_resync(CLUSTER, "demand", "req-6")
    with caplog.at_level(logging.WARNING):
        await sink.end_resync(CLUSTER, "req-6", 0, True)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "ingestion.resync.demand_not_pruned" in warnings[0].getMessage()
    assert "NOT pruned" in warnings[0].getMessage()


async def test_a_supply_only_resync_says_nothing_about_demand(caplog) -> None:
    sink = DatabaseSink(FakeSessions())
    await sink.begin_resync(CLUSTER, "supply", "req-7")
    with caplog.at_level(logging.WARNING):
        await sink.end_resync(CLUSTER, "req-7", 0, True)

    assert not [r for r in caplog.records if "demand_not_pruned" in r.getMessage()]


async def test_a_full_resync_prunes_the_supply_half_and_warns_about_the_other(caplog) -> None:
    """`all` carries both scopes, so the prune and the warning both belong to it."""
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.begin_resync(CLUSTER, "all", "req-8")
    await sink.apply_observed(observed_frame("node.observed", reply_to="req-8"))
    with caplog.at_level(logging.WARNING):
        await sink.end_resync(CLUSTER, "req-8", 1, True)

    assert [s for s in sessions.executed if str(s).upper().startswith("DELETE")]
    assert any("demand_not_pruned" in r.getMessage() for r in caplog.records)


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
