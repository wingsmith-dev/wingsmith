from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import DeviceClassService


async def device_class_svc_generator(context: RequestContext) -> AsyncGenerator[DeviceClassService, None]:
    yield DeviceClassService(db=context.db)
