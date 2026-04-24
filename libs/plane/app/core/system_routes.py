"""What this process is actually running with, reduced to what may be seen.

A misconfigured `HANGAR_METRICS_SOURCES` can silence a freshly enrolled
cluster's panels without failing anything, and until this endpoint existed the
only way to find that out was to shell into the cluster and read a ConfigMap.
So the plane reports its own effective configuration.

**The exposed set is an allowlist and must stay one.** Every field below is
named by hand in a `Struct` and copied across in `effective_config`. There is no
`asdict(settings)` here and there is deliberately no denylist of suspicious
names: a denylist means the next credential anyone adds to `Settings` is public
the moment it is committed, and nobody would notice until it was in a screenshot.
Adding a field here is one line and a decision; forgetting to add one costs
nothing.

Three rules follow from that:

- A credential is never a value, only a boolean. `operator_token_set` says
  whether the fleet token is configured and nothing else — not a prefix, not a
  length, not a digest. Any of those three narrow a search for the real thing.
- `DATABASE_URL` is reported in parts, never as a string. The driver, host, port
  and database name are what tell an operator the plane is pointed at the wrong
  Postgres; the userinfo is the part that must not travel, and splitting the URL
  means it cannot travel by accident in a query string or a fragment either.
- Metrics sources go through `app.modules.metrics.sources.Source.public`, which
  already exists to answer exactly this question without the per-source
  `|token=`. The `cluster` label matcher is added alongside it because a wrong
  label is the failure mode this panel exists to reveal, and a label is a
  selector, not a secret.

Registered under `/v1/system` from `app.api.routes`, not as a module: `app/modules/`
is one package per object in the object model, and the process's own
configuration is not an object the product has. It sits beside `HealthRoutes`
and `AuthRoutes` as machinery.
"""

from urllib.parse import urlsplit

from litestar import Controller
from litestar import get

from app.config import settings
from app.core.context import RequestContext
from app.utils.schemas import Struct
from app.config.settings import resolved_admin_auth
from app.core.exceptions import Forbidden
from app.modules.metrics import sources


class MetricsSourceConfigSchema(Struct):
    """One entry of `METRICS_SOURCES`, without the credential it carries."""

    cluster: str
    url: str
    kind: str
    label: str
    """The `cluster` label queries are narrowed by; empty means a dedicated store."""


class AppConfigSchema(Struct):
    env: str
    debug: bool
    log_level: str
    log_format: str
    allowed_origins: list[str]


class ApiConfigSchema(Struct):
    host: str
    port: int


class HubConfigSchema(Struct):
    host: str
    port: int
    path: str
    public_host: str
    public_url: str
    instance_id: str
    """As configured. Empty resolves per-process — see `app.hub.identity.instance_id`."""

    session_ttl_seconds: int
    reaper_interval_seconds: float
    ownership_check_seconds: float


class ReactorConfigSchema(Struct):
    enabled: bool
    interval_seconds: float
    lease_seconds: float
    event_batch: int
    notify_timeout: float


class DatabaseConfigSchema(Struct):
    """`DATABASE_URL` in parts. Never the string, and never the userinfo."""

    driver: str
    host: str
    port: int | None
    database: str
    echo: bool
    pool_size: int
    pool_max_overflow: int
    pool_pre_ping: bool


class MetricsConfigSchema(Struct):
    timeout: float
    window: str
    step: str
    shared_url: str
    """The `default=` endpoint every unlabelled entry inherits; empty means none is set.

    Reported separately because "no store at all" and "a store, but this cluster
    has no entry in it" are different faults with different fixes, and a source
    list alone cannot tell them apart.
    """

    remote_write_url: str
    """Where the fleet is told to push, i.e. what the setup values render.

    Not a credential and not a secret — it is a public ingress, and it is the one
    value that decides whether a freshly enrolled cluster sends anything at all.
    Empty means the register screen hands out `monitoring.enabled: false`.
    """

    remote_write_auth_secret: str
    """`<namespace>/<name>` of the htpasswd Secret the reactor reconciles, or empty
    when the reconciler is disabled. A Secret's coordinates, never its contents."""

    sources: list[MetricsSourceConfigSchema]


class AuthConfigSchema(Struct):
    """Credentials as booleans, and the one mode switch as its resolved enum."""

    admin_auth: str
    """`enabled` or `disabled`, after the `local`-only default is applied."""

    admin_password_hash_set: bool
    session_signing_secret_set: bool
    session_ttl_seconds: int
    operator_token_set: bool
    huggingface_token_set: bool


class SetupConfigSchema(Struct):
    """What the register-cluster screen hands an operator to install."""

    operator_image: str
    chart_ref: str
    chart_version: str
    chart_private: bool


class ConfigSchema(Struct):
    app: AppConfigSchema
    api: ApiConfigSchema
    hub: HubConfigSchema
    reactor: ReactorConfigSchema
    database: DatabaseConfigSchema
    metrics: MetricsConfigSchema
    auth: AuthConfigSchema
    setup: SetupConfigSchema


def _database() -> DatabaseConfigSchema:
    """Splits `DATABASE_URL` and keeps only the parts that name the server.

    `urlsplit().hostname` is read rather than `netloc` precisely because it
    cannot carry `user:password@` — the redaction is structural, not a regex
    that a future URL shape could slip past.
    """
    parsed = urlsplit(settings.DATABASE_URL)
    port: int | None
    try:
        port = parsed.port
    except ValueError:
        port = None
    return DatabaseConfigSchema(
        driver=parsed.scheme,
        host=parsed.hostname or "",
        port=port,
        database=parsed.path.lstrip("/"),
        echo=settings.DB_ECHO,
        pool_size=settings.DB_POOL_SIZE,
        pool_max_overflow=settings.DB_POOL_MAX_OVERFLOW,
        pool_pre_ping=settings.DB_POOL_PRE_PING,
    )


def _metrics_sources() -> list[MetricsSourceConfigSchema]:
    parsed = sources.configured()
    return [MetricsSourceConfigSchema(**source.public(), label=source.label) for _, source in sorted(parsed.items())]


def _shared_url() -> str:
    """The shared endpoint's URL, read off a `Source` so its token is dropped with it."""
    shared = sources.shared(sources.DEFAULT_KEY, "")
    return "" if shared is None else shared.url


def _remote_write_auth_secret() -> str:
    """`<namespace>/<name>` the reactor reconciles, or empty when it is off.

    Empty rather than the configured names when the flag is false, because the
    question this answers is "is the plane maintaining that file", and printing
    coordinates it is not writing to would answer it wrongly.
    """
    if not settings.REMOTE_WRITE_AUTH_ENABLED:
        return ""
    return f"{settings.REMOTE_WRITE_AUTH_NAMESPACE}/{settings.REMOTE_WRITE_AUTH_SECRET}"


def effective_config() -> ConfigSchema:
    """The allowlisted view of `settings` this process is running with."""
    return ConfigSchema(
        app=AppConfigSchema(
            env=settings.APP_ENV,
            debug=settings.APP_DEBUG,
            log_level=settings.APP_LOG_LEVEL,
            log_format=settings.APP_LOG_FORMAT,
            allowed_origins=[o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()],
        ),
        api=ApiConfigSchema(host=settings.API_HOST, port=settings.API_PORT),
        hub=HubConfigSchema(
            host=settings.HUB_HOST,
            port=settings.HUB_PORT,
            path=settings.HUB_PATH,
            public_host=settings.PUBLIC_HOST,
            public_url=settings.HUB_PUBLIC_URL,
            instance_id=settings.HUB_INSTANCE_ID,
            session_ttl_seconds=settings.HUB_SESSION_TTL_SECONDS,
            reaper_interval_seconds=settings.HUB_REAPER_INTERVAL_SECONDS,
            ownership_check_seconds=settings.HUB_OWNERSHIP_CHECK_SECONDS,
        ),
        reactor=ReactorConfigSchema(
            enabled=settings.REACTOR_ENABLED,
            interval_seconds=settings.REACTOR_INTERVAL_SECONDS,
            lease_seconds=settings.REACTOR_LEASE_SECONDS,
            event_batch=settings.REACTOR_EVENT_BATCH,
            notify_timeout=settings.REACTOR_NOTIFY_TIMEOUT,
        ),
        database=_database(),
        metrics=MetricsConfigSchema(
            timeout=settings.METRICS_TIMEOUT,
            window=settings.METRICS_WINDOW,
            step=settings.METRICS_STEP,
            shared_url=_shared_url(),
            remote_write_url=settings.METRICS_REMOTE_WRITE_URL,
            remote_write_auth_secret=_remote_write_auth_secret(),
            sources=_metrics_sources(),
        ),
        auth=AuthConfigSchema(
            admin_auth=resolved_admin_auth(settings),
            admin_password_hash_set=bool(settings.ADMIN_PASSWORD_HASH),
            session_signing_secret_set=bool(settings.SESSION_SIGNING_SECRET),
            session_ttl_seconds=settings.SESSION_TTL_SECONDS,
            operator_token_set=bool(settings.OPERATOR_TOKEN),
            huggingface_token_set=bool(settings.HF_TOKEN),
        ),
        setup=SetupConfigSchema(
            operator_image=settings.OPERATOR_IMAGE,
            chart_ref=settings.SETUP_CHART_REF,
            chart_version=settings.SETUP_CHART_VERSION,
            chart_private=settings.SETUP_CHART_PRIVATE,
        ),
    )


class SystemRoutes(Controller):
    tags = ["Main"]
    path = "/system"

    @get("/config", sync_to_thread=False)
    def config(self, context: RequestContext) -> ConfigSchema:
        """The plane's effective non-secret configuration.

        Admin only. A cluster agent authenticates with an operator token to
        report state and read its own declared state; the plane's process
        configuration is neither, and it is a human's diagnostic rather than
        anything the reconcile loop consumes.
        """
        if context.principal.kind != "admin":
            raise Forbidden("plane configuration is readable by an admin principal only")
        return effective_config()
