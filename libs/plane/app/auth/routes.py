"""The auth routes: the endpoints that issue, rotate, and revoke credentials.

Everything else in the API goes through `request_context`. These routes exist
because a session token has to come from somewhere, and it cannot come from a
route that itself requires a session token.

Being unauthenticated and publicly reachable, the login route is the only place
where an anonymous caller decides how much work the plane does. Two things
follow, and both are load-bearing rather than defensive habit:

- The bcrypt verification runs in a worker thread (`asyncio.to_thread`), not
  on the event loop. A default-cost hash is roughly 300ms of CPU, so a handful
  of concurrent logins on the loop would stall every other request in the
  process — the console's polling and the gateway's routing-table read
  included.
- Failures are budgeted and logged. One shared password with no lockout and no
  log line is a guessing oracle nobody is watching.
"""

import asyncio

from logging import getLogger

from litestar import Controller
from litestar import post
from litestar import status_codes
from litestar.di import Provide

from app.config import settings
from app.core.throttle import FailureBudget
from app.core.exceptions import NotAuthorized
from app.core.exceptions import TooManyRequests

from .depends import auth_svc_generator
from .manager import AuthManager
from .schemas import LoginRequest
from .schemas import LoginResponse
from .schemas import LogoutRequest
from .schemas import RefreshRequest
from .schemas import RefreshResponse
from .service import AuthService


logger = getLogger(__name__)

LOGIN_GUESSES = FailureBudget(limit=10, window_seconds=60.0)
"""Failed logins this process will answer in a rolling minute.

Ten is far above what a human mistyping one shared password produces and far
below what guessing it needs.

Consulted *before* the hash is verified, unlike the tunnel-auth budget. That is
the difference between bounding guesses and bounding work: an exhausted budget
answers 429 without spending bcrypt, which is the only thing that stops an
anonymous caller from buying 300ms of this process's CPU per request. The cost
is that a correct password is also refused for the rest of the window — a real
denial of service against the operator, and the right trade only because the
window is a minute and the alternative is a denial of service against the whole
plane.

Per-process like every other budget here (see `app.core.throttle`) — two
replicas mean twice the limit, which is fine for a window whose purpose is to be
slow and loud rather than exact.
"""


TENANT_GUESSES = FailureBudget(limit=60, window_seconds=60.0)
"""Failed tenant sign-ins this process will answer in a rolling minute.

Its own budget rather than a share of `LOGIN_GUESSES`, and the reason is who gets
locked out. One budget across both kinds would let anyone spend the operator's
allowance with sixty wrong tenant tokens and leave the fleet unable to sign in
during an incident — a denial of service handed to an anonymous caller.

Six times the admin limit because the two paths bound different things. There the
budget bounds *work*, since a guess costs a bcrypt; here it costs one indexed
lookup, so what it bounds is guessing against 256 bits of randomness, which no
rate makes reachable. It is a loud line in the log long before it is a defence.
"""


class AuthRoutes(Controller):
    path = "/auth"
    tags = ["Main"]
    dependencies = {"svc": Provide(auth_svc_generator)}

    @post("/login")
    async def login(self, data: LoginRequest, svc: AuthService) -> LoginResponse:
        """One form, two credentials, told apart by the slug the caller names.

        `admin` is the fleet operator and costs a bcrypt verification. Anything else
        is a tenant and costs one indexed lookup, so the budget is spent before the
        admin path and not before the tenant one: a hash is the expensive thing an
        anonymous caller can buy here, and a hash is what the tenant path never does.

        Both failures answer the same 401 with the same words. Distinguishing "no
        such tenant" from "wrong token" at an unauthenticated form is an enumeration
        oracle for the tenant list.
        """
        if data.slug and data.slug != "admin":
            if TENANT_GUESSES.spent():
                logger.warning("auth.login_throttled", extra={"kind": "tenant"})
                raise TooManyRequests("too many failed sign-ins; retry shortly")
            resolved = await svc.resolve_tenant_token(data.password)
            if resolved is None:
                TENANT_GUESSES.fail()
                logger.warning("auth.login_failed", extra={"slug": data.slug, "kind": "tenant"})
                raise NotAuthorized("invalid credentials")
            tenant, token = resolved
            if tenant.slug != data.slug:
                TENANT_GUESSES.fail()
                logger.warning("auth.login_failed", extra={"slug": data.slug, "kind": "tenant"})
                raise NotAuthorized("invalid credentials")
            return LoginResponse(
                access_token=AuthManager.issue_session_token(name=tenant.slug, kind="tenant", scope=tenant.uid),
                refresh_token=await svc.create_refresh_token(name=tenant.slug, tenant_token_id=token.id),
                expires_in=settings.SESSION_TTL_SECONDS,
                kind="tenant",
                tenant=tenant.slug,
            )

        if LOGIN_GUESSES.spent():
            logger.warning("auth.login_throttled")
            raise TooManyRequests("too many failed logins; retry shortly")
        if not await asyncio.to_thread(AuthManager.verify_admin_password, data.password, settings.ADMIN_PASSWORD_HASH):
            LOGIN_GUESSES.fail()
            logger.warning("auth.login_failed")
            raise NotAuthorized("invalid credentials")
        access_token = AuthManager.issue_session_token(name="admin")
        refresh_token = await svc.create_refresh_token(name="admin")
        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.SESSION_TTL_SECONDS,
        )

    @post("/refresh")
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
        await svc.revoke_refresh_token(data.refresh_token)
