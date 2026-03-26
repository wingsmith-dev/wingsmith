import uuid
import inspect

import pytest

from app.core.ids import POLICY
from app.core.ids import CLUSTER
from app.core.ids import PROJECT
from app.core.ids import new_uid
from app.core.exceptions import Invalid
from app.modules.policies.routes import PolicyRoutes
from app.modules.policies.schemas import PolicySchema
from app.modules.policies.schemas import PolicyCreateRequest
from app.modules.policies.schemas import PolicyUpdateRequest
from app.modules.policies.schemas import PolicyAttachmentSchema
from app.modules.policies.schemas import PolicyAttachmentRequest
from app.modules.policies.services import SUBJECT_TYPES
from app.modules.policies.services import check_subject_type
from app.modules.policies.services import check_cluster_lists
from app.modules.policies.repository import SUBJECTS
from app.modules.policies.repository import PolicyRepository
from app.modules.policies.repository import ref_column


@pytest.fixture
def repo():
    return PolicyRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_reference_is_a_public_id_or_a_name():
    assert ref_column("eu-only").name == "name"
    assert ref_column(new_uid(POLICY)).name == "uid"
    assert ref_column(str(uuid.uuid4())).name == "name"


def test_get_query_resolves_a_name(repo):
    assert "bind_policy.name = " in sql(repo.get_query("eu-only"))


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(limit=10, after_seq=2, enabled=True, region="iad", device_class="h100", search="eu")
    )
    assert "bind_policy.seq > " in text
    assert "bind_policy.enabled IS true" in text
    assert "bind_policy.allowed_regions" in text
    assert "bind_policy.allowed_device_classes" in text
    assert "lower(bind_policy.name) LIKE lower(" in text
    assert "ORDER BY bind_policy.seq ASC" in text


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=10).compile().params["param_1"] == 11


def test_attachments_are_fetched_for_a_whole_page_in_one_query(repo):
    text = sql(repo.attachment_query([uuid.uuid4(), uuid.uuid4()]))
    assert "FROM bind_policy_attachment" in text
    assert "bind_policy_attachment.policy_id IN " in text


def test_what_constrains_this_subject_is_a_filter_not_a_nested_route(repo):
    text = sql(repo.policy_ids_for_subject("project", uuid.uuid4()))
    assert text.startswith("SELECT bind_policy_attachment.policy_id")
    assert "bind_policy_attachment.subject_type = " in text
    assert "bind_policy_attachment.subject_id = " in text


def test_subject_types_cover_the_model_docstring_and_nothing_else():
    assert SUBJECT_TYPES == frozenset({"tenant", "project", "cluster", "endpoint"})
    assert set(SUBJECTS) == SUBJECT_TYPES


def test_unknown_subject_type_is_refused_before_it_reaches_sql():
    with pytest.raises(Invalid):
        check_subject_type("workload")


def test_subject_query_resolves_a_project_by_slug_or_by_prefixed_id(repo):
    assert "bind_project.slug = " in sql(repo.subject_query("project", "research"))
    assert "bind_project.uid = " in sql(repo.subject_query("project", new_uid(PROJECT)))


def test_subject_query_resolves_a_tenant_by_slug_since_it_has_no_uid(repo):
    assert "bind_tenant.slug = " in sql(repo.subject_query("tenant", "acme"))


def test_subject_query_resolves_a_cluster_by_name_or_id(repo):
    assert "supply_cluster.name = " in sql(repo.subject_query("cluster", "iad-1"))
    assert "supply_cluster.uid = " in sql(repo.subject_query("cluster", new_uid(CLUSTER)))


def test_subject_query_for_an_unknown_type_is_none_rather_than_a_guess(repo):
    assert repo.subject_query("workload", "anything") is None


def test_a_cluster_cannot_be_both_allowed_and_denied():
    with pytest.raises(Invalid):
        check_cluster_lists(["a", "b"], ["b"])


def test_an_empty_allow_list_is_no_opinion_not_a_contradiction():
    check_cluster_lists([], ["a"])
    check_cluster_lists(["a"], [])


def test_attachments_are_on_the_detail_response():
    assert "attachments" in PolicySchema.__struct_fields__
    assert set(PolicyAttachmentSchema.__struct_fields__) == {
        "subject_type",
        "subject_id",
        "subject",
        "subject_name",
    }


def test_cluster_lists_are_exposed_as_references_not_raw_keys():
    fields = PolicySchema.__struct_fields__
    assert "allowed_clusters" in fields
    assert "denied_clusters" in fields
    assert "allowed_cluster_ids" not in fields


def test_update_request_leaves_absent_fields_unset():
    assert PolicyUpdateRequest(enabled=False).to_dict(exclude_unset=True) == {"enabled": False}


def test_absent_attachments_leave_the_bindings_alone():
    assert "attachments" not in PolicyUpdateRequest(enabled=True).to_dict(exclude_unset=True)


def test_an_empty_attachment_list_is_an_explicit_detach_all():
    assert PolicyUpdateRequest(attachments=[]).to_dict(exclude_unset=True) == {"attachments": []}


def test_create_request_defaults_to_an_enabled_policy_with_no_opinions():
    data = PolicyCreateRequest(name="eu-only")
    assert data.enabled is True
    assert data.allowed_regions == []
    assert data.allowed_clusters == []
    assert data.attachments == []


def test_attachment_request_names_a_subject_and_its_type():
    request = PolicyAttachmentRequest(subject_type="project", subject="research")
    assert request.subject_type == "project"


def test_controller_exposes_full_crud_on_a_declared_object():
    handlers = {h.handler_name for h in PolicyRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {"list_policies", "create_policy", "get_policy", "update_policy", "delete_policy"}


def test_controller_path_matches_the_contract():
    assert PolicyRoutes.path == "/policies"


def test_update_accepts_if_match_against_the_revision():
    parameter = inspect.signature(PolicyRoutes.update_policy.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"


def test_class_filter_is_exposed_as_class_not_device_class():
    parameter = inspect.signature(PolicyRoutes.list_policies.fn).parameters["device_class"]
    assert parameter.default.query == "class"
