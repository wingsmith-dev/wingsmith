import datetime as dt

from app.core.schemas import Struct


class ServedModelSchema(Struct):
    """One model a tenant is being served, as the tenant is allowed to see it.

    Not the endpoint object. That one carries the targets, the
    clusters behind them, the weights and the failover order: operational facts
    about where we chose to run something, which are ours and change without the
    tenant being told. What a tenant is owed is the name they call, whether it
    answers, and how much of it they have.

    `model` is the string a client sends as `model` in an OpenAI-compatible
    request. `name` is our internal handle and is shown beside it only because the
    two are usually equal and a support conversation needs the one we can search.

    `state` is derived and has three values. `serving` means at least one replica
    behind this name is ready. `degraded` means the name is live but nothing behind
    it is ready right now. `paused` means somebody chose to turn it off,
    which is a different thing from broken, and the only one of the three that is
    not an incident.
    """

    id: str
    name: str
    model: str
    state: str
    replicas_ready: int
    replicas_desired: int
    created_at: dt.datetime


class ServingLimitsSchema(Struct):
    """What was committed on one model. Every field is null when nothing was sold.

    Null is uncommitted and never zero: an absent limit is not a limit of nothing.
    `enforced` is false until the gateway enforces serving quotas, which it does not
    yet, so these numbers are the agreed terms and not limits a request will hit.
    """

    requests_per_minute: int | None
    tokens_per_minute: int | None
    max_concurrent_requests: int | None
    enforced: bool


class ServedModelDetailSchema(Struct):
    """One served model, with the terms it is served under."""

    model: ServedModelSchema
    limits: ServingLimitsSchema | None
    protocols: list[str]


class ServingOverviewSchema(Struct):
    """Everything the tenant page needs in one read.

    One route instead of a list route the page then walks, because the page shows
    every model at once and a per-model round trip would make the view's cost grow
    with the tenant's catalogue for no added detail.
    """

    tenant: str
    tenant_name: str
    models: list[ServedModelSchema]
