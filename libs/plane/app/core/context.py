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

Before any of that, a state-changing request from a browser must come from an
origin the plane serves or lists in `ALLOWED_ORIGINS`, and any body must be JSON
(`refuse_cross_site`). That is the CSRF defence, and it matters most under
`ADMIN_AUTH=disabled` in `local`, where the browser's ambient access to
`localhost` is the only credential there is. In that mode every request must
also name a host the plane is known by (`refuse_unknown_host`), because a page
that rebinds its own name to `127.0.0.1` is, to the browser, same-origin with
the plane: it chooses `Origin` and `Host` alike.
"""

from urllib.parse import urlsplit
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
from app.auth import AuthService
from app.config import settings
from app.db.engine import session_factory
from app.db.session import provide_session
from app.core.exceptions import Forbidden
from app.core.exceptions import PlaneError
from app.core.exceptions import NotAuthorized


API_PREFIX = "/v1"
"""Everything under this prefix is authenticated unless it is in `PUBLIC_ROUTES`.
Outside it live `/health`, the OpenAPI schema and the console's static files."""

PUBLIC_ROUTES = frozenset({"/v1/auth/login", "/v1/auth/refresh", "/v1/auth/logout"})
"""The API routes an anonymous caller may reach. Each one checks the credential it
is handed itself (a password, a tenant token, a refresh token) because a
session has to come from somewhere."""

TENANT_SURFACE = "/v1/serving"
"""The only path prefix a tenant principal may reach, and it is read-only.

An allowlist of one, enforced here, not as a filter inside each module,
because those are two different promises. A per-module filter says every module
remembered to scope its query; this says a tenant principal never reaches a
module that was not written for it, including the next one somebody adds. The
cost is that widening the tenant surface is a deliberate edit to this constant,
which is the point.

The prefix is matched at a segment boundary, not as a string. `/v1/serving-quotas`
begins with `/v1/serving` and is a different surface entirely: the commercial
terms of every endpoint on the fleet.

Read-only is enforced by the method, not by the routes behind it. Every
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


UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class UnsupportedMediaType(PlaneError):
    """A state-changing request whose body is not JSON. Every body this API reads
    is JSON, and a form-encoded or plain-text body is what a cross-site form sends."""

    code = "unsupported_media_type"
    status = 415


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


def _hostname(netloc: str) -> str:
    """The host name in a `Host` header or an origin's netloc, port and brackets removed."""
    try:
        return (urlsplit(f"//{netloc}").hostname or "").lower()
    except ValueError:
        return ""


def known_hosts() -> frozenset[str]:
    """The names the plane answers to while admin auth is off: loopback, the
    hosts in `ALLOWED_ORIGINS`, and `PUBLIC_HOST`."""
    names = set(LOOPBACK_HOSTS)
    for origin in settings.ALLOWED_ORIGINS.split(","):
        names.add(_hostname(urlsplit(origin.strip()).netloc))
    names.add(_hostname(settings.PUBLIC_HOST.strip()))
    names.discard("")
    return frozenset(names)


def refuse_unknown_host(connection: ASGIConnection) -> None:
    """Refuses, while admin auth is off, a request addressed to a name the plane is
    not known by. That is DNS rebinding: a page on `evil.example` re-resolves its
    own name to `127.0.0.1`, and the browser then sends it the plane's responses
    and lets it write, because to the browser it is one origin. Its `Host` still
    says `evil.example`, and nothing else in the request can tell.

    With admin auth on there is no ambient credential for such a page to borrow,
    so the check is skipped and a deployment need not list every name it serves on.
    """
    if AuthManager.admin_auth_enabled():
        return
    host = connection.headers.get("Host", "")
    if _hostname(host) not in known_hosts():
        raise Forbidden(
            "unknown host; with admin auth off the plane answers only to loopback, "
            "PUBLIC_HOST and the hosts in ALLOWED_ORIGINS",
            {"host": host},
        )


def _origin_allowed(origin: str, host: str) -> bool:
    if origin in {o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()}:
        return True
    netloc = urlsplit(origin).netloc
    return bool(netloc) and netloc == host


def refuse_cross_site(connection: ASGIConnection) -> None:
    """Refuses a state-changing request a browser sent from another site.

    A browser always sends `Origin` on a cross-origin write; a request without
    one comes from something that is not a browser (`hgr`, the gateway, `curl`)
    and carries its own credential. With one, it must name an allowed origin or
    the origin the plane itself is served from. Any body must be JSON, because
    the bodies a cross-site form can send without a preflight are not.
    """
    if connection.scope.get("method", "GET") not in UNSAFE_METHODS:
        return
    origin = connection.headers.get("Origin")
    if origin is not None and not _origin_allowed(origin, connection.headers.get("Host", "")):
        raise Forbidden("cross-origin request refused", {"origin": origin})
    has_body = connection.headers.get("Transfer-Encoding") or connection.headers.get("Content-Length", "0") != "0"
    if not has_body:
        return
    media_type = connection.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise UnsupportedMediaType("request bodies must be application/json")


async def authenticate(connection: ASGIConnection, _: BaseRouteHandler) -> None:
    """App-wide guard: refuses rebound hosts and cross-site writes, then resolves
    and authorizes the caller of every non-public route.

    A session token is also checked against its session, one indexed read, so a
    logout, a revoked tenant token or a disabled tenant takes effect on the next
    request, not when the access token expires. A service credential and
    the local admin bypass carry no session and need no read.

    The principal is left on `connection.state` for `request_context` to hand to
    the handler, so it is resolved once per request.
    """
    refuse_unknown_host(connection)
    refuse_cross_site(connection)
    route = route_of(connection)
    if is_public(route):
        return
    principal = AuthManager.resolve_principal(AuthManager.bearer_token(connection.headers.get("Authorization")))
    authorize(principal, connection.scope.get("method", "GET"), route)
    if principal.session_id is not None and not await session_is_live(principal):
        raise NotAuthorized("this session has ended; sign in again")
    connection.state.principal = principal


async def session_is_live(principal: Principal) -> bool:
    """Whether the session behind a session token is still open."""
    async with session_factory()() as db:
        return await AuthService(db).session_is_live(principal)


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
