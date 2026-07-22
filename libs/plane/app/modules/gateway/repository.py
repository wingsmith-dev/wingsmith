from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tenant


class GatewayRepository:
    """Reads what the gateway needs to trust a tenant. Nothing here writes —
    this surface is a projection of `tenants`, not a second copy of it."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def tenants_query(self) -> Select:
        """Every tenant with its keys eager-loaded, one round trip.

        `selectinload` rather than a join: `Tenant.api_keys` is one-to-many, and
        a join here would multiply every tenant row by its key count for no
        reason — the resolution below reads each tenant once and its keys as a
        list beside it.
        """
        return select(Tenant).options(selectinload(Tenant.api_keys)).order_by(Tenant.seq.asc())

    async def tenants(self) -> list[Tenant]:
        result = await self.db.execute(self.tenants_query())
        return list(result.scalars().unique().all())
