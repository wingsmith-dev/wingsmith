import pytest

from app.core.ids import CLUSTER
from app.core.ids import COMPONENT
from app.core.ids import new_uid
from app.modules.components.routes import ComponentRoutes
from app.modules.components.schemas import ComponentSchema
from app.modules.components.schemas import ComponentStatusSchema
from app.modules.components.schemas import ComponentCreateRequest
from app.modules.components.schemas import ComponentUpdateRequest
from app.modules.components.services import UNREPORTED
from app.modules.components.services import is_behind
from app.modules.components.repository import ComponentRepository


@pytest.fixture
def repo():
    return ComponentRepository(db=None)


def sql(query) -> str:
    return str(query)


# -------------------------------------------------------------------- shapes


def test_create_request_defaults_to_an_enabled_component():
    data = ComponentCreateRequest(cluster="iad-1", name="agent", kind="agent", desired_version="1.4.0")
    assert data.enabled is True
    assert data.config == {}


def test_update_request_leaves_absent_fields_unset():
    data = ComponentUpdateRequest(desired_version="1.5.0")
    assert data.to_dict(exclude_unset=True) == {"desired_version": "1.5.0"}


def test_the_natural_key_cannot_be_patched():
    fields = ComponentUpdateRequest.__struct_fields__
    assert "cluster" not in fields
    assert "name" not in fields


def test_declared_and_observed_are_a_pair_not_one_nullable_column():
    fields = set(ComponentSchema.__struct_fields__)
    assert "desired_version" in fields
    assert "status" in fields
    assert "observed_version" not in fields
    assert "observed_version" in ComponentStatusSchema.__struct_fields__


def test_the_observed_half_is_never_absent_from_a_response():
    assert ComponentSchema.__annotations__["status"] is ComponentStatusSchema


def test_no_write_request_can_set_the_observed_half():
    observed = set(ComponentStatusSchema.__struct_fields__)
    assert not (observed & set(ComponentCreateRequest.__struct_fields__))
    assert not (observed & set(ComponentUpdateRequest.__struct_fields__))


# -------------------------------------------------------------------- behind


@pytest.mark.parametrize(
    ("desired", "observed", "expected"),
    [
        ("1.4.0", "1.4.0", False),
        ("1.4.0", "1.3.9", True),
        ("1.4.0", None, True),
        ("1.4.0", "1.4.1", True),
        ("1.4.0", "", True),
    ],
)
def test_behind_compares_declared_against_observed(desired, observed, expected):
    assert is_behind(desired, observed) is expected


def test_an_unreported_component_counts_as_behind():
    assert UNREPORTED.observed_version is None
    assert UNREPORTED.rollout_phase == "unknown"
    assert UNREPORTED.healthy is False
    assert is_behind("1.4.0", UNREPORTED.observed_version) is True


# ------------------------------------------------------------------ querying


def test_the_pair_is_read_in_one_join_not_a_second_query(repo):
    text = sql(repo.list_query(limit=10))
    assert "FROM ops_component JOIN supply_cluster" in text
    assert "LEFT OUTER JOIN ops_component_status" in text


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(
            limit=10,
            after_seq=7,
            cluster="iad-1",
            name="serving-runtime",
            kind="runtime",
            enabled=True,
            search="vllm",
        )
    )
    assert "ops_component.seq > " in text
    assert "supply_cluster.name = " in text
    assert "ops_component.name = " in text
    assert "ops_component.kind = " in text
    assert "ops_component.enabled IS true" in text
    assert "lower(ops_component.name) LIKE lower(" in text
    assert "ORDER BY ops_component.seq ASC" in text


def test_which_clusters_are_behind_on_this_component(repo):
    text = sql(repo.list_query(limit=200, name="agent", behind=True))
    assert "ops_component.name = " in text
    assert "ops_component_status.observed_version IS NULL" in text
    assert "ops_component_status.observed_version != ops_component.desired_version" in text


def test_up_to_date_is_the_negation_of_the_same_predicate(repo):
    text = sql(repo.list_query(limit=10, behind=False))
    assert "NOT (" in text
    assert "ops_component_status.observed_version IS NULL" in text


def test_behind_is_not_filtered_when_it_is_not_asked_for(repo):
    assert "observed_version IS NULL" not in sql(repo.list_query(limit=10))


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=10).compile().params["param_1"] == 11


def test_get_query_resolves_a_name_or_an_id(repo):
    assert "ops_component.name = " in sql(repo.get_query("agent"))
    assert "ops_component.uid = " in sql(repo.get_query(new_uid(COMPONENT)))


def test_a_name_is_scoped_by_cluster_because_it_is_unique_per_cluster(repo):
    text = sql(repo.get_query("agent", cluster="iad-1"))
    assert "supply_cluster.name = " in text


def test_cluster_ref_resolves_an_id_against_uid_not_name(repo):
    assert "supply_cluster.uid = " in sql(repo.list_query(limit=10, cluster=new_uid(CLUSTER)))


# -------------------------------------------------------------------- routes


def test_controller_exposes_full_crud_over_the_declared_half():
    assert ComponentRoutes.path == "/components"
    handlers = {h.handler_name for h in ComponentRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {
        "list_components",
        "create_component",
        "get_component",
        "update_component",
        "delete_component",
    }


def test_there_is_no_endpoint_that_writes_the_observed_half():
    from app.modules.components.services import ComponentService

    assert not any(name.startswith("set_status") or "observed" in name for name in vars(ComponentService))


class RecordingSession:
    """Records which session calls the repository makes, so committing can be caught by behaviour."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def add(self, row) -> None:
        self.calls.append("add")

    async def delete(self, row) -> None:
        self.calls.append("delete")

    async def flush(self) -> None:
        self.calls.append("flush")

    async def commit(self) -> None:
        self.calls.append("commit")


async def test_the_service_owns_the_commit_and_the_repository_only_flushes():
    session = RecordingSession()
    repository = ComponentRepository(db=session)

    await repository.add(object())
    await repository.delete(object())

    assert session.calls == ["add", "flush", "delete", "flush"]
