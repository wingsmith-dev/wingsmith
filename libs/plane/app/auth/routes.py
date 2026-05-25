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
from .manager import AuthManager
from .schemas import LoginRequest
from .schemas import LoginResponse
from .schemas import RefreshRequest
from .schemas import RefreshResponse
from .schemas import LogoutRequest
from .depends import auth_svc_generator
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


class AuthRoutes(Controller):
    path = "/auth"
    tags = ["Main"]
    dependencies = {"svc": Provide(auth_svc_generator)}

    @post("/login")
    async def login(self, data: LoginRequest, svc: AuthService) -> LoginResponse:
        if LOGIN_GUESSES.spent():
            logger.warning("auth.login_throttled")
            raise TooManyRequests("too many failed logins; retry shortly")
        if not await asyncio.to_thread(
            AuthManager.verify_admin_password, data.password, settings.ADMIN_PASSWORD_HASH
        ):
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
        new_refresh, access_token = result
        return RefreshResponse(
            access_token=access_token,
            refresh_token=new_refresh,
            expires_in=settings.SESSION_TTL_SECONDS,
        )

    @post("/logout", status_code=status_codes.HTTP_204_NO_CONTENT)
    async def logout(self, data: LogoutRequest, svc: AuthService) -> None:
        await svc.revoke_refresh_token(data.refresh_token)
