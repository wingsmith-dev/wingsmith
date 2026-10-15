"""Wire shapes and the principal identity."""

from typing import Literal

from app.utils.schemas import Struct


ADMIN = "admin"
"""A human operating the whole plane through the console or `hgr`. There is one
admin identity: Hangar is single-operator software, and every admin session is
that operator."""

TENANT = "tenant"
"""A tenant's console session, confined to its own read-only serving view."""

OPERATOR = "operator"
"""A cluster operator on the hub. Always bound to one cluster by that cluster's
enrolment token, and refused on every HTTP route."""

GATEWAY = "gateway"
"""The inference gateway reading the routing table, the tenant table and agent
identities. A service credential, never a human, and never an admin."""

PrincipalKind = Literal["admin", "tenant", "operator", "gateway"]


class Principal(Struct):
    """The identity of a caller.

    `kind` is the whole of a principal's authority: what each kind may reach is
    decided in one place, `app.core.context.authorize`, rather than by scopes a
    route would have to remember to check. `scope` narrows a kind to one object
    — the tenant's uid for a tenant, the cluster's uid for an operator — and is
    empty for kinds that are not narrowed. `session_id` names the signed-in
    session an admin or tenant principal came from, which the guard checks is
    still live; it is empty for a service credential.
    """

    id: str
    kind: PrincipalKind
    name: str
    scope: str | None = None
    session_id: str | None = None


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
    """The refresh token of the session to end. Ending it revokes the whole
    session: every refresh token rotated from it and every access token it
    issued, which the guard stops accepting on the next request."""

    refresh_token: str
