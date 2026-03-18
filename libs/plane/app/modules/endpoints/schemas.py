import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class EndpointTargetSchema(Struct):
    """One deployment behind one endpoint, with a traffic weight.

    The deployment is named by id and nothing about the cluster it sits on is
    repeated here. Which cluster a target runs in is a property of the deployment
    and is answered by reading it; copying it onto the endpoint would give a
    global object a per-cluster field and put the split back where it started.
    """

    deployment_id: str
    weight: int
    role: str
    enabled: bool


class EndpointSchema(Struct):
    """The stable public name clients call. Global and plane-only.

    There is no cluster on this object at any depth. An endpoint spans clusters
    by construction — that is what the weighted target set and the failover order
    are for — and it never syncs to one, so a cluster reference here would be a
    field with no writer and no meaning.
    """

    id: str
    name: str
    display_name: str | None
    project_id: str
    visibility: str
    routing_strategy: str
    enabled: bool
    targets: list[EndpointTargetSchema]
    failover_order: list[str]
    labels: dict
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str


class EndpointTargetRequest(Struct):
    deployment: str
    weight: int = 100
    role: str = "primary"
    enabled: bool = True


class EndpointCreateRequest(Struct):
    name: str
    project: str
    display_name: str | None = None
    visibility: str = "private"
    routing_strategy: str = "weighted"
    enabled: bool = True
    targets: list[EndpointTargetRequest] = []
    failover_order: list[str] = []
    labels: dict = {}


class EndpointUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `name` and `project` are absent by design. The name is the contract clients
    hold — an endpoint exists so that deployments can be replaced without anyone
    noticing, and renaming it would spend that on the one object that must not
    move. The project owns quota and priority, so moving an endpoint between
    projects is a different endpoint. Supplying `targets` replaces the whole set:
    a target is identified by its deployment, and a patch that merged element-wise
    could not express removing one. `failover_order` orders that set, so it is
    only accepted alongside it — an order over targets that are about to be
    replaced is an order over nothing.
    """

    display_name: str | None | UnsetType = UNSET
    visibility: str | UnsetType = UNSET
    routing_strategy: str | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
    targets: list[EndpointTargetRequest] | UnsetType = UNSET
    failover_order: list[str] | UnsetType = UNSET
    labels: dict | UnsetType = UNSET
