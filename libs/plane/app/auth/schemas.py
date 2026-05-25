"""Wire shapes and the principal identity."""

from app.utils.schemas import Struct


class Principal(Struct):
    """The identity of a caller.

    Kept deliberately thin so a future OIDC/SSO integration can replace how a
    `Principal` is produced without touching a single call site that consumes
    one. `scope` is a seam for future tenant/project scoping — nothing in this
    change reads it; per-request project filtering stays out of scope here.
    """

    id: str
    kind: str
    name: str
    scopes: tuple[str, ...] = ()
    scope: str | None = None


class LoginRequest(Struct):
    password: str


class LoginResponse(Struct):
    access_token: str
    refresh_token: str
    expires_in: int


class RefreshRequest(Struct):
    refresh_token: str


class RefreshResponse(Struct):
    access_token: str
    refresh_token: str
    expires_in: int


class LogoutRequest(Struct):
    refresh_token: str
