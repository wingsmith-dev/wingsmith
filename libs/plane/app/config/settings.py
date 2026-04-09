import os

from typing import Any
from typing import get_args
from typing import get_origin
from dataclasses import fields
from dataclasses import dataclass


ENV_PREFIX = "HANGAR_"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass
class Settings:
    """Typed process configuration, loaded from the environment.

    Every field is read from `HANGAR_<FIELD>` first and then from the bare
    `<FIELD>` name, so standard variables such as `DATABASE_URL` work unprefixed.
    """

    APP_ENV: str = "local"
    APP_DEBUG: bool = False
    APP_LOG_LEVEL: str = "INFO"
    APP_LOG_FORMAT: str = "console"

    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    HUB_HOST: str = "0.0.0.0"
    HUB_PORT: int = 8001
    HUB_PATH: str = "/v1/hub/sync"

    HUB_INSTANCE_ID: str = ""
    """Identity this hub process claims clusters under. Empty resolves, in order, to
    `HOSTNAME` (a pod name under Kubernetes) and then a random id — see
    `app.hub.identity.instance_id`. Never a default worth hardcoding: two pods that
    resolved to the same value would be indistinguishable owners in the claim table.
    """

    HUB_SESSION_TTL_SECONDS: int = 600
    """How long an idle `SessionState` — cursors and replay buffer — survives with no
    connection attached before the reaper drops it. Sized well past a rolling restart
    (PROTOCOL §4's buffer is sized the same way) so a routine redeploy never reaps a
    session an operator is about to resume; a cluster gone for longer than this pays a
    `full_resync` on return, which is the documented, correct cost of losing the cache.
    """

    HUB_REAPER_INTERVAL_SECONDS: float = 60.0
    """How often the reaper sweeps the registry for idle sessions past `HUB_SESSION_TTL_SECONDS`."""

    HUB_OWNERSHIP_CHECK_SECONDS: float = 10.0
    """How often a live session polls its own claim row to notice it has been displaced
    by a newer `hello` elsewhere in the fleet. Kept close to the ping interval so a
    displaced pod stops writing within roughly one liveness cycle of losing the claim.
    """

    REACTOR_ENABLED: bool = True
    """Whether the hub process also runs the reactor loop. Turn off to run
    `python -m app.reactor` as its own Deployment instead; the lease makes either
    arrangement safe, and running both is a waste rather than a hazard.
    """

    REACTOR_INTERVAL_SECONDS: float = 60.0
    """How often conditions are re-evaluated and the event log is drained.

    A minute because every condition here has a `for` window measured in minutes:
    ticking faster would not make anything fire sooner, it would only re-run four
    fleet-wide queries for a set of alerts that cannot have changed state yet.
    """

    REACTOR_LEASE_SECONDS: float = 180.0
    """How long a reactor's claim survives without renewal. Three ticks, so a slow
    tick or a single failed one does not hand the lease to another process
    mid-evaluation; a genuinely dead reactor costs at most this much silence.
    """

    REACTOR_EVENT_BATCH: int = 500
    """Events matched per tick. Bounds the catch-up after downtime: a reactor that
    has been off for a day works through the backlog over several ticks rather than
    holding one transaction open across all of it.
    """

    REACTOR_NOTIFY_TIMEOUT: float = 5.0
    """Per-webhook timeout. Short: a hung Slack must not stall the tick, and the
    alert is durable and visible in the console whether or not the message lands.
    """

    DATABASE_URL: str = "postgresql+psycopg://hangar:hangar@localhost:5442/hangar"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_POOL_MAX_OVERFLOW: int = 10
    DB_POOL_PRE_PING: bool = True

    METRICS_SOURCES: str = ""
    """Per-cluster metric stores: `us=https://prom.us:9090,ap=https://prom.ap:9090`.

    One store per cluster rather than one store filtered by label, because the
    reference clusters each keep their own Prometheus and set no `externalLabels`.
    Empty means the console shows inventory only, which is a working state.
    """

    METRICS_TIMEOUT: float = 5.0
    METRICS_WINDOW: str = "5m"
    METRICS_STEP: str = "60s"

    OPERATOR_TOKEN: str = ""
    """Bearer token cluster agents present to the hub and the API.

    No default outside `local`: an unset token in any other `APP_ENV` refuses to
    boot rather than fall back to something guessable. In `local`, an unset
    token resolves to `app.core.auth.LOCAL_OPERATOR_TOKEN` so a fresh checkout
    still runs without any configuration.
    """

    HF_TOKEN: str = ""
    """Token presented to the HuggingFace Hub when resolving a model slug.

    Empty is a working state: public repos need no credential, and an unset token
    surfaces a gated repo as an authorization error the caller can report rather
    than a boot failure.
    """

    ADMIN_AUTH: str = ""
    """`enabled` or `disabled`. Empty means "not explicitly set", which resolves
    to `disabled` when `APP_ENV == "local"` and `enabled` everywhere else — see
    `app.core.auth.admin_auth_enabled`. This is the one switch that turns admin
    authentication on the HTTP API off entirely; it never affects the hub, whose
    operator-token check cannot be disabled.
    """

    ADMIN_PASSWORD_HASH: str = ""
    """Bcrypt hash of the admin password, e.g. `python -m app.core.auth hash`."""

    SESSION_SIGNING_SECRET: str = ""
    """HMAC key signing admin session tokens. Required outside `local` whenever
    admin auth is enabled; subject to the same fail-closed rule as `OPERATOR_TOKEN`.
    """

    SESSION_TTL_SECONDS: int = 900
    """Lifetime of a session token minted by `POST /v1/auth/login`."""

    ALLOWED_ORIGINS: str = "http://localhost:5273,http://127.0.0.1:5273"
    """Comma-separated origins the API's CORS policy accepts. Empty disallows
    every browser origin; there is no `*` option.

    5273 rather than Vite's default 5173: the console pins its own port
    (`libs/console/vite.config.ts`) because Vite otherwise walks forward from 5173
    until it finds a free one, which makes the console's origin depend on which
    other projects are running. An origin list cannot be written against that.

    Both spellings are listed because they are two origins to a browser and one
    address to everything else. Vite prints `localhost` and serves `127.0.0.1`
    just as happily, so which one is in the address bar is not a decision anyone
    makes — and listing only one turns it into the difference between a working
    console and a wall of blocked requests.
    """


def _coerce(raw: str, target: Any) -> Any:
    if get_origin(target) is not None:
        args = [a for a in get_args(target) if a is not type(None)]
        target = args[0] if args else str
    if target is bool:
        lowered = raw.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ValueError(f"cannot read {raw!r} as a boolean")
    if target is int:
        return int(raw)
    if target is float:
        return float(raw)
    return raw


def resolved_admin_auth(s: "Settings") -> str:
    """The effective `ADMIN_AUTH` mode after applying the `local`-only default."""
    if s.ADMIN_AUTH:
        return s.ADMIN_AUTH
    return "disabled" if s.APP_ENV == "local" else "enabled"


def validate(s: "Settings") -> None:
    """Fails process boot on credentials that would otherwise fail open.

    Called once by each entrypoint (`app.api.app.create_app`, `app.hub.server.run`),
    never at import time, so tests can build arbitrary `Settings` without a live
    process refusing to start.
    """
    if s.ADMIN_AUTH not in ("", "enabled", "disabled"):
        raise RuntimeError(f"ADMIN_AUTH must be 'enabled' or 'disabled', got {s.ADMIN_AUTH!r}")

    if s.APP_ENV == "local":
        return

    if not s.OPERATOR_TOKEN:
        raise RuntimeError("OPERATOR_TOKEN must be set when APP_ENV is not 'local'")

    if resolved_admin_auth(s) == "enabled":
        if not s.SESSION_SIGNING_SECRET:
            raise RuntimeError(
                "SESSION_SIGNING_SECRET must be set when APP_ENV is not 'local' and admin auth is enabled"
            )
        if not s.ADMIN_PASSWORD_HASH:
            raise RuntimeError("ADMIN_PASSWORD_HASH must be set when APP_ENV is not 'local' and admin auth is enabled")


def load(env: dict[str, str] | None = None) -> Settings:
    """Builds a Settings instance from the given environment mapping."""
    source = os.environ if env is None else env
    values: dict[str, Any] = {}
    for spec in fields(Settings):
        raw = source.get(f"{ENV_PREFIX}{spec.name}") or source.get(spec.name)
        if raw is None or raw == "":
            continue
        values[spec.name] = _coerce(raw, spec.type)
    return Settings(**values)


settings = load()
