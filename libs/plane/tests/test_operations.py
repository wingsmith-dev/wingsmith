import inspect
import datetime as dt

import pytest

from app.core.ids import NODE
from app.core.ids import POOL
from app.core.ids import DEVICE
from app.core.ids import CLUSTER
from app.core.ids import ARTIFACT
from app.core.ids import WORKLOAD
from app.core.ids import COMPONENT
from app.core.ids import OPERATION
from app.core.ids import DEPLOYMENT
from app.core.ids import new_uid
from app.core.exceptions import Invalid
from app.modules.operations.routes import OperationRoutes
from app.modules.operations.schemas import OperationSchema
from app.modules.operations.schemas import OperationCreateRequest
from app.modules.operations.services import TYPES
from app.modules.operations.services import PHASES
from app.modules.operations.services import TERMINAL_PHASES
from app.modules.operations.services import wire_kind
from app.modules.operations.repository import TARGET_KINDS
from app.modules.operations.repository import OperationRepository
from app.modules.operations.repository import target_kind
from app.modules.operations.repository import parse_target
from app.modules.operations.repository import target_model


NODE_UID = new_uid(NODE)
CLUSTER_UID = new_uid(CLUSTER)


@pytest.fixture
def repo():
    return OperationRepository(db=None)


def sql(query) -> str:
    return str(query)


def prefix_matches(type_name: str, target: str) -> bool:
    """The creation-time check, isolated from the database round trips around it."""
    return target.startswith(TYPES[type_name][0])


# ---------------------------------------------------------------- type/target

EVERY_PREFIX = [CLUSTER, NODE, DEVICE, DEPLOYMENT, WORKLOAD, COMPONENT, ARTIFACT]

MATRIX = [
    ("node.drain", NODE, True),
    ("node.drain", DEVICE, False),
    ("node.drain", CLUSTER, False),
    ("node.drain", ARTIFACT, False),
    ("node.cordon", NODE, True),
    ("node.cordon", DEPLOYMENT, False),
    ("node.uncordon", NODE, True),
    ("node.uncordon", COMPONENT, False),
    ("node.reboot", NODE, True),
    ("node.reboot", DEVICE, False),
    ("device.reset", DEVICE, True),
    ("device.reset", NODE, False),
    ("device.reset", CLUSTER, False),
    ("cache.warm", ARTIFACT, True),
    ("cache.warm", DEPLOYMENT, False),
    ("cache.warm", NODE, False),
    ("deployment.roll", DEPLOYMENT, True),
    ("deployment.benchmark", DEPLOYMENT, True),
    ("deployment.benchmark", NODE, False),
    ("deployment.benchmark", ARTIFACT, False),
    ("deployment.roll", COMPONENT, False),
    ("deployment.roll", NODE, False),
    ("workload.scale", WORKLOAD, True),
    ("workload.scale", DEPLOYMENT, False),
    ("workload.scale", CLUSTER, False),
    ("workload.pause", WORKLOAD, True),
    ("workload.reshape", WORKLOAD, True),
    ("workload.reshape", DEPLOYMENT, False),
    ("workload.pause", DEPLOYMENT, False),
    ("workload.drain", WORKLOAD, True),
    ("workload.drain", NODE, False),
    ("component.upgrade", COMPONENT, True),
    ("component.upgrade", DEPLOYMENT, False),
    ("component.upgrade", CLUSTER, False),
]


@pytest.mark.parametrize(("type_name", "prefix", "accepted"), MATRIX)
def test_target_prefix_is_validated_against_the_type(type_name, prefix, accepted):
    assert prefix_matches(type_name, new_uid(prefix)) is accepted


@pytest.mark.parametrize("type_name", sorted(TYPES))
def test_every_type_accepts_exactly_one_target_prefix(type_name):
    accepted = [p for p in EVERY_PREFIX if prefix_matches(type_name, new_uid(p))]
    assert len(accepted) == 1
    assert accepted[0] == TYPES[type_name][0]


@pytest.mark.parametrize("type_name", sorted(TYPES))
def test_the_matrix_covers_every_type(type_name):
    assert any(row[0] == type_name for row in MATRIX)


@pytest.mark.parametrize("type_name", sorted(TYPES))
def test_every_type_names_a_prefix_operations_can_act_upon(type_name):
    assert TYPES[type_name][0] in TARGET_KINDS


def test_uncordon_shares_the_cordon_wire_kind():
    assert wire_kind("node.uncordon") == wire_kind("node.cordon") == "cordon"


def test_wire_kinds_are_named_for_the_mechanism_not_the_type():
    assert wire_kind("device.reset") == "device_reset"
    assert wire_kind("cache.warm") == "cache_warm"


# ------------------------------------------------------------------- targets


@pytest.mark.parametrize("prefix", EVERY_PREFIX)
def test_target_model_resolves_every_actionable_prefix(prefix):
    assert target_model(new_uid(prefix)) is not None
    assert target_kind(new_uid(prefix)) == TARGET_KINDS[prefix]


def test_a_bare_name_is_not_a_target():
    """Refused, never guessed at: a bare name says nothing about its own kind."""
    with pytest.raises(Invalid):
        target_model("iad-1")


@pytest.mark.parametrize("prefix", EVERY_PREFIX)
def test_a_qualified_name_names_the_same_kind_the_prefix_does(prefix):
    ref = f"{TARGET_KINDS[prefix]}:gpu-node-04"
    assert target_kind(ref) == TARGET_KINDS[prefix]
    assert target_model(ref) is target_model(new_uid(prefix))


def test_a_qualified_name_matches_the_name_column_not_the_uid(repo):
    text = sql(repo.target_query("node:gpu-node-04"))
    assert "supply_node.name = " in text
    assert "supply_node.uid = " not in text


def test_a_device_is_named_by_the_uuid_stamped_on_the_card(repo):
    """A device row carries no name; `device_uuid` is what an operator reads off it."""
    assert "supply_device.device_uuid = " in sql(repo.target_query("device:GPU-8f3c"))


def test_a_kind_operations_do_not_act_upon_cannot_be_qualified():
    with pytest.raises(Invalid):
        target_model("pool:hopper")


def test_a_qualified_reference_with_no_name_is_refused():
    with pytest.raises(Invalid):
        target_model("node:")


def test_the_uid_form_still_names_itself(repo):
    uid = new_uid(NODE)
    assert parse_target(uid).value == uid
    assert "supply_node.uid = " in sql(repo.target_query(uid))


def test_a_prefix_operations_do_not_act_upon_is_rejected():
    with pytest.raises(Invalid):
        target_model(new_uid(POOL))


def test_artifact_targets_carry_no_cluster_of_their_own(repo):
    text = sql(repo.target_query(new_uid(ARTIFACT)))
    assert "AS cluster_id" in text
    assert "NULL" in text


def test_a_cluster_target_is_its_own_cluster(repo):
    text = sql(repo.target_query(new_uid(CLUSTER)))
    assert "supply_cluster.id AS cluster_id" in text


def test_node_target_resolves_through_its_own_cluster_column(repo):
    text = sql(repo.target_query(new_uid(NODE)))
    assert "supply_node.cluster_id" in text


# ------------------------------------------------------------------ querying


def test_list_query_filters_compile_into_one_statement(repo):
    import datetime as dt

    text = sql(
        repo.list_query(
            limit=10,
            before_seq=99,
            cluster="iad-1",
            kind="node.drain",
            phase="running",
            target=new_uid(NODE),
            actor="operator@example.com",
            since=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        )
    )
    assert "FROM ops_operation LEFT OUTER JOIN supply_cluster" in text
    assert "ops_operation.seq < " in text
    assert "supply_cluster.name = " in text
    assert "ops_operation.type = " in text
    assert "ops_operation.phase = " in text
    assert "ops_operation.target_type = " in text
    assert "ops_operation.target_id IN (SELECT supply_node.id" in text
    assert "ops_operation.requested_by = " in text
    assert "ops_operation.created_at >= " in text


def test_list_is_newest_first(repo):
    assert "ORDER BY ops_operation.seq DESC" in sql(repo.list_query(limit=10))


def test_target_filter_is_scoped_to_the_kind_not_just_the_row_id(repo):
    text = sql(repo.list_query(limit=10, target=new_uid(DEVICE)))
    assert "ops_operation.target_type = " in text


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=10).compile().params["param_1"] == 11


def test_operations_are_addressed_by_id_only(repo):
    text = sql(repo.get_query(new_uid(OPERATION)))
    assert "ops_operation.uid = " in text
    assert "name" not in text


def test_cluster_ref_resolves_an_id_against_uid_not_name(repo):
    assert "supply_cluster.uid = " in sql(repo.list_query(limit=10, cluster=new_uid(CLUSTER)))


def test_idempotency_key_is_looked_up_in_its_own_column(repo):
    """A column, not `payload ->> 'idempotency_key'`: only a column can be indexed."""
    text = sql(repo.idempotency_query("retry-1"))
    assert "ops_operation.idempotency_key = " in text
    assert "payload ->>" not in text


# -------------------------------------------------------------------- shapes


def test_create_request_is_a_type_and_a_target():
    data = OperationCreateRequest(type="node.drain", target=new_uid(NODE))
    assert data.reason is None
    assert data.parameters == {}
    assert data.cluster is None


def test_api_written_and_ingestion_written_fields_are_disjoint():
    api_written = {"type", "target", "reason", "requested_by", "parameters", "deadline_at"}
    ingestion_written = {"phase", "result", "error", "attempts", "started_at", "finished_at"}
    fields = set(OperationSchema.__struct_fields__)
    assert api_written <= fields
    assert ingestion_written <= fields
    assert not (api_written & ingestion_written)


def test_the_target_kind_is_carried_explicitly_rather_than_parsed_off_the_id():
    from app.modules.operations.schemas import OperationTargetSchema

    assert set(OperationTargetSchema.__struct_fields__) == {"type", "id"}


def test_phases_cover_the_open_and_terminal_sets():
    assert {"pending", "running"} <= PHASES
    assert TERMINAL_PHASES <= PHASES
    assert "cancelled" in TERMINAL_PHASES


# -------------------------------------------------------------------- routes


def test_there_is_no_verb_endpoint_only_a_collection():
    assert OperationRoutes.path == "/operations"
    handlers = {h.handler_name for h in OperationRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {
        "list_operations",
        "create_operation",
        "get_operation",
        "cancel_operation",
        "operations_summary",
    }


def test_the_type_filter_is_exposed_as_type_not_kind():
    parameter = inspect.signature(OperationRoutes.list_operations.fn).parameters["kind"]
    assert parameter.default.query == "type"


def test_create_accepts_an_idempotency_key_header():
    parameter = inspect.signature(OperationRoutes.create_operation.fn).parameters["idempotency_key"]
    assert parameter.default.header == "Idempotency-Key"


def test_cancellation_returns_the_operation_rather_than_deleting_it():
    doc = OperationRoutes.cancel_operation.fn.__doc__ or ""
    assert "not deletion" in doc


def test_the_plane_and_ingestion_write_disjoint_cancel_fields():
    """The plane stamps `cancel_requested_at`; only ingestion moves `phase`.

    Except while the operation is still `pending`, where nothing has reported on
    it and there is no writer to race.
    """
    from app.modules.operations.services import OperationService

    source = inspect.getsource(OperationService.cancel)
    assert "cancel_requested_at" in source
    assert source.count('operation.phase = "cancelled"') == 1
    assert "if operation.phase == PENDING:" in source


def test_a_qualified_target_filter_compiles_to_the_name_column(repo):
    text = sql(repo.list_query(limit=10, target="node:gpu-node-04"))
    assert "ops_operation.target_type = " in text
    assert "ops_operation.target_id IN (SELECT supply_node.id" in text
    assert "supply_node.name = " in text


# ------------------------------------------------------------------- summary


def test_summary_groups_the_collection_by_phase(repo):
    text = sql(repo.phase_counts_query())
    assert "GROUP BY ops_operation.phase" in text
    assert "count(" in text


def test_summary_can_be_narrowed_to_one_cluster(repo):
    text = sql(repo.phase_counts_query(cluster=new_uid(CLUSTER)))
    assert "supply_cluster.uid = " in text


def test_finished_window_is_closed_by_the_database_clock_not_the_plane(repo):
    """The 24h boundary is `now()`, computed server-side, never the plane's clock."""
    text = sql(repo.finished_since_query("failed"))
    assert "now()" in text
    assert "ops_operation.phase = " in text
    assert "ops_operation.finished_at IS NOT NULL" in text


@pytest.mark.parametrize("route", ["operations_summary"])
def test_summary_is_exposed_as_a_read_route(route):
    handlers = {h.handler_name for h in OperationRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert route in handlers


def test_summary_schema_covers_phase_mix_and_both_24h_terminals():
    from app.modules.operations.schemas import OperationSummarySchema

    fields = set(OperationSummarySchema.__struct_fields__)
    assert fields == {"total", "by_phase", "failed_24h", "succeeded_24h"}


class _StubSession:
    """Answers `_target_uid`'s single scalar and nothing else."""

    def __init__(self, target_uid: str) -> None:
        self._target_uid = target_uid

    async def scalar(self, _query):
        return self._target_uid


def _operation(**overrides):
    from app.db.models import Operation

    operation = Operation(
        uid=new_uid(OPERATION),
        type="node.drain",
        target_type="node",
        target_id=1,
        reason="driver upgrade",
        requested_by="operator@example.com",
        phase="running",
        payload={"parameters": {"grace_period_seconds": "1800"}},
    )
    operation.created_at = dt.datetime(2026, 8, 19, 14, 2, tzinfo=dt.UTC)
    operation.deadline_at = None
    operation.cancel_requested_at = None
    for key, value in overrides.items():
        setattr(operation, key, value)
    return operation


async def test_an_operation_nobody_withdrew_renders_as_the_instruction() -> None:
    from app.ingestion.operations import DESIRED
    from app.ingestion.operations import _render

    rendered = await _render(_StubSession(NODE_UID), _operation(), CLUSTER_UID)

    assert rendered is not None
    type_name, payload = rendered
    assert type_name == DESIRED
    assert payload.operation_id
    assert payload.kind == wire_kind("node.drain")


async def test_a_withdrawn_operation_renders_as_a_cancellation_and_never_again_as_the_instruction() -> None:
    """The property that keeps a reconnect from re-sending a drain the caller stopped.

    `open_operations` replays every operation that has not reached a terminal
    phase, and a cancelled-but-still-running one is in that set by definition —
    the cluster has not answered yet. Rendering the instruction there would ask
    for the work again on exactly the path that exists to recover it.
    """
    from app.ingestion.operations import CANCEL
    from app.ingestion.operations import _render

    requested = dt.datetime(2026, 8, 19, 14, 31, 44, tzinfo=dt.UTC)
    rendered = await _render(_StubSession(NODE_UID), _operation(cancel_requested_at=requested), CLUSTER_UID)

    assert rendered is not None
    type_name, payload = rendered
    assert type_name == CANCEL
    assert payload.cluster_id == CLUSTER_UID
    assert payload.requested_at.startswith("2026-08-19T14:31:44")
    assert not hasattr(payload, "kind")


async def test_a_cancellation_carries_who_asked_and_why() -> None:
    """Asserted on the cancellation type itself, not on the fields alone.

    `OperationDesired` carries a `requested_by` and a `reason` too, so a test
    that checked only those two would pass just as well against the frame this
    one exists to prove is no longer sent.
    """
    from app.hub.messages import OperationCancel
    from app.ingestion.operations import CANCEL
    from app.ingestion.operations import _render

    rendered = await _render(
        _StubSession(NODE_UID),
        _operation(cancel_requested_at=dt.datetime(2026, 8, 19, 14, 31, tzinfo=dt.UTC)),
        CLUSTER_UID,
    )

    assert rendered is not None
    type_name, payload = rendered
    assert type_name == CANCEL
    assert isinstance(payload, OperationCancel)
    assert payload.requested_by == "operator@example.com"
    assert payload.reason == "driver upgrade"
