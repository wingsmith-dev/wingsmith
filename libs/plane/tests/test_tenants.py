"""The billing root, which had no collection until a project needed one.

`libs/packages/contract/API.md` §6 listed no `/v1/tenants`, so the console typed a tenant slug into
the project form and the plane answered `no tenant 'x'` for every value anyone could
type: nothing in the API could make the first one. The collection is the fix, and
these hold its shape to what the model already says a tenant is.
"""

import pytest
import msgspec

from litestar.di import Provide
from litestar.testing import create_test_client

from app.auth import AuthManager
from app.core.ids import TENANT
from app.core.ids import new_uid
from app.core.context import RequestContext
from app.core.handlers import EXCEPTION_HANDLERS
from app.core.exceptions import Invalid
from app.modules.tenants.routes import TenantRoutes
from app.modules.tenants.schemas import TenantSchema
from app.modules.tenants.schemas import TenantCreateRequest
from app.modules.tenants.schemas import TenantUpdateRequest
from app.modules.tenants.services import TenantService
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


# --- what a request may carry ----------------------------------------------------


def decode(body: dict, type_=TenantCreateRequest):
    return msgspec.json.decode(msgspec.json.encode(body), type=type_)


@pytest.mark.parametrize(
    "slug",
    ["Acme", "acme corp", "-acme", "acme-", "acme\n", "a" * 64, "", "acme.inc", "acme_inc", "../acme"],
)
def test_a_slug_that_is_not_a_dns_label_is_refused(slug):
    """A slug is a login name and a host prefix; anything else breaks one of them."""
    with pytest.raises(msgspec.ValidationError):
        decode({"slug": slug, "name": "Acme"})


@pytest.mark.parametrize("slug", ["acme", "a", "acme-2", "a" * 63])
def test_a_dns_label_slug_is_accepted(slug):
    assert decode({"slug": slug, "name": "Acme"}).slug == slug


def test_an_unbounded_name_is_refused_before_it_reaches_the_database():
    with pytest.raises(msgspec.ValidationError):
        decode({"slug": "acme", "name": "x" * 201})


@pytest.mark.parametrize(
    "host", ["Acme.example.com", "acme.example.com.", "acme..example", "acme.example.com\n", "a b"]
)
def test_a_host_that_is_not_a_lowercase_hostname_is_refused(host):
    with pytest.raises(msgspec.ValidationError):
        decode({"slug": "acme", "name": "Acme", "host": host})


def test_a_hostname_is_accepted():
    assert decode({"slug": "acme", "name": "Acme", "host": "acme.hangar.example.com"}).host == "acme.hangar.example.com"


def test_ingress_and_auth_are_closed_sets():
    with pytest.raises(msgspec.ValidationError):
        decode({"slug": "acme", "name": "Acme", "auth": "basic"})
    with pytest.raises(msgspec.ValidationError):
        decode({"ingress": "everywhere"}, TenantUpdateRequest)


async def test_the_login_form_s_admin_slug_is_reserved():
    """`admin` on the login form is the fleet operator; a tenant holding it could never sign in."""
    with pytest.raises(Invalid) as refused:
        await TenantService(db=None).create(TenantCreateRequest(slug="admin", name="Admin"))
    assert refused.value.detail == {"reserved": ["admin"]}


def test_a_malformed_slug_is_a_400_over_http():
    context = RequestContext(principal=AuthManager.admin_principal(), db=None)
    with create_test_client(
        route_handlers=[TenantRoutes],
        dependencies={"context": Provide(lambda: context, sync_to_thread=False)},
        exception_handlers=EXCEPTION_HANDLERS,
    ) as http:
        response = http.post("/tenants", json={"slug": "Acme Corp", "name": "Acme"})
    assert response.status_code == 400
