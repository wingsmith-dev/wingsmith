import datetime as dt

from typing import Any

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


class ParameterRuleSchema(Struct):
    """What one request parameter may be set to, in the policy language v1 spoke.

    `enum` pins a parameter to an exact set of values; `min`/`max` bound a range.
    Both may be given, and an absent field constrains nothing.

    Kept only to read what is already stored. Nothing is written in this shape
    any more — `endpoint_policy` converts it on the way out — and nothing new
    should be added to it.
    """

    enum: list | None = None
    min: float | None = None
    max: float | None = None


class ParameterPolicySchema(Struct):
    """The v1 policy: a map of parameter names to constraints, plus denials.

    Superseded by `PolicySchema` and kept for exactly one reason: it is the shape
    the stored surfaces are in, and a migration that cannot read what is already
    written is not a migration. Read, converted, never written.

    It could say what a parameter may be set to and nothing else. It could not
    say *where* a term applied — so a rule measured against chat completions was
    enforced against `/v1/embeddings`, `/v1/messages` and `/v1/responses` too —
    and it could not say what an absent field should become.
    """

    version: int = 1
    parameters: dict[str, ParameterRuleSchema] = {}
    deny: list[str] = []


class RuleSchema(Struct):
    """One term this endpoint sells: where it applies, what it is about, what it says.

    **Scope.** `route` is the request path the term was measured against,
    `stream` the transport mode, and `when_present` another JSON path the request
    must carry for this rule to apply at all. All three are optional and an
    omitted one matches everything, so an unscoped rule behaves exactly as a v1
    policy did. A term is a claim about one protocol's request shape, and it is a
    claim somebody measured — writing the path down is what stops it being
    applied to three other protocols nobody measured.

    `when_present` is a scope and not a term, which is why it sits here and not
    beside `drop`. "Does this rule look at this request at all" is one question
    however it is narrowed, and keeping the condition in the scope block means it
    composes with every term rather than with one of them: the same field that
    says "drop `reasoning_effort` when `thinking.effort` is set" would say
    "`temperature` must be 1 while thinking is on", with no second way to spell
    `when`.

    **Subject.** Exactly one of `key` and `predicate`. A `key` is a JSON path
    into the request body, dotted for nesting (`thinking.type`). A `predicate`
    names a rule the gateway implements in Go, out of the same registry the v1
    `deny` list named. The two are separate namespaces and never fall back to one
    another: a key is read out of the body, a predicate is looked up in a map.

    **Terms.** `enum`, `min` and `max` say what the value may be; `deny` refuses
    the request outright — whenever the key is sent at all, or whenever the
    predicate fires. `default`, `drop` and `value_from` are the terms that write
    instead of refusing, and none of them ever fails a request: `default` says what
    to put in the request when the key is absent and its parent object is not,
    `drop` takes the key back out, and `value_from` copies another path's value
    onto the key. `drop` is for a field the provider accepts and ignores —
    denying one of those would 400 a request the vendor serves, and leaving it in
    place lets an engine honour a field the provider does not, which is a wrong
    answer rather than an error and the harder of the two to notice.

    `value_from` is for the case `drop` looks like it covers and does not: a knob
    the provider spells one way and the engine behind it spells another. Removing
    the engine's spelling makes it ignored, which is only half of "the provider's
    field wins" — the other half is that the provider's field is *read*, and if
    nothing downstream reads it, the removal just leaves the engine on its own
    default. `{key: "reasoning_effort", value_from: "thinking.effort"}` says both
    halves at once: the vendor's value is carried onto the field the engine reads,
    so it takes effect, and the caller's own `reasoning_effort` is ignored because
    it has been overwritten. It overwrites where `default` defers, because the
    value being replaced is exactly the one the provider ignores, and it writes
    nothing at all when the source path is not in the request.

    Every rule whose scope matches applies and the request must satisfy all of
    them. There is no precedence and the order of the list is never read: a term
    is something that was sold, and a second term does not cancel the first. Two
    that cannot both hold are an authoring mistake and are refused here, where
    somebody is writing the policy, rather than on a request already in flight.

    A rule is data and never an expression. This is read on every request, and it
    is also the thing somebody reads across a negotiating table — neither
    survives a small language.
    """

    route: str = ""
    stream: bool | None = None
    when_present: str = ""
    key: str = ""
    predicate: str = ""
    enum: list | None = None
    min: float | None = None
    max: float | None = None
    deny: bool = False
    drop: bool = False
    default: Any = None
    value_from: str = ""


class PolicySchema(Struct):
    """The request terms this endpoint enforces, on top of whatever the engine does.

    It exists because "consistent with the original model provider" is a term of
    sale, and a provider is sometimes stricter than the engine we run: Moonshot
    answers 400 to any `temperature` but 1, where an ordinary OpenAI-compatible
    server accepts the whole range. Conforming means refusing a request the engine
    would happily serve — and sometimes it means the opposite, filling in a field
    the provider treats as having a default so that the engine reads what the
    provider's own frontend would have.

    Absent is the default and means no opinion — every endpoint that has not sold
    a consistency term behaves exactly as it did before this existed. That default
    is the whole design: the policy is per endpoint precisely so that conforming
    on one model never narrows another.

    `version` is what a reader that does not understand a future rule reports
    rather than guesses. The gateway enforces what it knows and counts what it
    does not, because a policy it cannot fully read is a conformance gap somebody
    must see — never a reason to drop traffic. 2 is this shape; a v1 policy is
    converted on read and reaches the gateway as this one.
    """

    version: int = 2
    rules: list[RuleSchema] = []


class EndpointSurfaceSchema(Struct):
    """The terms on which the served API is exposed, as they are read back.

    Typed rather than the bare dict it is stored as, because half of it the
    gateway acts on and the other half is a disclosure somebody repeats to a
    customer — and a field that only exists when it was written cannot be drawn
    either way. Every field is nullable or empty by default and absent means the
    gateway's own behaviour, never a claim.

    `policy` is converted on the way out by `endpoint_policy`, so a surface
    stored in the v1 language reads back in the one language the gateway speaks.
    """

    protocols: list[str] = []
    auth_mode: str | None = None
    error_shape: str | None = None
    streaming_transport: str | None = None
    output_length_params: list[str] = []
    cors_enabled: bool | None = None
    max_request_bytes: int | None = None
    request_timeout_seconds: int | None = None
    policy: PolicySchema | None = None


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
    surface: EndpointSurfaceSchema
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
    policy: PolicySchema | None
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
