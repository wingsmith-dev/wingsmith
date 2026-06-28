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
    """One form for both kinds of caller: a handle and a credential.

    `slug` names who is signing in — `admin` for the people who operate the fleet,
    a tenant's slug for the people we serve. It defaults to `admin` so that every
    caller written against the single-field form keeps working unchanged.
    """

    password: str
    slug: str = "admin"


class LoginResponse(Struct):
    """`kind` and `tenant` say what was signed in to.

    The console needs it to decide which application to render, and it must come
    from here rather than from what was typed: the slug is a claim until the
    credential behind it is verified.
    """

    access_token: str
    refresh_token: str
    expires_in: int
    kind: str = "admin"
    tenant: str | None = None


class RefreshRequest(Struct):
    refresh_token: str


class RefreshResponse(Struct):
    access_token: str
    refresh_token: str
    expires_in: int
    kind: str = "admin"
    tenant: str | None = None


class LogoutRequest(Struct):
    refresh_token: str
