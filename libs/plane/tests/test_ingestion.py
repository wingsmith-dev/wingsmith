"""Ingestion, exercised without a database.

There is no Postgres in CI, so the statements the sink builds are compiled and
asserted on instead: which constraint an upsert targets, which columns its
update set is willing to touch, and which payload fields never reach a
parameter at all. The prune guard is unit-tested directly, because it is the
one decision in this package that can delete a cluster's whole inventory.
"""

import json
import uuid

from typing import Any
from pathlib import Path

import pytest

from msgspec import json as msgjson
from sqlalchemy.dialects import postgresql

from app.hub.sink import Sink
from app.hub.sink import SinkFrame
from app.hub.sink import DesiredSource
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


def test_observed_uid_adopts_a_well_formed_cluster_side_id() -> None:
    wire = "nod_01J9X2BBBBBBBBBBBBBBBBBBBB"
    assert statements.observed_uid(wire, "nod_") == wire


def test_observed_uid_mints_when_the_wire_id_is_unusable() -> None:
    minted = statements.observed_uid("node-7", "nod_")
    assert minted.startswith("nod_")
    assert minted != statements.observed_uid("", "nod_")


def test_observed_uid_refuses_a_wrongly_prefixed_id() -> None:
    minted = statements.observed_uid("dev_01J9X2CCCCCCCCCCCCCCCCCCCC", "nod_")
    assert minted.startswith("nod_")


# --------------------------------------------------------------------------- telemetry


TELEMETRY_VALUES = {
    74210,  # device.observed memory_used_mb
    12,  # device.observed ecc.volatile_corrected
}


def test_device_upsert_keeps_state_and_drops_measurements() -> None:
    device = payload("device.observed")
    _, params = compile_sql(statements.upsert_device(CLUSTER_ID, device))
    values = scalar_params(params)
    assert device.memory_total_mb in values
    assert device.memory_used_mb not in values
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


def test_the_database_sink_satisfies_the_protocols() -> None:
    assert isinstance(DatabaseSink(), Sink)
    assert isinstance(DatabaseDesiredSource(), DesiredSource)


async def test_applying_one_frame_commits_once() -> None:
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.apply_observed(observed_frame("node.observed"))
    assert sessions.commits == 1


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


async def test_a_whole_snapshot_issues_three_scoped_deletes() -> None:
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.begin_resync(CLUSTER, "supply", "req-3")
    await sink.apply_observed(observed_frame("node.observed", reply_to="req-3"))
    await sink.end_resync(CLUSTER, "req-3", 1, True)
    deletes = [s for s in sessions.executed if str(s).upper().startswith("DELETE")]
    assert len(deletes) == 3


async def test_a_truncated_snapshot_deletes_nothing() -> None:
    sessions = FakeSessions()
    sink = DatabaseSink(sessions)
    await sink.begin_resync(CLUSTER, "supply", "req-4")
    await sink.apply_observed(observed_frame("node.observed", reply_to="req-4"))
    await sink.end_resync(CLUSTER, "req-4", 9, True)
    assert not [s for s in sessions.executed if str(s).upper().startswith("DELETE")]


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
