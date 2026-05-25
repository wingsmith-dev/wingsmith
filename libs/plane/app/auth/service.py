"""Database-backed lifecycle for admin refresh tokens.

Refresh tokens are the one piece of auth state that must survive a server
restart, so they cannot be stateless like session tokens. Every operation here
is idempotent or atomic: rotation replaces one token with the next in a single
commit, and revocation is a no-op on an already-revoked or unknown token.
"""

import time

from datetime import datetime
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from .manager import AuthManager
from app.db.models import AuthRefreshToken


class AuthService:
    """Owns the refresh-token lifecycle: create, rotate, revoke."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_refresh_token(self, name: str) -> str:
        """Mints, stores, and returns a new refresh token's plaintext."""
        plaintext, token_hash = AuthManager.mint_refresh_token()
        row = AuthRefreshToken(
            token_hash=token_hash,
            name=name,
            expires_at=datetime.fromtimestamp(
                time.time() + settings.REFRESH_TTL_SECONDS,
                tz=timezone.utc,
            ),
        )
        self.db.add(row)
        await self.db.commit()
        return plaintext

    async def rotate_refresh_token(
        self, plaintext: str
    ) -> tuple[str, str] | None:
        """Validates a refresh token and rotates it.

        Returns `(new_refresh_plaintext, new_access_token)` on success, `None`
        on any invalid, expired, revoked, or already-rotated token.
        """
        token_hash = AuthManager.hash_refresh_token(plaintext)
        result = await self.db.execute(
            select(AuthRefreshToken).where(AuthRefreshToken.token_hash == token_hash)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        now = datetime.now(tz=timezone.utc)
        if row.revoked_at is not None or row.replaced_by is not None:
            return None
        if row.expires_at <= now:
            return None

        new_plaintext, new_hash = AuthManager.mint_refresh_token()
        new_row = AuthRefreshToken(
            token_hash=new_hash,
            name=row.name,
            expires_at=datetime.fromtimestamp(
                time.time() + settings.REFRESH_TTL_SECONDS,
                tz=timezone.utc,
            ),
        )
        self.db.add(new_row)
        row.replaced_by = new_hash
        row.revoked_at = now
        await self.db.commit()

        access_token = AuthManager.issue_session_token(name=row.name)
        return new_plaintext, access_token

    async def revoke_refresh_token(self, plaintext: str) -> None:
        """Revokes a refresh token. Idempotent: unknown or already-revoked is a no-op."""
        token_hash = AuthManager.hash_refresh_token(plaintext)
        result = await self.db.execute(
            select(AuthRefreshToken).where(AuthRefreshToken.token_hash == token_hash)
        )
        row = result.scalar_one_or_none()
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.now(tz=timezone.utc)
            await self.db.commit()
