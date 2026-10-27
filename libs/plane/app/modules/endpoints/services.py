from __future__ import annotations

import hashlib
import builtins
import datetime as dt

from uuid import UUID

from msgspec import UNSET
from msgspec import ValidationError
from msgspec import convert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import ENDPOINT
from app.core.ids import new_uid
from app.db.models import Endpoint
from app.db.models import EndpointAlias
from app.db.models import EndpointTarget
from app.core.schemas import ListResponse
from app.core.serving import TEXT
from app.core.serving import served_model
from app.core.serving import output_modality
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.modules.quotas.services import declared_limits

from .schemas import RuleSchema
from .schemas import PolicySchema
from .schemas import EndpointSchema
from .schemas import ResolvedTableSchema
from .schemas import EndpointTargetSchema
from .schemas import EndpointCreateRequest
from .schemas import EndpointSurfaceSchema
from .schemas import EndpointTargetRequest
from .schemas import EndpointUpdateRequest
from .schemas import ParameterPolicySchema
from .schemas import ResolvedEndpointSchema
from .schemas import ResolvedUpstreamSchema
from .repository import EndpointRepository


VISIBILITIES = frozenset({"private", "public"})
STRATEGIES = frozenset({"weighted", "failover", "latency"})
ROLES = frozenset({"primary", "canary", "fallback"})
SERVING_PHASES = frozenset({"active"})
"""The cluster phases that may carry inference traffic.

`pending` has never been enrolled, `draining` is being intentionally emptied and
`disabled` is out of service: sending requests to any of them is the plane
contradicting the intent somebody declared. Stated here, not imported from
the clusters module for the same reason `PLACEABLE_PHASES` is stated in
`placement`: which phases serve is this reader's rule, not the vocabulary's."""


def check_targets(targets: list[EndpointTargetRequest]) -> None:
    """Rejects a target set that could not route.

    Two deployments cannot appear twice (the table's unique constraint says so
    and an integrity error at commit time names neither of them), and a set of
    enabled targets whose weights sum to zero is an endpoint that accepts traffic
    and sends it nowhere, which reads as an outage, not as configuration.
    """
    seen: set[str] = set()
    live = 0
    for target in targets:
        if target.weight < 0:
            raise Invalid("a target weight cannot be negative", {"deployment": target.deployment})
        if target.role not in ROLES:
            raise Invalid(f"unknown role {target.role!r}", {"allowed": sorted(ROLES)})
        if target.deployment in seen:
            raise Invalid(f"deployment {target.deployment!r} appears twice", {"deployment": target.deployment})
        seen.add(target.deployment)
        if target.enabled:
            live += target.weight
    if targets and not live:
        raise Invalid("no enabled target carries any weight")


def public_model(endpoint: Endpoint) -> str:
    """The model id clients send for this endpoint.

    A read of one column now. It used to be a rule (the slug, else the name) with a
    `resolved_model_slug` computed on the way out so that an operator could see which of the two
    had won. That is what a nullable public identifier costs: a second field to report the answer,
    and two call sites that have to agree or the console documents a model id that does not route.

    Kept as a function because the routing key comes from here and not from `Endpoint.name`, and
    that is the whole reason a public id can be corrected without touching a cluster: the gateway
    is told what to route on every time it polls (`next[route.Name]` in `routing/table.go`).
    """
    return str(endpoint.slug)


def alias_names(endpoint: Endpoint) -> list[str]:
    """The other public ids this endpoint answers to, sorted.

    Sorted, not left in insertion order, because the set is what matters and the order is
    not a fact anybody declared: it also goes into the resolved table's revision, which must not
    move because two rows came back the other way round.
    """
    return sorted(alias.alias for alias in endpoint.aliases)


def normalise_aliases(values: list[str]) -> list[str]:
    """Trims, lowercases and deduplicates a declared alias set.

    Lowercased here and matched case-insensitively at the gateway, so `Kimi-K3` and `kimi-k3` are
    one route instead of two somebody has to keep in step. An empty string is refused, not
    dropped: it is a route on the empty string, and a caller who typed one meant something.

    Deduplication is not a collision. Two spellings of one alias in one request say the same thing
    twice, and the set they describe is unambiguous.
    """
    cleaned: list[str] = []
    for value in values:
        alias = str(value).strip().lower()
        if not alias:
            raise Invalid("an alias cannot be empty")
        if alias not in cleaned:
            cleaned.append(alias)
    return cleaned


def check_shape(visibility: str, routing_strategy: str) -> None:
    if visibility not in VISIBILITIES:
        raise Invalid(f"unknown visibility {visibility!r}", {"allowed": sorted(VISIBILITIES)})
    if routing_strategy not in STRATEGIES:
        raise Invalid(f"unknown routing strategy {routing_strategy!r}", {"allowed": sorted(STRATEGIES)})


POLICY_VERSION = 2
"""The policy language the plane emits. A stored v1 policy is converted to it on read."""


def endpoint_policy(surface: dict | None) -> PolicySchema | None:
    """The endpoint's request terms, or None when it has not sold any.

    Read on every table build and validated on every write, both through this one
    function, so a policy that would not survive the gateway cannot be stored.

    A policy stored in the v1 shape is converted here, not rewritten in
    the database. Converting on read is what makes this a migration nobody has to
    schedule: there is no batch job to run, no window where half the rows are in
    one language, and no way for a policy written before this change to reach a
    gateway in a shape it stopped reading. The cost is this function carrying two
    input shapes, which is one function against every stored surface, forever
    against the length of one deploy.
    """
    raw = (surface or {}).get("policy")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise Invalid("surface.policy must be an object", {"field": "surface.policy"})
    if _is_legacy(raw):
        return _migrate(_as(raw, ParameterPolicySchema))
    return _as(raw, PolicySchema)


def _as(raw: dict, schema):
    """Convert, reporting a policy the gateway could not read as a bad request."""
    try:
        return convert(raw, schema)
    except ValidationError as exc:
        raise Invalid(f"surface.policy is not a policy: {exc}", {"field": "surface.policy"}) from exc


def _is_legacy(raw: dict) -> bool:
    """Which of the two languages this policy is written in.

    Decided on the keys, not on `version` alone, because `version` is
    absent far more often than it is wrong and the two shapes have no key in
    common: `forbid_unknown_fields` then catches every mixture of them, which is
    what turns a typo into a 400 instead of a rule that silently enforces nothing.
    """
    if "rules" in raw:
        return False
    return "parameters" in raw or "deny" in raw or raw.get("version") == 1


def _migrate(legacy: ParameterPolicySchema) -> PolicySchema:
    """A v1 policy, read as the language that replaced it.

    Every rule comes out unscoped, which is what it meant: v1 had no route, so a
    stored parameter pin applied to every path the endpoint answers on and it
    still does. Narrowing them to `/v1/chat/completions` here would be the right
    guess for every policy in the fleet today and still the wrong thing to do: a
    migration that quietly stops enforcing a sold term on two protocols is the
    failure this whole object exists to prevent, and the operator who wrote the
    term is the one who gets to say where it applies. Scoping is an edit somebody
    makes, one endpoint at a time, and it is visible in the surface when they do.

    Parameters are emitted in name order so that a converted policy is the same
    bytes every time it is read, and the gateway's `If-None-Match` still answers
    304 on a table nothing has moved in.

    One edge is accepted, not handled: a v1 parameter name containing a
    dot was a top-level key spelled with a dot in it, and it converts into a
    two-segment path. No field on any wire this gateway fronts is spelled that
    way, so the case is theoretical: it is written down here, not guarded
    against, because a guard would be a branch nothing ever takes.
    """
    rules = [
        RuleSchema(key=name, enum=rule.enum, min=rule.min, max=rule.max)
        for name, rule in sorted(legacy.parameters.items())
    ]
    rules.extend(RuleSchema(predicate=name, deny=True) for name in legacy.deny)
    return PolicySchema(version=POLICY_VERSION, rules=rules)


SURFACE_CHOICES = {
    "protocols": frozenset({"openai.chat", "openai.completions", "openai.responses", "openai.embeddings"}),
    "auth_mode": frozenset({"none", "api_key", "mtls"}),
    "error_shape": frozenset({"passthrough"}),
    "streaming_transport": frozenset({"sse", "none"}),
    "output_length_params": frozenset({"max_tokens", "max_completion_tokens", "max_output_tokens"}),
}
"""The values each surface field may take, matching the console's own constants.

Checked here because the surface is patchable and half of it the gateway acts on.
An unchecked `streaming_transport` is a term an operator reads off a page and
repeats to a customer, and nothing downstream would have caught it.
"""

SURFACE_LISTS = frozenset({"protocols", "output_length_params"})
SURFACE_SCALARS = {"auth_mode", "error_shape", "streaming_transport"}


def endpoint_surface(surface: dict | None) -> EndpointSurfaceSchema:
    """The stored surface, read back in the shape the console and the CLI draw.

    Only the declared fields are carried. The surface is validated key by key on
    write, so anything else in the column is a key nothing reads, and reporting
    one would give an operator a term to repeat that no reader honours.
    """
    raw = surface or {}
    return EndpointSurfaceSchema(
        protocols=list(raw.get("protocols") or []),
        auth_mode=raw.get("auth_mode"),
        error_shape=raw.get("error_shape"),
        streaming_transport=raw.get("streaming_transport"),
        output_length_params=list(raw.get("output_length_params") or []),
        cors_enabled=raw.get("cors_enabled"),
        max_request_bytes=raw.get("max_request_bytes"),
        request_timeout_seconds=raw.get("request_timeout_seconds"),
        policy=endpoint_policy(raw),
    )


def check_surface(surface: dict) -> None:
    """Refuse a surface that is not one the gateway and console can read."""
    if not isinstance(surface, dict):
        raise Invalid("surface must be an object", {"field": "surface"})

    for field in SURFACE_LISTS:
        values = surface.get(field)
        if values is None:
            continue
        if not isinstance(values, list) or any(v not in SURFACE_CHOICES[field] for v in values):
            raise Invalid(
                f"surface.{field} has a value that is not allowed",
                {"field": f"surface.{field}", "allowed": sorted(SURFACE_CHOICES[field])},
            )

    for field in SURFACE_SCALARS:
        value = surface.get(field)
        if value is not None and value not in SURFACE_CHOICES[field]:
            raise Invalid(
                f"surface.{field} {value!r} is not allowed",
                {"field": f"surface.{field}", "allowed": sorted(SURFACE_CHOICES[field])},
            )

    if surface.get("cors_enabled") is not None and not isinstance(surface["cors_enabled"], bool):
        raise Invalid("surface.cors_enabled must be true or false", {"field": "surface.cors_enabled"})

    for field in ("max_request_bytes", "request_timeout_seconds"):
        value = surface.get(field)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise Invalid(f"surface.{field} must be a positive whole number", {"field": f"surface.{field}"})

    check_policy(endpoint_policy(surface))


def check_policy(policy: PolicySchema | None) -> None:
    """Refuse a policy that would not mean, at the gateway, what its author meant.

    Every check here exists because the alternative is a policy that looks like a
    working configuration and enforces nothing: silently not conforming, which
    is the one failure mode a consistency term must not have. A rule that
    constrains nothing, a route that is not a path, a key with an empty segment:
    each of them is a term somebody sold and nobody applies, and none of them
    would produce a symptom anywhere downstream.
    """
    if policy is None:
        return
    if policy.rules and policy.version < POLICY_VERSION:
        raise Invalid(
            f"surface.policy carries rules, so it must declare version {POLICY_VERSION} or above",
            {"field": "surface.policy.version"},
        )

    for index, rule in enumerate(policy.rules):
        _check_rule(rule, f"surface.policy.rules[{index}]")

    _check_one_writer_per_key(
        [rule for rule in policy.rules if rule.default is not None or rule.drop or rule.value_from]
    )


def _check_rule(rule: RuleSchema, where: str) -> None:
    """Refuse one rule that does not say something the gateway can act on.

    The subject is named beside the index, because a policy stored in the v1
    shape is converted before it gets here and its author never wrote an index:
    they wrote a parameter name, and that is what has to appear in the error.
    """
    subject = f"key {rule.key!r}" if rule.key else f"predicate {rule.predicate!r}"
    if bool(rule.key) == bool(rule.predicate):
        raise Invalid(
            f"{where} must name exactly one of key and predicate: a key is a path into the "
            f"request body, a predicate is a rule the gateway implements",
            {"field": where},
        )
    if rule.route and not rule.route.startswith("/"):
        raise Invalid(
            f"{where}.route {rule.route!r} is not a request path: it must start with '/'",
            {"field": f"{where}.route"},
        )
    for field, path in (("key", rule.key), ("when_present", rule.when_present), ("value_from", rule.value_from)):
        if path and not all(segment for segment in path.split(".")):
            raise Invalid(
                f"{where}.{field} {path!r} is not a JSON path: every segment between the dots must be a name",
                {"field": f"{where}.{field}"},
            )
    if rule.when_present and rule.when_present == rule.key:
        # It reads as a condition and is not one: a key is present or it is not,
        # and either way the rule does the same thing. Refused because a term
        # that looks conditional and is unconditional is the kind of policy
        # somebody writes once and reads wrong for a year.
        raise Invalid(
            f"{where}.when_present names its own key {rule.key!r}, which is not a condition",
            {"field": f"{where}.when_present"},
        )

    if rule.value_from and rule.value_from == rule.key:
        # Copying a key onto itself is the same request either way. Refused for
        # the reason a self-naming `when_present` is: a term that looks like it
        # moves something and moves nothing is read wrong for a year.
        raise Invalid(
            f"{where}.value_from names its own key {rule.key!r}, which copies nothing",
            {"field": f"{where}.value_from"},
        )

    values = rule.enum is not None or rule.min is not None or rule.max is not None or rule.default is not None
    if rule.predicate:
        if rule.drop:
            raise Invalid(
                f"{where} names {subject}, which has no key to drop",
                {"field": where},
            )
        if rule.value_from:
            raise Invalid(
                f"{where} names {subject}, which has no key to write into",
                {"field": where},
            )
        # A predicate reads the whole body and answers yes or no. There is no
        # value for a bound to bound or a default to fill in, so a term about one
        # is a term about nothing.
        if values:
            raise Invalid(
                f"{where} names {subject}, which takes deny and nothing else",
                {"field": where},
            )
        if not rule.deny:
            raise Invalid(
                f"{where} names {subject} but does not deny it: it constrains nothing",
                {"field": where},
            )
        return

    if rule.deny and rule.drop:
        # One answers 400 and the other answers 200 with the field taken out.
        # There is no request for which both are what the author meant.
        raise Invalid(
            f"{where} both denies and drops {subject}: a refusal and a silent removal are different answers",
            {"field": where},
        )
    if rule.value_from and (rule.deny or rule.drop):
        # A copy writes the key, a deny refuses it and a drop takes it away.
        # Whichever the author meant, the other two are not it.
        raise Invalid(
            f"{where} both copies into and denies or drops {subject}: a write and a removal are different answers",
            {"field": where},
        )
    if (rule.deny or rule.drop) and values:
        # Deny refuses the key whenever it is sent and drop removes it, so
        # nothing else on the rule can ever be reached, including a default,
        # which would be filling in a field the same rule takes away.
        raise Invalid(
            f"{where} denies or drops {subject}, so enum, min, max and default on it are unreachable",
            {"field": where},
        )
    if rule.value_from and values:
        # The value at the key is about to be replaced by the one carried in, so
        # a bound on what the caller sent is a bound on something nothing reads,
        # and a default beside a copy is two writers for one key.
        raise Invalid(
            f"{where} copies into {subject}, so enum, min, max and default on it are unreachable",
            {"field": where},
        )
    if not rule.deny and not rule.drop and not rule.value_from and not values:
        raise Invalid(
            f"{where} ({subject}) constrains nothing: give enum, min, max, deny, drop, default or value_from",
            {"field": where},
        )
    if rule.min is not None and rule.max is not None and rule.min > rule.max:
        raise Invalid(f"{where} ({subject}) has min above max", {"field": where})


def _check_one_writer_per_key(writers: list[RuleSchema]) -> None:
    """Refuse two rules that could disagree about what one key becomes.

    Resolved here so the gateway never has to. A write happens to a request in
    flight and there is no good answer to "which of these two values" at that
    point: picking one by array order would make the order semantic, and picking
    the most specific scope would make a policy mean something different from
    what each of its rules says. So the conflict is refused where somebody is
    writing it down and can say which they meant.

    Two drops of one key are not a disagreement: both take it out, in either
    order, and two conditions for removing the same field is an ordinary thing to
    want. A default against a default, or a default against a drop, is one. So is
    anything against a `value_from`: a copy replaces the key outright, so a second
    writer beside it (another copy, a default, or a drop) is two answers to
    "what does this key become", which is the question this refuses to leave to
    the request path.
    """
    for first in range(len(writers)):
        for second in range(first + 1, len(writers)):
            left, right = writers[first], writers[second]
            if left.key != right.key or (left.drop and right.drop):
                continue
            if _scopes_overlap(left, right):
                raise Invalid(
                    f"surface.policy declares two writes to {left.key!r} on scopes that overlap",
                    {"field": "surface.policy.rules", "key": left.key},
                )


def _scopes_overlap(left: RuleSchema, right: RuleSchema) -> bool:
    """Whether one request could match both scopes. An omitted term matches everything.

    `when_present` is intentionally not read here. Two rules guarded on different
    paths still both fire on a request that carries both of them, so a differing
    condition narrows nothing for this question: treating it as though it did
    would let exactly the conflict this function exists to refuse through.
    """
    routes = not left.route or not right.route or left.route == right.route
    streams = left.stream is None or right.stream is None or left.stream == right.stream
    return routes and streams


def _revision(endpoint: Endpoint) -> str:
    """The value `If-Match` is checked against, derived from the last write."""
    stamp = endpoint.updated_at or endpoint.created_at
    return str(int(stamp.timestamp() * 1_000_000))


def _stamp(row) -> dt.datetime | None:
    """When a row last moved, for the revision the whole table is versioned by."""
    if row is None:
        return None
    return row.updated_at or row.created_at


def _alias_term(alias: str) -> str:
    """One alias, as it enters the revision's content hash.

    Prefixed, because `_served` hashes a *set* and an alias is very often a string already in it.
    Renaming an endpoint means publishing the engine's own name as the alias (`moonshotai/kimi-k3`
    aliased onto `kimi-k3`), where that is exactly what the artifact's slug already resolves to, so
    folding the bare string in makes the one change that matters invisible. Nothing else moves on
    an alias-only update, so the gateway keeps its `If-None-Match`, gets a `304`, and 404s the name
    a client was just told to use.
    """
    return f"alias\x1e{alias}"


def _served(values: list[str]) -> str:
    """The third revision term: what the engines behind this table answer to.

    A short hash over the serving digests and resolved model names a resolution
    was built from. It is content instead of a timestamp, and that is the whole
    reason it exists: it moves when, and only when, what the gateway has to
    rewrite an inbound `model` to has actually changed.

    It replaces a stamp taken off the recipe row, which was a proxy for the same
    question and wrong in both directions. Renaming a recipe, or editing a field
    that never reached an engine, pushed the revision forward and made every
    gateway re-fetch a table that had not changed. Meanwhile the workload's own
    `served_model_name` was never stamped at all, so declaring one moved nothing
    and a gateway sat behind a `304` rewriting to the name it had before.

    Sorted and deduplicated so the value depends on the set and not on the row
    order the database happened to return. Truncated because this is a change
    detector, not a signature: a collision costs one stale `304` against a table
    that also has two other terms moving under it.
    """
    if not values:
        return "0"
    body = "\x1f".join(sorted(set(values)))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _revision_of(stamps: list[dt.datetime | None], served: list[str]) -> str:
    """The newest stamp a resolution was built from, how many there were, and what they serve.

    An empty table still needs a value a caller can send back as `If-None-Match`,
    and it needs the value to change the moment the table stops being empty,
    which it does, because the first row that appears carries a stamp.

    The second term is what makes the value sensitive to a *deletion*. A create
    stamps a new row and an update fires `onupdate`, so both push the maximum
    forward; a delete only removes a stamp, and unless that stamp happened to be
    the maximum across the whole fleet the value does not move. The gateway would
    then sit behind a `304` holding a route, or a rate limit, that no longer
    exists, and the asymmetry is what makes it dangerous, because create and
    update work and only delete silently does not. The count moves on any
    disappearance; the maximum still catches a simultaneous add and delete,
    because the added row is freshly stamped.

    The third term is `_served`, and it is content, not time. The value
    stays opaque to its readers, which compare it as a string.
    """
    live = [stamp for stamp in stamps if stamp is not None]
    if not live:
        return f"0-0-{_served(served)}"
    return f"{int(max(live).timestamp() * 1_000_000)}-{len(live)}-{_served(served)}"


class EndpointService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = EndpointRepository(db)

    def schema(self, endpoint: Endpoint, project_uid: str, uids: dict[UUID, str]) -> EndpointSchema:
        return EndpointSchema(
            id=endpoint.uid,
            name=endpoint.name,
            slug=endpoint.slug,
            project_id=project_uid,
            visibility=endpoint.visibility,
            routing_strategy=endpoint.routing_strategy,
            enabled=endpoint.enabled,
            aliases=alias_names(endpoint),
            targets=[
                EndpointTargetSchema(
                    deployment_id=uids[t.deployment_id],
                    weight=t.weight,
                    role=t.role,
                    enabled=t.enabled,
                    served_model_name=t.served_model_name,
                )
                for t in endpoint.targets
                if t.deployment_id in uids
            ],
            failover_order=[uids[d] for d in endpoint.failover_order if d in uids],
            surface=endpoint_surface(endpoint.surface),
            labels=endpoint.labels,
            created_at=endpoint.created_at,
            updated_at=endpoint.updated_at,
            revision=_revision(endpoint),
        )

    def _referenced(self, endpoints: builtins.list[Endpoint]) -> builtins.list[UUID]:
        ids: list[UUID] = []
        for endpoint in endpoints:
            ids.extend(t.deployment_id for t in endpoint.targets)
            ids.extend(endpoint.failover_order)
        return ids

    async def _one(self, ref: str, lock: bool = False):
        row = await self.repo.get(ref, lock=lock)
        if row is None:
            raise NotFound(f"no endpoint {ref!r}")
        return row

    async def _render(self, endpoint: Endpoint, project_uid: str) -> EndpointSchema:
        uids = await self.repo.deployment_uids(self._referenced([endpoint]))
        return self.schema(endpoint, project_uid, uids)

    async def get(self, ref: str) -> EndpointSchema:
        row = await self._one(ref)
        return await self._render(row[0], row.project_uid)

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        project: str | None = None,
        artifact: str | None = None,
        workload: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[EndpointSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            project=project,
            artifact=artifact,
            workload=workload,
            search=search,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        uids = await self.repo.deployment_uids(self._referenced([r[0] for r in kept]))
        items = [self.schema(r[0], r.project_uid, uids) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def _resolve(
        self, targets: builtins.list[EndpointTargetRequest], failover: builtins.list[str], tenant_id: UUID | None
    ) -> tuple[builtins.list[EndpointTarget], builtins.list[UUID]]:
        """Turns target and failover references into rows and an ordered id array.

        Targets resolve inside the endpoint's tenant, and a bare deployment name that
        matches deployments in more than one cluster is refused with the candidates,
        not resolved to the oldest of them.

        The failover order is resolved against the target set, not against
        every deployment there is: an endpoint that fails over to something it
        does not route to is a routing table with a hole in it.
        """
        refs = [t.deployment for t in targets]
        matches = await self.repo.deployments_by_ref(refs, tenant_id)
        missing = [ref for ref in refs if ref not in matches]
        if missing:
            raise NotFound("unknown deployment", {"missing": sorted(set(missing))})
        ambiguous = {ref: sorted(d.uid for d in rows) for ref, rows in matches.items() if len(rows) > 1}
        if ambiguous:
            raise Invalid(
                "a deployment name matches deployments in more than one cluster; name the one you mean by id",
                {"ambiguous": ambiguous},
            )
        found = {ref: rows[0] for ref, rows in matches.items()}

        rows = [
            EndpointTarget(
                deployment_id=found[t.deployment].id,
                weight=t.weight,
                role=t.role,
                enabled=t.enabled,
                served_model_name=(t.served_model_name or "").strip() or None,
            )
            for t in targets
        ]

        order: list[UUID] = []
        for ref in failover:
            deployment = found.get(ref)
            if deployment is None:
                raise Invalid(
                    f"failover names {ref!r}, which is not a target of this endpoint",
                    {"deployment": ref},
                )
            if deployment.id in order:
                raise Invalid(f"failover names {ref!r} twice", {"deployment": ref})
            order.append(deployment.id)
        return rows, order

    async def _check_namespace(self, slug: str, aliases: builtins.list[str], endpoint_id: UUID | None = None) -> None:
        """Refuses a public id that two endpoints would answer to.

        Aliases and canonical slugs are one namespace, and no database constraint can say so: the
        unique index on `alias` stops two endpoints claiming one alias and nothing else. The four
        refusals here are the rest of it: an alias equal to this endpoint's own slug, an alias
        equal to any other endpoint's slug, an alias another endpoint already holds, and the
        symmetric case of a slug moved onto somebody else's alias.

        It has to be a refusal and not a preference, because the gateway resolves a duplicate key
        by keeping whichever binding it indexed first. That is the right behaviour there (order-
        dependent routing is worse than a loud misconfiguration), but it means the plane is the
        only place the ambiguity can actually be prevented.
        """
        owner = await self.repo.get_alias_owner(slug)
        if owner is not None and owner != endpoint_id:
            raise Conflict(f"{slug!r} is already an alias of another endpoint", {"slug": slug})
        for alias in aliases:
            if alias == slug:
                raise Conflict(f"{alias!r} is already this endpoint's own model id", {"alias": alias})
            if await self.repo.name_exists(alias, exclude_id=endpoint_id):
                raise Conflict(f"{alias!r} is already another endpoint's model id", {"alias": alias})
            owner = await self.repo.get_alias_owner(alias)
            if owner is not None and owner != endpoint_id:
                raise Conflict(f"{alias!r} is already an alias of another endpoint", {"alias": alias})

    async def create(self, data: EndpointCreateRequest) -> EndpointSchema:
        check_shape(data.visibility, data.routing_strategy)
        check_targets(data.targets)
        project = await self.repo.project_by_ref(data.project)
        if project is None:
            raise NotFound(f"no project {data.project!r}")
        targets, failover_order = await self._resolve(data.targets, data.failover_order, project.tenant_id)

        # Defaulted from the weights behind the first target, then from the endpoint's own name.
        #
        # The default is the reason for the field, not a convenience. An endpoint in front of
        # one model answers to that model's name, and requiring a caller to type it is how the
        # public id and the id the engine registered come to differ, which is not a visible
        # failure but a 404 from inside the cluster, with the deployment healthy and the tunnel
        # open. Declaring one is then unambiguously a request for an alias.
        slug = (data.slug or "").strip().lower()
        if not slug and targets:
            slug = await self.repo.artifact_slug(targets[0].deployment_id)
        if not slug:
            slug = str(data.name)

        aliases = normalise_aliases(data.aliases)
        await self._check_namespace(slug, aliases)

        endpoint = Endpoint(
            uid=new_uid(ENDPOINT),
            project_id=project.id,
            name=data.name,
            slug=slug,
            visibility=data.visibility,
            routing_strategy=data.routing_strategy,
            failover_order=failover_order,
            enabled=data.enabled,
            labels=data.labels,
            targets=targets,
            aliases=[EndpointAlias(alias=alias) for alias in aliases],
        )
        try:
            await self.repo.add(endpoint)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"endpoint {data.name!r} already exists") from exc
        return await self.get(endpoint.uid)

    async def update(self, ref: str, data: EndpointUpdateRequest, if_match: str | None = None) -> EndpointSchema:
        """Replaces the declared half, and the target set as a whole when it is given.

        The replacement empties the collection and flushes before attaching the new
        rows: `(endpoint_id, deployment_id)` is unique, and a single flush orders
        every insert before the deletes, so re-weighting a target the endpoint
        already has would collide with the row it is about to replace.
        """
        row = await self._one(ref, lock=True)
        endpoint = row[0]
        if if_match is not None and if_match != _revision(endpoint):
            raise Conflict("endpoint changed since it was read", {"revision": _revision(endpoint)})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        if "surface" in fields:
            check_surface(fields["surface"])
        check_shape(
            fields.get("visibility", endpoint.visibility),
            fields.get("routing_strategy", endpoint.routing_strategy),
        )

        if "targets" in fields or "failover_order" in fields:
            targets = fields.pop("targets", UNSET)
            if targets is UNSET:
                raise Invalid("failover order can only be changed alongside the targets it orders")
            check_targets(targets)
            tenant_id = await self.repo.tenant_of(endpoint.project_id)
            rows, order = await self._resolve(targets, fields.pop("failover_order", []), tenant_id)
            endpoint.targets = []
            await self.db.flush()
            endpoint.targets = rows
            endpoint.failover_order = order

        if "slug" in fields:
            # Lowercased and never emptied. This is the public model id, so a caller who clears it
            # has said nothing about what clients should send, and the accurate reading of that is
            # "leave it" instead of "serve under the empty string".
            slug = str(fields.pop("slug")).strip().lower()
            if slug:
                endpoint.slug = slug

        aliases = fields.pop("aliases", UNSET)
        if aliases is not UNSET:
            aliases = normalise_aliases(aliases)
        await self._check_namespace(
            endpoint.slug,
            aliases if aliases is not UNSET else alias_names(endpoint),
            endpoint_id=endpoint.id,
        )
        if aliases is not UNSET:
            await self.repo.replace_aliases(endpoint, aliases)

        for key, value in fields.items():
            setattr(endpoint, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("endpoint conflicts with an existing one") from exc
        return await self.get(endpoint.uid)

    async def delete(self, ref: str) -> None:
        """Deletes the endpoint and its targets; the deployments behind it stay.

        A target is the association, not the thing associated. Removing a public
        name must never take the capacity serving it with it.
        """
        row = await self._one(ref)
        await self.repo.delete(row[0])
        await self.db.commit()

    async def resolved(self, ref: str | None = None) -> ResolvedTableSchema:
        """The routing table: every enabled endpoint, with its targets as addresses.

        Derived on every read and stored nowhere. The endpoint is what somebody
        declared; this is what that currently means, and materialising it would
        create a second copy of the same fact with its own way of being wrong.

        A target is dropped when it is disabled, when its deployment is disabled,
        when the cluster it sits on is not in a serving phase, or when that
        cluster has declared a serving address and then turned it off: each of
        those is somebody saying "not this one", and the endpoint survives all of
        them with fewer upstreams.

        A cluster with no `ClusterServing` row at all is *not* one of them, and it
        is the normal case: inference reaches a cluster over the tunnel its
        serving agent dialled out, so there is no address to publish and
        nothing declares one. Such a target resolves with an empty `url`, and the
        `cluster_id` beside it is the whole of the routing key: the gateway keys
        its tunnel pool by exactly that and dials the published address only when
        there is one. Dropping these rows would make the table empty on every
        cluster we run, which reads as "no model is serving", not as
        "nobody publishes an ingress any more".

        Every one of those is decided here, not in the query, because the
        query is an outer join the whole way down and a `WHERE` on the cluster
        would drop the endpoint's last row along with its last upstream: turning
        "this model has nowhere to go" into "no such model". The cluster's stamp
        goes into the revision for the same reason the others do: marking a
        cluster `draining` changes this table, and a gateway sitting behind a
        `304` would keep routing to it.

        Each upstream carries the name the engine behind it answers to, because
        the public name and the served name are not the same string and nothing
        makes them agree: a workload may declare a `served_model_name` of its
        own, and otherwise the name resolves to the artifact's Hugging Face slug.
        A client calling the endpoint by its public name would be asking the
        frontend for a model it does not have, so the gateway rewrites it.

        That resolution goes into the revision as content (the workload's
        `serving_digest` and the resolved name itself), not as a stamp
        taken off the row it was read from. It used to be the recipe's
        `updated_at`, and a stamp is a proxy for the question that is wrong in
        both directions: it moved when a recipe was renamed or when a field that
        never reaches an engine was edited, making every gateway re-fetch an
        unchanged table, and it said nothing at all about the workload's own
        `served_model_name`, which was never stamped, so declaring one left the
        gateway behind a `304`, rewriting to the name it had before. A digest
        moves on a real configuration change and on nothing else.

        The serving quota is one row per endpoint instead of one per target, so
        its stamp goes into `own` beside the endpoint's and not into the row loop.
        Its limits are passed through undivided: how many gateway replicas are
        running is a fact of the process reading this, not of the plane writing
        it, so the fleet-wide numbers travel and `limit_scope` says how to read
        them.
        """
        rows = await self.repo.resolved(ref)
        if ref is not None and not rows:
            raise NotFound(f"no endpoint {ref!r}")

        order: list[UUID] = []
        grouped: dict[UUID, list] = {}
        endpoints: dict[UUID, Endpoint] = {}
        for row in rows:
            endpoint = row.Endpoint
            if endpoint.id not in grouped:
                order.append(endpoint.id)
                grouped[endpoint.id] = []
                endpoints[endpoint.id] = endpoint
            grouped[endpoint.id].append(row)

        stamps: list[dt.datetime | None] = []
        resolved_names: list[str] = []
        items: list[ResolvedEndpointSchema] = []
        for endpoint_id in order:
            endpoint = endpoints[endpoint_id]
            first = grouped[endpoint_id][0]
            quota, project, tenant = first.ServingQuota, first.Project, first.Tenant
            own: list[dt.datetime | None] = [_stamp(endpoint), _stamp(quota), _stamp(project)]
            aliases = alias_names(endpoint)
            upstreams: list[ResolvedUpstreamSchema] = []
            uids: dict[UUID, str] = {}

            served: list[str] = [_alias_term(alias) for alias in aliases]
            for row in grouped[endpoint_id]:
                target, deployment, cluster = row.EndpointTarget, row.Deployment, row.Cluster
                serving, artifact, workload = row.ClusterServing, row.Artifact, row.Workload
                if target is None:
                    continue
                own.extend([_stamp(target), _stamp(deployment), _stamp(cluster), _stamp(serving)])
                own.append(_stamp(artifact))
                if workload is not None:
                    served.append(workload.serving_digest)
                if deployment is not None:
                    uids[deployment.id] = deployment.uid
                if not target.enabled or deployment is None or not deployment.enabled:
                    continue
                if cluster is None or cluster.phase not in SERVING_PHASES:
                    continue
                if serving is not None and not serving.enabled:
                    continue
                name = (target.served_model_name or "").strip() or (
                    served_model(workload, artifact) if workload is not None and artifact is not None else ""
                )
                served.append(name)
                upstreams.append(
                    ResolvedUpstreamSchema(
                        deployment_id=deployment.uid,
                        deployment_name=deployment.name,
                        admission_phase=deployment.admission_phase,
                        cluster_id=cluster.uid,
                        cluster_name=cluster.name,
                        url=serving.url if serving is not None else "",
                        served_model_name=name,
                        credential_id=str(serving.credential_id)
                        if serving is not None and serving.credential_id
                        else None,
                        weight=target.weight,
                        role=target.role,
                        output_modality=output_modality(workload.serving) if workload is not None else TEXT,
                    )
                )

            stamps.extend(own)
            resolved_names.extend(served)
            items.append(
                ResolvedEndpointSchema(
                    id=endpoint.uid,
                    name=public_model(endpoint),
                    aliases=aliases,
                    visibility=endpoint.visibility,
                    routing_strategy=endpoint.routing_strategy,
                    upstreams=upstreams,
                    failover_order=[uids[d] for d in endpoint.failover_order if d in uids],
                    limits=declared_limits(quota) if quota is not None else None,
                    policy=endpoint_policy(endpoint.surface),
                    tenant_id=tenant.uid,
                    revision=_revision_of(own, served),
                )
            )

        return ResolvedTableSchema(revision=_revision_of(stamps, resolved_names), endpoints=items)
