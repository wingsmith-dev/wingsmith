"""Devices: observed, read-only.

`GET` list and `GET` one. There is no `POST /v1/devices` — a device is
discovered, not created, and writing to it is a category error rather than a
validation failure. Device reset goes through `POST /v1/operations`.

The `class` filter is spelled `class` on the wire and `device_class` in Python,
because the wire name is the object model's and the Python name has to avoid the
keyword.
"""

from litestar import Response
from litestar import Controller
from litestar import get
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import device_svc_generator
from .schemas import DeviceSchema
from .services import DeviceService


class DeviceRoutes(Controller):
    tags = ["Devices"]
    path = "/devices"
    dependencies = {"svc": Provide(device_svc_generator)}

    @get()
    async def list_devices(
        self,
        context: RequestContext,
        svc: DeviceService,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        node: str | None = None,
        pool: str | None = None,
        device_class: str | None = Parameter(query="class", default=None, required=False),
        health: str | None = None,
        allocatable: bool | None = None,
        topology_domain: str | None = None,
    ) -> Response[ListResponse[DeviceSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            cluster=cluster,
            node=node,
            pool=pool,
            device_class=device_class,
            health=health,
            allocatable=allocatable,
            topology_domain=topology_domain,
        )
        return Response(result)

    @get("/{ref:str}")
    async def get_device(self, context: RequestContext, svc: DeviceService, ref: str) -> Response[DeviceSchema]:
        return Response(await svc.get(ref))
