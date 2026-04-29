"""The one unauthenticated write in the API: admin login.

Everything else goes through `request_context`. This controller exists
because a session token has to come from somewhere, and it cannot come from a
route that itself requires a session token.

Being unauthenticated and publicly reachable, it is the only route where an
anonymous caller decides how much work the plane does. Two things follow, and
both are load-bearing rather than defensive habit:

- The bcrypt verification runs in a worker thread (`sync_to_thread=True`), not
  on the event loop. A default-cost hash is roughly 300ms of CPU, so a handful
  of concurrent logins on the loop would stall every other request in the
  process — the console's polling and the gateway's routing-table read
  included.
- Failures are budgeted and logged. One shared password with no lockout and no
  log line is a guessing oracle nobody is watching.
"""

from logging import getLogger

from litestar import Controller
from litestar import post

from app.config import settings
from app.core.auth import verify_password
from app.core.auth import issue_session_token
from app.core.throttle import FailureBudget
from app.utils.schemas import Struct
from app.core.exceptions import NotAuthorized
from app.core.exceptions import TooManyRequests


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


class LoginRequest(Struct):
    password: str


class LoginResponse(Struct):
    token: str
    expires_in: int


class AuthRoutes(Controller):
    path = "/auth"
    tags = ["Main"]

    @post("/login", sync_to_thread=True)
    def login(self, data: LoginRequest) -> LoginResponse:
        if LOGIN_GUESSES.spent():
            logger.warning("auth.login_throttled")
            raise TooManyRequests("too many failed logins; retry shortly")
        if not verify_password(data.password, settings.ADMIN_PASSWORD_HASH):
            LOGIN_GUESSES.fail()
            logger.warning("auth.login_failed")
            raise NotAuthorized("invalid credentials")
        return LoginResponse(
            token=issue_session_token(name="admin"),
            expires_in=settings.SESSION_TTL_SECONDS,
        )
