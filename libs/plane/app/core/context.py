from collections.abc import AsyncGenerator

from litestar import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.auth import AuthManager
from app.db.session import provide_session
from app.core.exceptions import NotAuthorized


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


class RequestContext[T]:
    """What every handler receives: who is calling, and the open session."""

    __slots__ = ("_svc", "db", "principal")

    def __init__(self, principal: Principal, db: AsyncSession, svc: T | None = None) -> None:
        self.principal = principal
        self.db = db
        self._svc = svc

    @property
    def svc(self) -> T:
        if self._svc is None:
            raise ValueError("Service is not set")
        return self._svc

    def set_svc(self, svc: T) -> None:
        self._svc = svc


def _tenant_may(request: Request) -> bool:
    """Whether a tenant principal may make this request at all."""
    path = request.scope["path"]
    inside = path == TENANT_SURFACE or path.startswith(f"{TENANT_SURFACE}/")
    return request.method == "GET" and inside


async def request_context(request: Request) -> AsyncGenerator[RequestContext, None]:
    """Litestar dependency providing the caller identity and a db session."""
    principal = AuthManager.resolve_principal(AuthManager.bearer_token(request.headers.get("Authorization")))
    if principal.kind == "tenant" and not _tenant_may(request):
        raise NotAuthorized("this credential is scoped to its own serving view")
    async for session in provide_session():
        yield RequestContext(principal=principal, db=session)
