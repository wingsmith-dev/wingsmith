from collections.abc import AsyncGenerator

from app.db.session import provide_session

from .service import AuthService


async def auth_svc_generator() -> AsyncGenerator[AuthService, None]:
    async for session in provide_session():
        yield AuthService(db=session)
