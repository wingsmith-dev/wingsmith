from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import DeviceService


async def device_svc_generator(context: RequestContext) -> AsyncGenerator[DeviceService, None]:
    yield DeviceService(db=context.db)
