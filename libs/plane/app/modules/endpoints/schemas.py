import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType
from app.modules.quotas.schemas import ResolvedLimitsSchema


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
    served_model_name: str | None


class EndpointSchema(Struct):
    """The stable public name clients call. Global and plane-only.

    There is no cluster on this object at any depth. An endpoint spans clusters
    by construction — that is what the weighted target set and the failover order
    are for — and it never syncs to one, so a cluster reference here would be a
    field with no writer and no meaning.
    """

    id: str
    name: str
    model_slug: str | None
    resolved_model_slug: str
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
    """One deployment behind one endpoint.

    `served_model_name` corrects what the engine answers to when the plane derived it wrong.
    Null, the default, means the deployment's own answer stands.
    """

    deployment: str
    weight: int = 100
    role: str = "primary"
    enabled: bool = True
    served_model_name: str | None = None


class EndpointCreateRequest(Struct):
    name: str
    project: str
    model_slug: str | None = None
    display_name: str | None = None
    visibility: str = "private"
    routing_strategy: str = "weighted"
    enabled: bool = True
    targets: list[EndpointTargetRequest] = []
    failover_order: list[str] = []
    labels: dict = {}


class EndpointUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `name` and `project` are absent by design. The name is the internal handle —
    it is in URLs, in metrics labels and in every link the console holds — and an
    endpoint exists so that deployments can be replaced without anyone noticing.
    `model_slug` is what clients actually send and it *is* patchable, which is the
    point of the split: the public model id can be corrected without moving the
    handle everything else is holding. The project owns quota and priority, so moving an endpoint between
    projects is a different endpoint. Supplying `targets` replaces the whole set:
    a target is identified by its deployment, and a patch that merged element-wise
    could not express removing one. `failover_order` orders that set, so it is
    only accepted alongside it — an order over targets that are about to be
    replaced is an order over nothing.
    """

    model_slug: str | None | UnsetType = UNSET
    display_name: str | None | UnsetType = UNSET
    visibility: str | UnsetType = UNSET
    routing_strategy: str | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
    targets: list[EndpointTargetRequest] | UnsetType = UNSET
    failover_order: list[str] | UnsetType = UNSET
    labels: dict | UnsetType = UNSET


class ResolvedUpstreamSchema(Struct):
    """One dialable address behind an endpoint, with everything needed to dial it.

    This is the one place a cluster appears next to an endpoint, and it is not a
    contradiction: the endpoint still holds no cluster, and this object is not
    the endpoint. It is the resolution of one, derived on read the way an
    `EndpointSlice` is derived from a `Service` — flattened here so that a
    gateway can hold the whole table in memory rather than re-walking four joins
    per request.

    `admission_phase` is passed through rather than filtered on. Whether a
    deployment is running is observed, it can lag what the cluster is actually
    doing, and a plane that silently drops targets on it would turn a stale
    observation into an outage. The gateway is closer to the truth and decides.

    `served_model_name` is the name the engine behind this one upstream answers
    to, and it is here because it is a fact about the upstream and not about the
    endpoint: one endpoint can front two clusters running different recipes, so
    there is no single answer to hang one level up. The gateway rewrites the
    inbound `model` to it before forwarding. It is usually *not* the endpoint's
    name — a recipe the console derived is named for its configuration
    (`llama-3-3-70b-instruct-fp8-tp2-32k`), which is an internal detail of the
    fit that no client should ever have to type or notice changing.
    """

    deployment_id: str
    deployment_name: str
    admission_phase: str
    cluster_id: str
    cluster_name: str
    url: str
    served_model_name: str
    credential_id: str | None
    weight: int
    role: str


class ResolvedEndpointSchema(Struct):
    """An endpoint with its targets resolved into addresses.

    An endpoint with no upstreams is returned, not omitted. The two states a
    caller must be able to tell apart are "no such model" and "this model has
    nowhere to go right now", and dropping the row collapses them into a 404.

    `limits` is the endpoint's serving quota, flattened onto the payload the
    gateway already polls rather than given a second poll of its own. It is null
    when nothing has been sold on this endpoint, and null means unlimited — never
    zeros. The commitment rides here because it is small, changes on the order of
    weeks and is needed on every request, which is the shape of everything else
    already on this object.
    """

    id: str
    name: str
    visibility: str
    routing_strategy: str
    upstreams: list[ResolvedUpstreamSchema]
    failover_order: list[str]
    limits: ResolvedLimitsSchema | None
    revision: str


class ResolvedTableSchema(Struct):
    """Every enabled endpoint, resolved. The whole routing table in one read.

    `revision` covers everything the table was built from — endpoints, their
    targets, the deployments behind them and the serving addresses of the
    clusters those sit on — so a caller that polls can send it back as
    `If-None-Match` and get a `304` for as long as none of the four have moved.
    """

    revision: str
    endpoints: list[ResolvedEndpointSchema]
