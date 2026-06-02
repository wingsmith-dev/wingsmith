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
    slug: str
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
    """`slug` is the model id clients send. Empty takes the slug of the weights behind the first
    target, which is what makes an endpoint over one model answer to that model without anybody
    typing its name a second time. Give one only to publish an alias."""

    name: str
    project: str
    slug: str = ""
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
    `slug` is what clients actually send and it *is* patchable, which is the
    point of the split: the public model id can be corrected without moving the
    handle everything else is holding. The project owns quota and priority, so moving an endpoint between
    projects is a different endpoint. Supplying `targets` replaces the whole set:
    a target is identified by its deployment, and a patch that merged element-wise
    could not express removing one. `failover_order` orders that set, so it is
    only accepted alongside it — an order over targets that are about to be
    replaced is an order over nothing.
    """

    slug: str | UnsetType = UNSET
    visibility: str | UnsetType = UNSET
    routing_strategy: str | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
    targets: list[EndpointTargetRequest] | UnsetType = UNSET
    failover_order: list[str] | UnsetType = UNSET
    labels: dict | UnsetType = UNSET
    surface: dict | UnsetType = UNSET


class ParameterRuleSchema(Struct):
    """What one request parameter may be set to.

    `enum` pins a parameter to an exact set of values; `min`/`max` bound a range.
    Both may be given, and an absent field constrains nothing.

    A rule is data and never an expression. This is read on every request, and it
    is also the thing somebody reads across a negotiating table — neither survives
    a small language.
    """

    enum: list | None = None
    min: float | None = None
    max: float | None = None


class ParameterPolicySchema(Struct):
    """The request terms this endpoint enforces, on top of whatever the engine does.

    It exists because "consistent with the original model provider" is a term of
    sale, and a provider is sometimes stricter than the engine we run: Moonshot
    answers 400 to any `temperature` but 1, where an ordinary OpenAI-compatible
    server accepts the whole range. Conforming means refusing a request the engine
    would happily serve.

    Absent is the default and means no opinion — every endpoint that has not sold
    a consistency term behaves exactly as it did before this existed. That default
    is the whole design: the policy is per endpoint precisely so that conforming
    on one model never narrows another.

    `version` is what a reader that does not understand a future rule reports
    rather than guesses. The gateway enforces what it knows and counts what it
    does not, because a policy it cannot fully read is a conformance gap somebody
    must see — never a reason to drop traffic.
    """

    version: int = 1
    parameters: dict[str, ParameterRuleSchema] = {}
    deny: list[str] = []


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
    endpoint: one endpoint can front two clusters running different
    configurations, so there is no single answer to hang one level up. The
    gateway rewrites the inbound `model` to it before forwarding. It is usually
    *not* the endpoint's name — a workload the console derived is named for its
    configuration (`llama-3-3-70b-instruct-fp8-tp2-32k`), which is an internal
    detail of the fit that no client should ever have to type or notice changing.
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

    `policy` rides here for the same reasons and is null far more often — it
    is set only on an endpoint that has sold a consistency term. Null means the
    gateway forwards what it is given, which is what every other endpoint wants.
    """

    id: str
    name: str
    visibility: str
    routing_strategy: str
    upstreams: list[ResolvedUpstreamSchema]
    failover_order: list[str]
    limits: ResolvedLimitsSchema | None
    policy: ParameterPolicySchema | None
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
