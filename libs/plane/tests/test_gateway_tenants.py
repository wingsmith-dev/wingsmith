import inspect
import datetime as dt

import pytest

from app.config import settings
from app.modules.gateway.routes import GatewayRoutes
from app.modules.gateway.services import GatewayService


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


class FakeKey:
    def __init__(self, key_hash="hash1", revoked_at=None, updated_at=None) -> None:
        self.key_hash = key_hash
        self.revoked_at = revoked_at
        self.created_at = EPOCH
        self.updated_at = updated_at


class FakeTenant:
    def __init__(
        self,
        uid="ten_1",
        slug="acme",
        enabled=True,
        ingress="internal",
        auth="none",
        host=None,
        api_keys=None,
        updated_at=None,
    ) -> None:
        self.uid = uid
        self.slug = slug
        self.enabled = enabled
        self.ingress = ingress
        self.auth = auth
        self.host = host
        self.api_keys = list(api_keys or [])
        self.created_at = EPOCH
        self.updated_at = updated_at


class FakeRepo:
    def __init__(self, tenants=()) -> None:
        self.tenants_list = list(tenants)

    async def tenants(self):
        return self.tenants_list


def resolution(tenants) -> GatewayService:
    svc = GatewayService.__new__(GatewayService)
    svc.db = None
    svc.repo = FakeRepo(tenants)
    return svc


def test_controller_exposes_only_the_resolve_route():
    """Read-only, and one route: this surface writes nothing about a tenant;
    `/v1/tenants` does that."""
    handlers = {h.handler_name for h in GatewayRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {"resolve_tenants"}


def test_the_feed_is_unpaginated_and_conditional():
    """A caller holding this in memory needs the whole table or none of it."""
    parameters = set(inspect.signature(GatewayRoutes.resolve_tenants.fn).parameters)
    assert "if_none_match" in parameters
    assert not parameters & {"limit", "cursor"}


@pytest.mark.asyncio
async def test_the_feed_carries_a_tenants_settings_and_host():
    tenant = FakeTenant(ingress="public", auth="api_key", host="globex.hangar.dev")
    table = await resolution([tenant]).resolve_tenants()
    row = table.tenants[0]
    assert (row.id, row.ingress, row.auth, row.host) == ("ten_1", "public", "api_key", "globex.hangar.dev")


@pytest.mark.asyncio
async def test_only_live_keys_reach_key_hashes():
    tenant = FakeTenant(
        api_keys=[
            FakeKey(key_hash="live"),
            FakeKey(key_hash="dead", revoked_at=EPOCH),
        ]
    )
    table = await resolution([tenant]).resolve_tenants()
    assert table.tenants[0].key_hashes == ["live"]


@pytest.mark.asyncio
async def test_a_revoke_moves_the_revision_even_though_the_hash_count_shrinks():
    """Otherwise a gateway behind a `304` would keep honouring a hash somebody
    just revoked."""
    live = FakeTenant(api_keys=[FakeKey(key_hash="k")])
    before = await resolution([live]).resolve_tenants()

    stamp = EPOCH + dt.timedelta(hours=1)
    revoked = FakeTenant(api_keys=[FakeKey(key_hash="k", revoked_at=stamp, updated_at=stamp)])
    after = await resolution([revoked]).resolve_tenants()

    assert after.revision != before.revision
    assert after.tenants[0].key_hashes == []


@pytest.mark.asyncio
async def test_deleting_a_tenant_moves_the_revision():
    """A tenant can be deleted outright, unlike a key, which is only revoked.
    The count term is what catches a disappearance the maximum stamp alone
    would not."""
    before = await resolution([FakeTenant(uid="ten_1"), FakeTenant(uid="ten_2")]).resolve_tenants()
    after = await resolution([FakeTenant(uid="ten_1")]).resolve_tenants()
    assert after.revision != before.revision


@pytest.mark.asyncio
async def test_an_empty_fleet_resolves_a_stable_value():
    table = await resolution([]).resolve_tenants()
    assert (table.revision, table.tenants) == ("0-0", [])


@pytest.mark.asyncio
async def test_a_disabled_tenant_is_left_out_of_the_feed():
    """The gateway never reads `enabled`, so a disabled tenant in the feed kept serving."""
    table = await resolution(
        [
            FakeTenant(uid="ten_1", host="acme.hangar.dev", api_keys=[FakeKey(key_hash="a")]),
            FakeTenant(uid="ten_2", enabled=False, host="globex.hangar.dev", api_keys=[FakeKey(key_hash="g")]),
        ]
    ).resolve_tenants()
    assert [row.id for row in table.tenants] == ["ten_1"]


@pytest.mark.asyncio
async def test_disabling_a_tenant_moves_the_revision():
    before = await resolution([FakeTenant(uid="ten_1"), FakeTenant(uid="ten_2")]).resolve_tenants()
    stamp = EPOCH + dt.timedelta(hours=1)
    after = await resolution(
        [FakeTenant(uid="ten_1"), FakeTenant(uid="ten_2", enabled=False, updated_at=stamp)]
    ).resolve_tenants()
    assert after.revision != before.revision
    assert [row.id for row in after.tenants] == ["ten_1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ingress", "host"),
    [("public", None), ("internal", "globex.hangar.dev"), ("public", "globex.hangar.dev")],
)
async def test_a_reachable_tenant_stored_without_auth_is_fed_as_api_key(monkeypatch, ingress, host):
    """A row from before the create-time check must not stay an open proxy on the public listener."""
    monkeypatch.setattr(settings, "TENANT_ALLOW_PUBLIC_NO_AUTH", False)
    tenant = FakeTenant(ingress=ingress, auth="none", host=host)
    table = await resolution([tenant]).resolve_tenants()
    assert table.tenants[0].auth == "api_key"


@pytest.mark.asyncio
async def test_an_internal_tenant_without_a_host_keeps_auth_none(monkeypatch):
    monkeypatch.setattr(settings, "TENANT_ALLOW_PUBLIC_NO_AUTH", False)
    table = await resolution([FakeTenant(ingress="internal", auth="none")]).resolve_tenants()
    assert table.tenants[0].auth == "none"


@pytest.mark.asyncio
async def test_the_opt_in_feeds_auth_none_under_its_own_revision(monkeypatch):
    """Flipping the setting moves no row stamp, so the revision has to differ or the gateway sees a 304."""
    tenant = FakeTenant(ingress="public", auth="none", host="globex.hangar.dev")
    monkeypatch.setattr(settings, "TENANT_ALLOW_PUBLIC_NO_AUTH", False)
    closed = await resolution([tenant]).resolve_tenants()
    monkeypatch.setattr(settings, "TENANT_ALLOW_PUBLIC_NO_AUTH", True)
    opened = await resolution([tenant]).resolve_tenants()
    assert opened.tenants[0].auth == "none"
    assert opened.revision != closed.revision
