"""Database-backed lifecycle for admin refresh tokens.

Refresh tokens are the one piece of auth state that must survive a server
restart, so they cannot be stateless like session tokens. Every operation here
is idempotent or atomic: rotation replaces one token with the next in a single
commit, and revocation is a no-op on an already-revoked or unknown token.
"""

import time

from datetime import UTC
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Tenant
from app.db.models import TenantToken
from app.db.models import AuthRefreshToken

from .manager import AuthManager


TOUCH_INTERVAL_SECONDS = 60.0
"""How stale `TenantToken.last_used_at` is allowed to get.

The field answers "is anybody still using this credential", which is a question
about days. Writing it on every sign-in would put a write in the path of a read
that a polling console repeats, for a resolution nobody reads.
"""


class AuthService:
    """Owns the refresh-token lifecycle: create, rotate, revoke."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def resolve_tenant_token(self, plaintext: str) -> tuple[Tenant, TenantToken] | None:
        """Resolves a tenant console token to its tenant, or `None`.

        `None` covers unknown, revoked, and belonging to a disabled tenant alike.
        The caller answers all three the same way, and telling them apart at the
        login form tells an anonymous guesser which tenants exist.
        """
        result = await self.db.execute(
            select(TenantToken, Tenant)
            .join(Tenant, Tenant.id == TenantToken.tenant_id)
            .where(TenantToken.token_hash == AuthManager.hash_tenant_token(plaintext))
        )
        row = result.first()
        if row is None:
            return None
        token, tenant = row[0], row[1]
        if token.revoked_at is not None or not tenant.enabled:
            return None
        now = datetime.now(tz=UTC)
        if token.last_used_at is None or (now - token.last_used_at).total_seconds() > TOUCH_INTERVAL_SECONDS:
            token.last_used_at = now
            await self.db.commit()
        return tenant, token

    async def create_refresh_token(self, name: str, tenant_token_id=None) -> str:
        """Mints, stores, and returns a new refresh token's plaintext."""
        plaintext, token_hash = AuthManager.mint_refresh_token()
        row = AuthRefreshToken(
            token_hash=token_hash,
            name=name,
            tenant_token_id=tenant_token_id,
            expires_at=datetime.fromtimestamp(
                time.time() + settings.REFRESH_TTL_SECONDS,
                tz=UTC,
            ),
        )
        self.db.add(row)
        await self.db.commit()
        return plaintext

    async def rotate_refresh_token(self, plaintext: str) -> tuple[str, str, str, str | None] | None:
        """Validates a refresh token and rotates it.

        Returns `(new_refresh_plaintext, new_access_token, kind, tenant_slug)` on
        success, `None` on any invalid, expired, revoked, or already-rotated token —
        and on a tenant session whose credential has since been revoked, which is
        what makes revoking a tenant token end the sessions it opened rather than
        only refusing the next sign-in.
        """
        token_hash = AuthManager.hash_refresh_token(plaintext)
        result = await self.db.execute(select(AuthRefreshToken).where(AuthRefreshToken.token_hash == token_hash))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        now = datetime.now(tz=UTC)
        if row.revoked_at is not None or row.replaced_by is not None:
            return None
        if row.expires_at <= now:
            return None

        kind, scope, slug = "admin", None, None
        if row.tenant_token_id is not None:
            live = await self.db.execute(
                select(TenantToken, Tenant)
                .join(Tenant, Tenant.id == TenantToken.tenant_id)
                .where(TenantToken.id == row.tenant_token_id)
            )
            pair = live.first()
            if pair is None or pair[0].revoked_at is not None or not pair[1].enabled:
                return None
            kind, scope, slug = "tenant", pair[1].uid, pair[1].slug

        new_plaintext, new_hash = AuthManager.mint_refresh_token()
        new_row = AuthRefreshToken(
            token_hash=new_hash,
            name=row.name,
            tenant_token_id=row.tenant_token_id,
            expires_at=datetime.fromtimestamp(
                time.time() + settings.REFRESH_TTL_SECONDS,
                tz=UTC,
            ),
        )
        self.db.add(new_row)
        row.replaced_by = new_hash
        row.revoked_at = now
        await self.db.commit()

        access_token = AuthManager.issue_session_token(name=row.name, kind=kind, scope=scope)
        return new_plaintext, access_token, kind, slug

    async def revoke_refresh_token(self, plaintext: str) -> None:
        """Revokes a refresh token. Idempotent: unknown or already-revoked is a no-op."""
        token_hash = AuthManager.hash_refresh_token(plaintext)
        result = await self.db.execute(select(AuthRefreshToken).where(AuthRefreshToken.token_hash == token_hash))
        row = result.scalar_one_or_none()
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.now(tz=UTC)
            await self.db.commit()
