"""Database-backed lifecycle for signed-in sessions.

A session is the chain of refresh tokens one sign-in produces. Its first row is
the session record: its id is the session id every token in the chain and every
access token it issues carries, its `expires_at` is the absolute end of the
session, and its `revoked_at` ends the whole session at once. Later rows exist
so a rotated token can be recognised when it is presented again.

- Rotation is one conditional `UPDATE ... RETURNING`: of two requests racing
  with the same refresh token, exactly one rotates it.
- Presenting a token that was already rotated is reuse, which means two parties
  hold the session. The session is revoked, so both are signed out. The one
  exception is a token rotated less than `REFRESH_RACE_GRACE_SECONDS` ago: that
  is two copies of one client refreshing at once (a duplicated browser tab
  carries its session storage with it), so the late one is refused and the
  session the winner just rotated is left alone.
- Rotation never extends the session: every row inherits the first row's expiry,
  so `REFRESH_TTL_SECONDS` is an absolute lifetime, not a sliding one.
- A session is bound to the credential that opened it — the admin password's
  generation, or the tenant token — so changing the one ends the other.
- `session_is_live` is what the API guard asks on every request a session token
  makes, so logout and revocation take effect on the next request rather than
  when the access token expires.
"""

import hmac
import time
import uuid

from logging import getLogger
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Tenant
from app.db.models import TenantToken
from app.db.models import AuthRefreshToken

from .manager import AuthManager
from .schemas import ADMIN
from .schemas import TENANT
from .schemas import Principal


logger = getLogger(__name__)

TOUCH_INTERVAL_SECONDS = 60.0
"""How stale `TenantToken.last_used_at` is allowed to get.

The field answers "is anybody still using this credential", which is a question
about days. Writing it on every sign-in would put a write in the path of a read
that a polling console repeats, for a resolution nobody reads.
"""


REFRESH_RACE_GRACE_SECONDS = 10.0
"""How recently a token must have been rotated for presenting it again to count as
a race rather than reuse.

Two copies of one client refresh at the same moment, so this only has to cover
a round trip and a little clock jitter. It is short because inside it a thief
who rotated first is not caught by the victim's late refresh; past it, the same
replay ends the session.
"""


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class AuthService:
    """Owns the session lifecycle: open, rotate, revoke, and check."""

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

    async def create_refresh_token(self, name: str, tenant_token_id: uuid.UUID | None = None) -> str:
        """Opens a session and returns its first refresh token's plaintext.

        The session id and generation are inside the plaintext
        (`AuthManager.parse_refresh_token`), which is how the caller mints the
        access token that goes with it.
        """
        session_id = uuid.uuid4()
        generation = tenant_token_id.hex if tenant_token_id is not None else AuthManager.admin_generation()
        plaintext, token_hash = AuthManager.mint_refresh_token(session_id.hex, generation)
        self.db.add(
            AuthRefreshToken(
                id=session_id,
                token_hash=token_hash,
                name=name,
                tenant_token_id=tenant_token_id,
                expires_at=datetime.fromtimestamp(time.time() + settings.REFRESH_TTL_SECONDS, tz=UTC),
            )
        )
        await self.db.commit()
        return plaintext

    async def _session(self, session_id: str) -> AuthRefreshToken | None:
        """The session's first row, if the session exists and has not ended."""
        sid = _uuid(session_id)
        if sid is None:
            return None
        root = await self.db.get(AuthRefreshToken, sid)
        if root is None or root.revoked_at is not None or root.expires_at <= datetime.now(tz=UTC):
            return None
        return root

    async def _credential(self, root: AuthRefreshToken, generation: str) -> tuple[str, str | None, str | None] | None:
        """`(kind, scope, slug)` if the credential that opened the session still holds."""
        if root.tenant_token_id is None:
            if not hmac.compare_digest(generation.encode(), AuthManager.admin_generation().encode()):
                return None
            return ADMIN, None, None
        if generation != root.tenant_token_id.hex:
            return None
        live = await self.db.execute(
            select(TenantToken, Tenant)
            .join(Tenant, Tenant.id == TenantToken.tenant_id)
            .where(TenantToken.id == root.tenant_token_id)
        )
        pair = live.first()
        if pair is None or pair[0].revoked_at is not None or not pair[1].enabled:
            return None
        return TENANT, pair[1].uid, pair[1].slug

    async def _revoke_session(self, session_id: str) -> None:
        sid = _uuid(session_id)
        if sid is None:
            return
        await self.db.execute(
            update(AuthRefreshToken)
            .where(AuthRefreshToken.id == sid, AuthRefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(tz=UTC))
        )
        await self.db.commit()

    async def rotate_refresh_token(self, plaintext: str) -> tuple[str, str, str, str | None] | None:
        """Validates a refresh token and rotates it.

        Returns `(new_refresh_plaintext, new_access_token, kind, tenant_slug)` on
        success, and `None` on any invalid, expired, revoked or already-rotated
        token — and on a session whose credential has since changed: a new admin
        password, or a tenant token revoked or a tenant disabled.
        """
        parsed = AuthManager.parse_refresh_token(plaintext)
        if parsed is None:
            return None
        session_id, generation = parsed
        root = await self._session(session_id)
        if root is None:
            return None
        credential = await self._credential(root, generation)
        if credential is None:
            return None
        kind, scope, slug = credential

        token_hash = AuthManager.hash_refresh_token(plaintext)
        new_plaintext, new_hash = AuthManager.mint_refresh_token(session_id, generation)
        claimed = await self.db.execute(
            update(AuthRefreshToken)
            .where(
                AuthRefreshToken.token_hash == token_hash,
                AuthRefreshToken.replaced_by.is_(None),
                AuthRefreshToken.revoked_at.is_(None),
            )
            .values(replaced_by=new_hash, updated_at=func.now())
            .returning(AuthRefreshToken.name)
        )
        name = claimed.scalar_one_or_none()
        if name is None:
            await self.db.rollback()
            await self._refused_rotation(session_id, token_hash)
            return None

        self.db.add(
            AuthRefreshToken(
                token_hash=new_hash,
                name=name,
                tenant_token_id=root.tenant_token_id,
                expires_at=root.expires_at,
            )
        )
        await self.db.commit()

        access_token = AuthManager.issue_session_token(
            name=name, kind=kind, scope=scope, session_id=session_id, generation=generation
        )
        return new_plaintext, access_token, kind, slug

    async def _refused_rotation(self, session_id: str, token_hash: str) -> None:
        """Decides what a refresh token that could not be rotated means.

        Rotated within `REFRESH_RACE_GRACE_SECONDS` (by the database's clock, the
        same one that stamped the rotation), it lost a race with its own client and
        the session stands. Rotated earlier, it is reuse and the session ends.
        Never rotated at all, it was revoked, and there is nothing more to end.
        """
        result = await self.db.execute(
            select(AuthRefreshToken.updated_at >= func.now() - timedelta(seconds=REFRESH_RACE_GRACE_SECONDS)).where(
                AuthRefreshToken.token_hash == token_hash,
                AuthRefreshToken.replaced_by.is_not(None),
            )
        )
        row = result.first()
        if row is None:
            return
        if row[0]:
            logger.info("auth.refresh_raced", extra={"session": session_id})
            return
        logger.warning("auth.refresh_reused", extra={"session": session_id})
        await self._revoke_session(session_id)

    async def revoke_refresh_token(self, plaintext: str) -> None:
        """Ends the session this refresh token belongs to. Idempotent.

        Any token of the session will do, rotated or not: holding one is proof
        enough of being the party that signed in. An unknown token is a no-op.
        """
        parsed = AuthManager.parse_refresh_token(plaintext)
        if parsed is None:
            return
        token_hash = AuthManager.hash_refresh_token(plaintext)
        known = await self.db.execute(select(AuthRefreshToken.id).where(AuthRefreshToken.token_hash == token_hash))
        if known.first() is None:
            return
        await self._revoke_session(parsed[0])

    async def session_is_live(self, principal: Principal) -> bool:
        """Whether the session a principal's access token names may still be used.

        Asked by the API guard on every request a session token makes. One read:
        the session's first row, joined to the tenant token for a tenant session.
        """
        if principal.session_id is None:
            return False
        root = await self._session(principal.session_id)
        if root is None:
            return False
        if principal.kind == ADMIN:
            return root.tenant_token_id is None
        if principal.kind != TENANT or root.tenant_token_id is None:
            return False
        live = await self.db.execute(
            select(Tenant.uid)
            .join(TenantToken, TenantToken.tenant_id == Tenant.id)
            .where(
                TenantToken.id == root.tenant_token_id,
                TenantToken.revoked_at.is_(None),
                Tenant.enabled.is_(True),
            )
        )
        return live.scalar_one_or_none() == principal.scope
