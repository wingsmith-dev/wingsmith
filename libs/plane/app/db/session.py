from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import session_factory


async def provide_session() -> AsyncGenerator[AsyncSession, None]:
    """Yields a request-scoped session and rolls back anything left open."""
    async with session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
