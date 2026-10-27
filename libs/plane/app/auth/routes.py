"""The auth routes: the endpoints that issue, rotate, and revoke credentials.

Everything else in the API goes through `request_context`. These routes exist
because a session token has to come from somewhere, and it cannot come from a
route that itself requires a session token.

Being unauthenticated and publicly reachable, the login route is the only place
where an anonymous caller decides how much work the plane does. Two things
follow, and both are essential, not defensive habit:

- The bcrypt verification runs in a worker thread (`asyncio.to_thread`), not
  on the event loop. A default-cost hash is roughly 300ms of CPU, so a handful
  of concurrent logins on the loop would stall every other request in the
  process: the console's polling and the gateway's routing-table read
  included.
- Failures are budgeted and logged. One shared password with no lockout and no
  log line is a guessing oracle nobody is watching. The budgets are charged to
  the caller's address and the account it named, so a caller that spends one
  locks out itself and not the operator signing in from somewhere else.
"""

import asyncio

from logging import getLogger

from litestar import Request
from litestar import Controller
from litestar import post
from litestar import status_codes
from litestar.di import Provide

from app.config import settings
from app.core.throttle import FailureBudget
from app.core.throttle import KeyedFailureBudget
from app.core.throttle import client_address
from app.core.exceptions import NotAuthorized
from app.core.exceptions import TooManyRequests

from .depends import auth_svc_generator
from .manager import AuthManager
from .schemas import ADMIN
from .schemas import TENANT
from .schemas import LoginRequest
from .schemas import LoginResponse
from .schemas import LogoutRequest
from .schemas import RefreshRequest
from .schemas import RefreshResponse
from .service import AuthService


logger = getLogger(__name__)

LOGIN_GUESSES = KeyedFailureBudget(limit=10, window_seconds=60.0)
"""Failed admin logins one caller may make in a rolling minute, keyed by address.

Ten is far above what a human mistyping one password produces and far below what
guessing it needs.

Consulted *before* the hash is verified, unlike the tunnel-auth budget. That is
the difference between bounding guesses and bounding work: an exhausted budget
answers 429 without spending bcrypt, which is the only thing that stops an
anonymous caller from buying 300ms of this process's CPU per request. Because it
is keyed by the caller's address (`app.core.throttle.client_address`), spending
it refuses that caller only; the operator signing in from anywhere else is
unaffected.

Per-process like every other budget here (see `app.core.throttle`): two
replicas mean twice the limit, which is fine for a window whose purpose is to be
slow and loud, not exact.
"""

LOGIN_CEILING = FailureBudget(limit=200, window_seconds=60.0)
"""Failed admin logins this process answers in a rolling minute from everyone.

A backstop on bcrypt work for a caller spread across many addresses, set far
enough above `LOGIN_GUESSES` that one caller, or a handful, never reaches it.
Reaching it refuses every admin login for the rest of the window; that is a
denial of service only a distributed caller can buy, in exchange for keeping the
process's CPU for everyone else's requests.
"""


TENANT_GUESSES = KeyedFailureBudget(limit=60, window_seconds=60.0)
"""Failed tenant sign-ins one caller may make against one tenant in a rolling minute.

Its own budget instead of a share of `LOGIN_GUESSES`, so wrong tenant tokens
never spend the operator's allowance. Keyed by the caller's address and the
tenant slug, so one caller cannot lock a tenant out for everyone, nor one tenant's
users lock out another sharing their address.

Six times the admin limit because the two paths bound different things. There the
budget bounds *work*, since a guess costs a bcrypt; here it costs one indexed
lookup, so what it bounds is guessing against 256 bits of randomness, which no
rate makes reachable. It is a loud line in the log long before it is a defence.
"""


def _access_token(refresh_token: str, *, name: str, kind: str, scope: str | None = None) -> str:
    """The access token for the session a freshly minted refresh token opened."""
    parsed = AuthManager.parse_refresh_token(refresh_token)
    assert parsed is not None
    session_id, generation = parsed
    return AuthManager.issue_session_token(
        name=name, kind=kind, scope=scope, session_id=session_id, generation=generation
    )


class AuthRoutes(Controller):
    path = "/auth"
    tags = ["Main"]
    dependencies = {"svc": Provide(auth_svc_generator)}

    @post("/login", status_code=status_codes.HTTP_200_OK)
    async def login(self, request: Request, data: LoginRequest, svc: AuthService) -> LoginResponse:
        """One form, two credentials, told apart by the slug the caller names.

        `admin` is the fleet operator and costs a bcrypt verification. Anything else
        is a tenant and costs one indexed lookup, so the budget is spent before the
        admin path and not before the tenant one: a hash is the expensive thing an
        anonymous caller can buy here, and a hash is what the tenant path never does.

        Both failures answer the same 401 with the same words. Distinguishing "no
        such tenant" from "wrong token" at an unauthenticated form is an enumeration
        oracle for the tenant list.
        """
        address = client_address(request)
        if data.slug and data.slug != "admin":
            key = f"{address}|{data.slug}"
            if TENANT_GUESSES.spent(key):
                logger.warning("auth.login_throttled", extra={"kind": "tenant", "client": address})
                raise TooManyRequests("too many failed sign-ins; retry shortly")
            resolved = await svc.resolve_tenant_token(data.password)
            if resolved is None or resolved[0].slug != data.slug:
                TENANT_GUESSES.fail(key)
                logger.warning("auth.login_failed", extra={"slug": data.slug, "kind": "tenant", "client": address})
                raise NotAuthorized("invalid credentials")
            tenant, token = resolved
            refresh_token = await svc.create_refresh_token(name=tenant.slug, tenant_token_id=token.id)
            return LoginResponse(
                access_token=_access_token(refresh_token, name=tenant.slug, kind=TENANT, scope=tenant.uid),
                refresh_token=refresh_token,
                expires_in=settings.SESSION_TTL_SECONDS,
                kind=TENANT,
                tenant=tenant.slug,
            )

        key = f"{address}|admin"
        if LOGIN_GUESSES.spent(key) or LOGIN_CEILING.spent():
            logger.warning("auth.login_throttled", extra={"kind": "admin", "client": address})
            raise TooManyRequests("too many failed logins; retry shortly")
        if not await asyncio.to_thread(AuthManager.verify_admin_password, data.password, settings.ADMIN_PASSWORD_HASH):
            LOGIN_GUESSES.fail(key)
            LOGIN_CEILING.fail()
            logger.warning("auth.login_failed", extra={"kind": "admin", "client": address})
            raise NotAuthorized("invalid credentials")
        refresh_token = await svc.create_refresh_token(name="admin")
        return LoginResponse(
            access_token=_access_token(refresh_token, name="admin", kind=ADMIN),
            refresh_token=refresh_token,
            expires_in=settings.SESSION_TTL_SECONDS,
        )

    @post("/refresh", status_code=status_codes.HTTP_200_OK)
    async def refresh(self, data: RefreshRequest, svc: AuthService) -> RefreshResponse:
        result = await svc.rotate_refresh_token(data.refresh_token)
        if result is None:
            raise NotAuthorized("invalid or expired refresh token")
        new_refresh, access_token, kind, tenant = result
        return RefreshResponse(
            access_token=access_token,
            refresh_token=new_refresh,
            expires_in=settings.SESSION_TTL_SECONDS,
            kind=kind,
            tenant=tenant,
        )

    @post("/logout", status_code=status_codes.HTTP_204_NO_CONTENT)
    async def logout(self, data: LogoutRequest, svc: AuthService) -> None:
        """Ends the session the refresh token belongs to, and every access token it issued.

        `204` whether or not the token was known, so the route says nothing about
        which tokens exist.
        """
        await svc.revoke_refresh_token(data.refresh_token)
