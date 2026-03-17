from collections.abc import AsyncGenerator

from litestar import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.core.auth import bearer_token
from app.core.auth import resolve_principal
from app.db.session import provide_session


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


async def request_context(request: Request) -> AsyncGenerator[RequestContext, None]:
    """Litestar dependency providing the caller identity and a db session."""
    principal = resolve_principal(bearer_token(request.headers.get("Authorization")))
    async for session in provide_session():
        yield RequestContext(principal=principal, db=session)
