import uuid
import inspect

import pytest

from app.core.ids import PREFIXES
from app.core.ids import PRIORITY_CLASS
from app.core.ids import new_uid
from app.modules.priority_classes.routes import PriorityClassRoutes
from app.modules.priority_classes.schemas import PriorityClassSchema
from app.modules.priority_classes.schemas import PriorityClassCreateRequest
from app.modules.priority_classes.schemas import PriorityClassUpdateRequest
from app.modules.priority_classes.services import precedence
from app.modules.priority_classes.repository import PriorityClassRepository
from app.modules.priority_classes.repository import ref_column


class FakeClass:
    def __init__(self, name: str, value: int) -> None:
        self.name = name
        self.value = value


@pytest.fixture
def repo():
    return PriorityClassRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_route_path_is_hyphenated_even_though_the_package_is_underscored():
    assert PriorityClassRoutes.path == "/priority-classes"
    assert PriorityClassRoutes.__module__ == "app.modules.priority_classes.routes"


def test_no_id_prefix_is_reserved_for_priority_classes():
    """`pol_` in the contract's prefix list is spent on pools, and there is no other."""
    assert "pol_" in PREFIXES
    assert not any(p.startswith(("pri", "pcl")) for p in PREFIXES)


def test_reference_is_a_public_id_or_a_name():
    assert ref_column("production").name == "name"
    assert ref_column(new_uid(PRIORITY_CLASS)).name == "uid"
    assert ref_column(str(uuid.uuid4())).name == "name"


def test_get_query_resolves_a_name(repo):
    assert "bind_priority_class.name = " in sql(repo.get_query("production"))


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(repo.list_query(limit=10, after_seq=3, preemptible=False, preempts=True, search="batch"))
    assert "bind_priority_class.seq > " in text
    assert "bind_priority_class.preemptible IS false" in text
    assert "bind_priority_class.preempts IS true" in text
    assert "lower(bind_priority_class.name) LIKE lower(" in text
    assert "ORDER BY bind_priority_class.seq ASC" in text


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=10).compile().params["param_1"] == 11


def test_value_holder_query_finds_the_class_already_at_a_value(repo):
    text = sql(repo.value_holder_query(100))
    assert "bind_priority_class.value = " in text


def test_value_holder_query_excludes_the_class_being_updated(repo):
    text = sql(repo.value_holder_query(100, exclude=uuid.uuid4()))
    assert "bind_priority_class.id != " in text


def test_workload_count_query_counts_rather_than_loads(repo):
    text = sql(repo.workload_count_query(uuid.uuid4()))
    assert text.startswith("SELECT count(demand_workload.id)")
    assert "demand_workload.priority_class_id = " in text


def test_preemptible_and_preempts_are_two_switches_not_one_axis():
    fields = PriorityClassSchema.__struct_fields__
    assert "preemptible" in fields
    assert "preempts" in fields


def test_name_cannot_be_changed_by_patch_because_it_is_the_reference():
    assert "name" not in PriorityClassUpdateRequest.__struct_fields__


def test_update_request_leaves_absent_fields_unset():
    assert PriorityClassUpdateRequest(value=90).to_dict(exclude_unset=True) == {"value": 90}


def test_update_request_can_clear_the_description_with_null():
    assert PriorityClassUpdateRequest(description=None).to_dict(exclude_unset=True) == {"description": None}


def test_create_request_defaults_to_preemptible_and_non_preempting():
    data = PriorityClassCreateRequest(name="batch", value=10)
    assert data.preemptible is True
    assert data.preempts is False
    assert data.is_default is False


def test_precedence_orders_by_value_descending():
    ordered = precedence([FakeClass("batch", 10), FakeClass("production", 100)])
    assert [c.name for c in ordered] == ["production", "batch"]


def test_a_tie_at_one_value_is_only_sorted_deterministically_not_resolved():
    """The model allows a tie; the API refuses to create one. See the report."""
    ordered = precedence([FakeClass("b", 50), FakeClass("a", 50)])
    assert [c.name for c in ordered] == ["a", "b"]


def test_controller_exposes_full_crud():
    handlers = {h.handler_name for h in PriorityClassRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {
        "list_priority_classes",
        "create_priority_class",
        "get_priority_class",
        "update_priority_class",
        "delete_priority_class",
    }


def test_update_accepts_if_match_against_the_revision():
    parameter = inspect.signature(PriorityClassRoutes.update_priority_class.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"


def test_no_label_filter_is_offered_because_priority_classes_carry_none():
    assert "label" not in inspect.signature(PriorityClassRoutes.list_priority_classes.fn).parameters
