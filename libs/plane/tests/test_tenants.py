"""The billing root, which had no collection until a project needed one.

`libs/packages/contract/API.md` §6 listed no `/v1/tenants`, so the console typed a tenant slug into
the project form and the plane answered `no tenant 'x'` for every value anyone could
type: nothing in the API could make the first one. The collection is the fix, and
these hold its shape to what the model already says a tenant is.
"""

import pytest

from app.core.ids import TENANT
from app.core.ids import new_uid
from app.modules.tenants.routes import TenantRoutes
from app.modules.tenants.schemas import TenantSchema
from app.modules.tenants.schemas import TenantCreateRequest
from app.modules.tenants.schemas import TenantUpdateRequest
from app.modules.tenants.repository import TenantRepository
from app.modules.tenants.repository import tenant_ids


@pytest.fixture
def repo():
    return TenantRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_tenant_ids_are_lowercase_and_prefixed():
    uid = new_uid(TENANT)
    assert uid.startswith("ten_")
    assert uid == uid.lower()
    assert len(uid) == 30


def test_a_tenant_is_referenced_by_id_or_slug(repo):
    assert "slug" in sql(tenant_ids("acme"))


def test_detail_carries_a_prefixed_id_and_a_revision():
    fields = TenantSchema.__struct_fields__
    assert "id" in fields
    assert "revision" in fields


def test_the_read_shape_says_how_much_a_delete_would_take():
    assert "project_count" in TenantSchema.__struct_fields__


def test_slug_is_identity_and_so_is_not_patchable():
    assert set(TenantUpdateRequest.__struct_fields__) == {"name", "enabled", "ingress", "auth", "host"}


def test_update_request_leaves_absent_fields_unset():
    assert TenantUpdateRequest(name="Acme Inc").to_dict(exclude_unset=True) == {"name": "Acme Inc"}


def test_a_tenant_is_enabled_unless_it_says_otherwise():
    assert TenantCreateRequest(slug="acme", name="Acme").enabled is True


def test_the_collection_is_mounted_where_projects_look_for_it():
    assert TenantRoutes.path == "/tenants"


def test_listing_filters_on_the_switch_and_the_name(repo):
    query = sql(repo.list_query(limit=10, enabled=True, search="ac"))
    assert "enabled IS true" in query
    assert "like lower" in query.lower()
