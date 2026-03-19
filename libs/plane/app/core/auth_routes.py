"""The one unauthenticated write in the API: admin login.

Everything else goes through `request_context`. This controller exists
because a session token has to come from somewhere, and it cannot come from a
route that itself requires a session token.
"""

from litestar import Controller
from litestar import post

from app.config import settings
from app.core.auth import verify_password
from app.core.auth import issue_session_token
from app.utils.schemas import Struct
from app.core.exceptions import NotAuthorized


class LoginRequest(Struct):
    password: str


class LoginResponse(Struct):
    token: str
    expires_in: int


class AuthRoutes(Controller):
    path = "/auth"
    tags = ["Main"]

    @post("/login", sync_to_thread=False)
    def login(self, data: LoginRequest) -> LoginResponse:
        if not verify_password(data.password, settings.ADMIN_PASSWORD_HASH):
            raise NotAuthorized("invalid credentials")
        return LoginResponse(
            token=issue_session_token(name="admin"),
            expires_in=settings.SESSION_TTL_SECONDS,
        )
