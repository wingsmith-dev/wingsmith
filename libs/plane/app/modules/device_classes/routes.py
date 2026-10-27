"""Device classes: the GPU model catalog.

`GET` list and `GET` one, and nothing else. A class is neither declared nor
observed: it is a definition the plane and the operator share, versioned in
`libs/packages/contract/devices.json`, so there is no `POST /v1/device-classes`.
Adding hardware is a change to the contract table and a release of both sides,
not an API call that would leave the two halves naming cards differently.

The path is `/v1/device-classes` per `libs/packages/contract/API.md` §6; the Python package is
underscored because a hyphen is not an identifier.
"""

from litestar import Response
from litestar import Controller
from litestar import get
from litestar.di import Provide

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import device_class_svc_generator
from .schemas import DeviceClassSchema
from .services import DeviceClassService


class DeviceClassRoutes(Controller):
    tags = ["Device Classes"]
    path = "/device-classes"
    dependencies = {"svc": Provide(device_class_svc_generator)}

    @get()
    async def list_device_classes(
        self,
        context: RequestContext,
        svc: DeviceClassService,
        cluster: str | None = None,
        architecture: str | None = None,
        present: bool | None = None,
    ) -> Response[ListResponse[DeviceClassSchema]]:
        return Response(await svc.list(cluster=cluster, architecture=architecture, present=present))

    @get("/{name:str}")
    async def get_device_class(
        self,
        context: RequestContext,
        svc: DeviceClassService,
        name: str,
        cluster: str | None = None,
    ) -> Response[DeviceClassSchema]:
        return Response(await svc.get(name, cluster=cluster))
