import pytest

from app.core.ids import PROJECT
from app.core.ids import new_uid
from app.modules.projects.routes import ProjectRoutes
from app.modules.projects.schemas import ProjectSchema
from app.modules.projects.schemas import ProjectTenantSchema
from app.modules.projects.schemas import ProjectCreateRequest
from app.modules.projects.schemas import ProjectUpdateRequest
from app.modules.projects.repository import ProjectRepository
from app.modules.projects.repository import tenant_ids
from app.modules.projects.repository import priority_class_ids


@pytest.fixture
def repo():
    return ProjectRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_tenant_is_a_field_on_the_project_not_a_collection():
    assert "tenant" in ProjectSchema.__struct_fields__
    assert set(ProjectTenantSchema.__struct_fields__) == {"id", "slug", "name", "enabled"}


def test_detail_carries_a_prefixed_id_and_a_revision():
    fields = ProjectSchema.__struct_fields__
    assert "id" in fields
    assert "revision" in fields


def test_project_ids_are_lowercase_and_prefixed():
    uid = new_uid(PROJECT)
    assert uid.startswith("prj_")
    assert uid == uid.lower()
    assert len(uid) == 30


def test_update_request_leaves_absent_fields_unset():
    assert ProjectUpdateRequest(name="research").to_dict(exclude_unset=True) == {"name": "research"}


def test_update_request_can_clear_the_default_priority_class_with_null():
    fields = ProjectUpdateRequest(default_priority_class=None).to_dict(exclude_unset=True)
    assert fields == {"default_priority_class": None}


def test_a_project_cannot_change_tenant_or_slug_by_patch():
    fields = ProjectUpdateRequest.__struct_fields__
    assert "tenant" not in fields
    assert "slug" not in fields


def test_create_request_defaults_to_enabled():
    data = ProjectCreateRequest(tenant="acme", slug="research", name="Research")
    assert data.enabled is True
    assert data.default_priority_class is None


def test_tenant_reference_resolves_against_slug_because_tenants_have_no_uid():
    text = sql(tenant_ids("acme"))
    assert "FROM bind_tenant" in text
    assert "bind_tenant.slug = " in text


def test_priority_class_reference_resolves_against_name():
    assert "bind_priority_class.name = " in sql(priority_class_ids("production"))


def test_get_query_resolves_a_slug_or_an_id(repo):
    assert "bind_project.slug = " in sql(repo.get_query("research"))
    assert "bind_project.uid = " in sql(repo.get_query(new_uid(PROJECT)))


def test_get_query_narrows_an_ambiguous_slug_by_tenant(repo):
    text = sql(repo.get_query("research", tenant="acme"))
    assert "bind_project.tenant_id IN (SELECT bind_tenant.id" in text


def test_list_query_joins_tenant_and_default_priority_class(repo):
    text = sql(repo.list_query(limit=10))
    assert "JOIN bind_tenant ON bind_project.tenant_id = bind_tenant.id" in text
    assert "LEFT OUTER JOIN bind_priority_class" in text
    assert "ORDER BY bind_project.seq ASC" in text


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(repo.list_query(limit=10, after_seq=7, tenant="acme", enabled=True, search="res"))
    assert "bind_project.seq > " in text
    assert "bind_project.enabled IS true" in text
    assert "lower(bind_project.name) LIKE lower(" in text


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=25).compile().params["param_1"] == 26


def test_controller_exposes_full_crud_on_a_declared_object():
    handlers = {h.handler_name for h in ProjectRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {"list_projects", "create_project", "get_project", "update_project", "delete_project"}


def test_controller_path_matches_the_contract():
    assert ProjectRoutes.path == "/projects"


def test_no_label_filter_is_offered_because_projects_carry_none():
    import inspect

    assert "label" not in inspect.signature(ProjectRoutes.list_projects.fn).parameters
