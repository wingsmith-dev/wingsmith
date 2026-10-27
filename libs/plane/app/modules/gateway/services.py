import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Tenant
from app.db.models import TenantAPIKey
from app.modules.tenants.services import needs_key

from .schemas import GatewayTenantSchema
from .schemas import GatewayTenantTableSchema
from .repository import GatewayRepository


def _stamp(row: Tenant | TenantAPIKey | None) -> dt.datetime | None:
    if row is None:
        return None
    return row.updated_at or row.created_at


def _revision_of(stamps: list[dt.datetime | None]) -> str:
    """The newest stamp this table was built from, and how many fed it.

    Same shape as `endpoints.services._revision_of`, minus its content term:
    there is no engine-facing name to hash here, only rows that appear or stop
    appearing. The count is what a delete moves: a tenant can be deleted
    outright, and unless its stamp happened to be the maximum across the whole
    table, the maximum alone would not catch that it is gone.
    """
    live = [stamp for stamp in stamps if stamp is not None]
    if not live:
        return "0-0"
    return f"{int(max(live).timestamp() * 1_000_000)}-{len(live)}"


class GatewayService:
    """What the gateway itself reads. A read-only projection of `tenants`:
    this module writes nothing and has no create, update or delete."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = GatewayRepository(db)

    def _tenant_schema(self, tenant: Tenant) -> GatewayTenantSchema:
        return GatewayTenantSchema(
            id=tenant.uid,
            slug=tenant.slug,
            enabled=tenant.enabled,
            ingress=tenant.ingress,
            # A stored `none` the public listener can reach goes out as
            # `api_key`, so a tenant from before the create-time check is not
            # an open proxy. The gateway then wants a key the tenant may not
            # have minted yet, which is the point.
            auth="api_key" if needs_key(tenant.ingress, tenant.auth, tenant.host) else tenant.auth,
            host=tenant.host,
            key_hashes=[key.key_hash for key in tenant.api_keys if key.revoked_at is None],
        )

    async def resolve_tenants(self) -> GatewayTenantTableSchema:
        """Host→tenant and hash→key, in one read, for enabled tenants only.

        A disabled tenant is left out of the payload, not sent with
        `enabled: false`, because the gateway's public listener resolves a host
        and checks a key without reading that flag: left in, a switched-off
        tenant kept serving. Out, its host is unknown and its keys match nothing.

        Its stamp still feeds the revision, as a revoked key's does (see
        `GatewayTenantSchema`), so disabling or re-enabling a tenant is never
        missed behind a `304`.
        """
        tenants = await self.repo.tenants()
        stamps: list[dt.datetime | None] = []
        items: list[GatewayTenantSchema] = []
        for tenant in tenants:
            stamps.append(_stamp(tenant))
            stamps.extend(_stamp(key) for key in tenant.api_keys)
            if tenant.enabled:
                items.append(self._tenant_schema(tenant))
        revision = _revision_of(stamps)
        if settings.TENANT_ALLOW_PUBLIC_NO_AUTH:
            # The opt-in changes what `auth` this table reports without moving
            # any row's stamp; a distinct revision keeps a gateway holding the
            # other table from being told `304`.
            revision += "-open"
        return GatewayTenantTableSchema(revision=revision, tenants=items)
