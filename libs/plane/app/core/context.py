"""Who is calling, decided once per request for every route.

Authentication is an app-wide guard (`authenticate`), not a dependency a handler
opts into by naming a `context` parameter. A handler written without `context`
is authenticated and authorized exactly like one written with it; `context` only
hands the already-decided principal and a database session to the handler.

What each principal kind may reach is decided here, in `authorize`, and nowhere
else. It is deny-by-default per kind:

- `admin` reaches every route except the ones built for the gateway, so the
  tenant key-hash feed never reaches a console.
- `tenant` reaches `GET` under `TENANT_SURFACE` only.
- `gateway` reaches the three routes in `GATEWAY_ROUTES` only.
- `operator` reaches nothing: operator credentials open hub sessions only.

A route is matched by its template (`/v1/clusters/{ref}`), not by the raw path,
so a trailing slash or an encoded segment cannot route around a rule.
"""

from collections.abc import AsyncGenerator

from litestar import Request
from litestar.connection import ASGIConnection
from litestar.handlers.base import BaseRouteHandler
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ADMIN
from app.auth import TENANT
from app.auth import GATEWAY
from app.auth import Principal
from app.auth import AuthManager
from app.db.session import provide_session
from app.core.exceptions import Forbidden
from app.core.exceptions import NotAuthorized


API_PREFIX = "/v1"
"""Everything under this prefix is authenticated unless it is in `PUBLIC_ROUTES`.
Outside it live `/health`, the OpenAPI schema and the console's static files."""

PUBLIC_ROUTES = frozenset({"/v1/auth/login", "/v1/auth/refresh", "/v1/auth/logout"})
"""The API routes an anonymous caller may reach. Each one checks the credential it
is handed itself — a password, a tenant token, a refresh token — because a
session has to come from somewhere."""

TENANT_SURFACE = "/v1/serving"
"""The only path prefix a tenant principal may reach, and it is read-only.

An allowlist of one, enforced here rather than as a filter inside each module,
because those are two different promises. A per-module filter says every module
remembered to scope its query; this says a tenant principal never reaches a
module that was not written for it — including the next one somebody adds. The
cost is that widening the tenant surface is a deliberate edit to this constant,
which is the point.

The prefix is matched at a segment boundary, not as a string. `/v1/serving-quotas`
begins with `/v1/serving` and is a different surface entirely — the commercial
terms of every endpoint on the fleet.

Read-only is enforced by the method rather than by the routes behind it. Every
handler under the prefix is a `GET` today, and stating it here means a write
added there later is refused until somebody decides it should not be.
"""

GATEWAY_ROUTES = frozenset(
    {
        ("GET", "/v1/gateway/tenants"),
        ("GET", "/v1/endpoints/resolved"),
        ("POST", "/v1/clusters/tunnel-auth"),
    }
)
"""Everything the gateway principal may call: the tenant table, the routing table,
and agent-token verification for its tunnel listener."""

SERVICE_ONLY = ("/v1/gateway", "/v1/clusters/tunnel-auth")
"""Routes built for the gateway that an admin session is refused on.

`/v1/gateway/tenants` carries every tenant's live key hashes, which must never
reach a console response. Tunnel verification turns an enrolment token into a
cluster identity; a human credential that can do that can sweep for tokens."""


def _within(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def route_of(connection: ASGIConnection) -> str:
    """The matched route's template, falling back to the raw path."""
    return connection.scope.get("path_template") or connection.scope["path"]


def is_public(route: str) -> bool:
    return not _within(route, API_PREFIX) or route in PUBLIC_ROUTES


def authorize(principal: Principal, method: str, route: str) -> None:
    """Raises `Forbidden` unless this principal's kind may make this request."""
    if principal.kind == ADMIN:
        if any(_within(route, prefix) for prefix in SERVICE_ONLY):
            raise Forbidden("this route is for the gateway's service credential only")
        return
    if principal.kind == TENANT:
        if method == "GET" and _within(route, TENANT_SURFACE):
            return
        raise Forbidden("this credential is scoped to its own serving view")
    if principal.kind == GATEWAY:
        if (method, route) in GATEWAY_ROUTES:
            return
        raise Forbidden("the gateway's service credential does not reach this route")
    raise Forbidden("this credential does not reach the HTTP API")


async def authenticate(connection: ASGIConnection, _: BaseRouteHandler) -> None:
    """App-wide guard: resolves and authorizes the caller of every non-public route.

    The principal is left on `connection.state` for `request_context` to hand to
    the handler, so it is resolved once per request.
    """
    route = route_of(connection)
    if is_public(route):
        return
    principal = AuthManager.resolve_principal(AuthManager.bearer_token(connection.headers.get("Authorization")))
    authorize(principal, connection.scope.get("method", "GET"), route)
    connection.state.principal = principal


class RequestContext:
    """What every handler receives: who is calling, and the open session."""

    __slots__ = ("db", "principal")

    def __init__(self, principal: Principal, db: AsyncSession) -> None:
        self.principal = principal
        self.db = db


async def request_context(request: Request) -> AsyncGenerator[RequestContext, None]:
    """Litestar dependency providing the guard's principal and a db session."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise NotAuthorized("missing or invalid credentials")
    async for session in provide_session():
        yield RequestContext(principal=principal, db=session)
