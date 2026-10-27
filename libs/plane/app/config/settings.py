import os
import ipaddress

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

    Every field is read from `HANGAR_<FIELD>` and nowhere else. A bare
    `DATABASE_URL` or `APP_ENV` left in a shell or a container by something
    unrelated is never picked up.
    """

    APP_ENV: str = "production"
    """`local` on a developer's machine, anything else for a deployment.

    Defaults to `production`, so a plane started without saying otherwise fails
    closed: admin auth on, and no boot without `SESSION_SIGNING_SECRET` and
    `ADMIN_PASSWORD_HASH`. `local` is opt-in (`HANGAR_APP_ENV=local`, which the
    compose stack sets); it is what lets admin auth be off and the secrets be
    unset."""

    APP_DEBUG: bool = False
    APP_LOG_LEVEL: str = "INFO"
    APP_LOG_FORMAT: str = "console"

    API_HOST: str = ""
    """Address the HTTP API binds. Empty resolves to `127.0.0.1` under
    `APP_ENV=local`, where admin auth may be off, and `0.0.0.0` everywhere else;
    see `api_host`. A container that must listen on every interface in `local`
    sets it explicitly, and publishes the port on the host's loopback only."""

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
    """Where the plane reads measurement from: `default=<url>[|token=…][|kind=…]`.

    The fleet remote-writes into one central Prometheus that stamps `cluster` on
    every series, so the endpoint is one value and which label narrows it is the
    cluster's own `metrics_label` column, not a per-entry setting here (ADR 0011).
    Empty means the console shows inventory only, which is a working state.

    Per-cluster entries still parse and still win over the column: `<cluster>|label=<name>`
    overrides the label for one cluster, and `<cluster>=<url>` means a store
    holding that cluster alone, queried with no cluster matcher at all (ADR 0008).

    See `app.modules.metrics.sources` for the parse.
    """

    METRICS_TIMEOUT: float = 5.0
    METRICS_WINDOW: str = "5m"
    METRICS_STEP: str = "60s"

    METRICS_REMOTE_WRITE_URL: str = ""
    """The central store's write endpoint every cluster's metrics agent pushes to.

    A fleet constant, not a per-cluster fact: one store fronts the fleet, so this
    is rendered verbatim into `monitoring.remoteWrite.url` in the setup values the
    register screen produces. Empty renders `monitoring.enabled: false` instead, so
    a deployment with no store still hands out installable values — an agent with
    no `remote_write` scrapes a cluster and discards every sample, which is worse
    than not scraping it.
    """

    REMOTE_WRITE_AUTH_ENABLED: bool = False
    """Whether the reactor keeps an htpasswd Secret for the metrics write endpoint.

    Optional, and only for a receiver that checks HTTP basic auth against an
    htpasswd file: every cluster's agent writes with its cluster id as the user
    and its enrolment token as the password, and this Secret holds one bcrypt
    line per cluster (ADR 0012). Any ingress or proxy that can read htpasswd from
    a Secret can front the receiver; Traefik's `basicAuth` middleware is one.

    Off by default because it is the one thing the plane does that reaches outside
    its own namespace, and because it is inert without a service account to
    authenticate with. Enabled without `REMOTE_WRITE_AUTH_NAMESPACE`, or with no
    in-cluster credential (local dev, tests), the pass logs once and does nothing
    — never a crash loop.
    """

    REMOTE_WRITE_AUTH_NAMESPACE: str = ""
    """Namespace of that Secret, wherever the receiver's ingress reads it from.
    No default: there is no namespace every deployment shares."""

    REMOTE_WRITE_AUTH_SECRET: str = "prom-remote-write-auth"
    """Name of that Secret. The plane's Role should be scoped to this one name."""

    REMOTE_WRITE_AUTH_KEY: str = "users"
    """Key within it holding the htpasswd file. `users` is what Traefik's
    `basicAuth.secret` reads when the Secret is not of type `kubernetes.io/basic-auth`."""

    GATEWAY_SERVICE_TOKEN: str = ""
    """Bearer token the inference gateway presents to the plane API.

    It resolves to the `gateway` principal, which may read the routing table and
    the tenant table and verify an agent's enrolment token, and nothing else — no
    admin rights, no writes. Generate a long random value and give the same one
    to the gateway as `HANGAR_GATEWAY_SERVICE_TOKEN`.

    Unset outside `local` means no gateway principal resolves at all, which
    fails closed: the gateway gets `401` until it is configured. In `local`, an
    unset token resolves to `app.auth.const.LOCAL_GATEWAY_SERVICE_TOKEN` so a
    fresh checkout's gateway runs without configuration.

    Cluster operators never use this or any other shared token. Each cluster
    holds its own enrolment token, minted by the plane at registration.
    """

    HF_TOKEN: str = ""
    """Token presented to the HuggingFace Hub when resolving a model slug.

    Empty is a working state: public repos need no credential, and an unset token
    surfaces a gated repo as an authorization error the caller can report rather
    than a boot failure.
    """

    ENGINE_RUNTIME_REPOS: str = "vllm=nvcr.io/nvidia/ai-dynamo/vllm-runtime,sglang=nvcr.io/nvidia/ai-dynamo/sglang-runtime,trtllm=nvcr.io/nvidia/ai-dynamo/tensorrtllm-runtime"
    """`engine=repository` pairs the plane resolves a workload's runtime image from.

    Keyed by engine because that is what a repository is: `vllm-runtime` is the
    Dynamo runtime with vLLM compiled into it, and `dynamo.vllm` exists nowhere
    else. Picking the engine therefore picks the repository, and the tag beside it
    — `engine_runtime_version` — is a Dynamo release rather than the engine's own number.

    A workload names an engine and a runtime version; the image those two mean is a
    property of the deployment, not of the configuration, and asking every author
    to retype it is how a workload reaches a cluster with no image at all. A workload
    with no image is not rejected by anything downstream — the operator simply
    never renders a prefetch job for its weights, and the deployment waits on a
    download nobody was told to start. Resolving it here removes the field from
    the set of things a user can silently get wrong.

    An explicit `engine_runtime_image` on the workload always wins; this is the fallback.
    An engine absent from this map has no default and still requires one.
    """

    ENGINE_RUNTIME_TAG_DEFAULT: str = "1.4.1"
    """Tag used when a workload's `engine_runtime_version` is empty or `latest`.

    `latest` is not a tag these registries publish, so accepting it verbatim
    produces an `ImagePullBackOff` rather than a validation error. Keep this in
    step with `frontend.shared.image.tag` in the cluster-setup chart: the
    prefetch job's layout has to be the one the engine later reads.
    """

    BENCHMARK_CONFORMANCE_IMAGE: str = ""
    """Image the `conformance` benchmark harness runs when a request names none.
    Empty refuses such runs with a message naming this setting."""

    BENCHMARK_EVALSCOPE_IMAGE: str = ""
    """Image the `evalscope` benchmark harness runs when a request names none, built from
    `libs/benchmarking/images/evalscope`. Empty refuses such runs with a message naming this setting."""

    ADMIN_AUTH: str = ""
    """`enabled` or `disabled`. Empty means "not explicitly set", which resolves
    to `disabled` when `APP_ENV == "local"` and `enabled` everywhere else — see
    `app.auth.manager.AuthManager.admin_auth_enabled`. This is the one switch that turns admin
    authentication on the HTTP API off entirely, and `validate` refuses `disabled`
    outside `local`. It never affects the hub, whose enrolment-token check cannot
    be disabled, nor the gateway, which still needs its own token.
    """

    ADMIN_PASSWORD_HASH: str = ""
    """Bcrypt hash of the admin password. Generate one with `uv run python -m app.auth hash`
    (or `docker run --rm -it <plane image> python -m app.auth hash`), which prompts for the
    password rather than taking it as an argument. Changing it ends every admin session."""

    SESSION_SIGNING_SECRET: str = ""
    """HMAC key signing session tokens. Required outside `local`, where the plane
    refuses to boot without it. Under `local`, an unset secret is a random key
    drawn once per process (`app.auth.manager.AuthManager.signing_secret`), never
    a constant."""

    SESSION_TTL_SECONDS: int = 900
    """Lifetime of a session token minted by `POST /v1/auth/login`."""

    REFRESH_TTL_SECONDS: int = 604800
    """Absolute lifetime of a signed-in session, from sign-in.

    Refreshing rotates the tokens but never extends this: past it, the person
    signs in again. Seven days, so a stolen refresh token is worth a week at most
    even if nobody notices it. Sessions are stored server-side, so logout,
    reuse of a rotated token, a password change or a revoked tenant token end one
    sooner (`app.auth.service`).
    """

    PUBLIC_HOST: str = ""
    """Public hostname that fronts the hub ingress, e.g. `hangar.example.com`.

    The hub's host, which is not necessarily the host serving this request: the
    console and the hub can be separate names in front of separate Services, and
    in some deployments they are.

    Reported by the config endpoint. The setup values do not derive the hub
    address from it: they take `HUB_PUBLIC_URL` and nothing else.
    """

    HUB_PUBLIC_URL: str = ""
    """The `wss://` URL cluster operators dial, written into the setup values.

    Required for cluster setup outside `APP_ENV=local`. Empty refuses to render
    setup values or mint an enrolment token rather than guessing the address
    from a request header, which a caller controls. Under `local`, empty means
    `ws://localhost:{HUB_PORT}{HUB_PATH}`, the compose hub.
    """

    OPERATOR_IMAGE: str = ""
    """Fully-qualified operator image (`registry/repo:tag`) pinned into the setup
    values so an install records what it ran. Pinned to a tag, never `latest`.
    Empty leaves the chart's own `appVersion` to name the image."""

    AGENT_IMAGE: str = ""
    """Fully-qualified serving-agent image, pinned into the setup values the
    same way `OPERATOR_IMAGE` is and for the same reason. Empty leaves the chart's
    own default."""

    IMAGE_PULL_SECRET_NAME: str = ""
    """Name of the Secret the operator and agent ServiceAccounts present to their
    registry, written into the setup values.

    Empty renders no pull secret at all, which is right when those images are
    public or sit in a registry the nodes already authenticate to. Set it, with the
    username and token below, when they are private — otherwise a registered
    cluster is handed values that install cleanly and cannot pull."""

    IMAGE_PULL_SECRET_REGISTRY: str = "ghcr.io"
    """The registry those credentials authenticate to."""

    IMAGE_PULL_SECRET_USERNAME: str = ""
    """The registry username. Not a secret, unlike the token beside it."""

    IMAGE_PULL_SECRET_TOKEN: str = ""
    """The plane's own registry token. Never rendered into a cluster's values
    unless `IMAGE_PULL_SECRET_SHARE_TOKEN` is set, and then only in the response
    to an enrolment-token mint, never on a plain read of the setup."""

    IMAGE_PULL_SECRET_SHARE_TOKEN: bool = False
    """Render `IMAGE_PULL_SECRET_TOKEN` into every cluster's values at mint.

    Off by default. Shared, it is one credential copied into every cluster, and
    rotating or revoking it is a `helm upgrade` everywhere. Off, a named pull
    secret is rendered as a blank for a read-only token issued to that cluster
    alone, which is the recommended setup when the images are private."""

    TENANT_ALLOW_PUBLIC_NO_AUTH: bool = False
    """Allow a tenant on the gateway's public listener (a host set, or ingress
    `public`) to declare `auth: none`.

    Off by default, because such a tenant is unauthenticated inference for anyone
    who finds its host. Turn it on only for a deployment whose public listener is
    itself behind something that authenticates callers."""

    TUNNEL_GATEWAY: str = ""
    """The gateway address a cluster's agent dials, `host:port`, written into the
    setup values.

    One address, however many replicas the gateway runs. Coverage does have to be
    complete — the tunnel pool is in-process, so a gateway replica can only answer
    for a cluster whose tunnel it is itself holding — but the replica count rides
    the handshake, so an agent given this one name redials it until it has found
    them all. Naming the replicas here instead would mean that scaling the gateway
    is an edit to the values of every cluster in the fleet.

    This is the plane's own deployment fact — the public tunnel name of the
    gateway's StatefulSet — which is why it is configuration here and not a
    column. Empty renders the agent block as a blank with an example,
    because a cluster cannot be handed an address the plane does not know, and
    because values that install and serve nothing beat values that do not
    install."""

    SETUP_CHART_REF: str = "oci://ghcr.io/hangar-dev/charts/cluster-setup"
    """OCI reference of the `cluster-setup` chart the register-cluster setup
    instructions tell an operator to install. Points at where `just chart-push`
    publishes it; a deployment overrides it only if it mirrors the chart
    elsewhere."""

    SETUP_CHART_VERSION: str = ""
    """Chart version the setup instructions pin (`helm upgrade --version`). The
    chart version is the compatibility unit — its `appVersion` names the operator
    image — so an install records exactly what it ran. Empty omits `--version`
    and takes whatever the registry resolves as latest, which is fine for dev but
    should be pinned in a deployment."""

    SETUP_CHART_PRIVATE: bool = False
    """Whether the chart registry requires a `helm registry login` before the
    pull. When true the setup instructions carry that login step (the registry
    host is derived from `SETUP_CHART_REF`). The published chart is public, so
    this is for a deployment that mirrors it into a private registry."""

    TRUSTED_PROXIES: str = ""
    """Comma-separated addresses or CIDRs of the proxies in front of the API whose
    `X-Forwarded-For` the plane believes, e.g. the ingress controller's pod range.

    Used to charge failed sign-ins to the caller rather than to the proxy
    (`app.core.throttle.client_address`). Empty ignores the header, so every
    caller behind a proxy shares one budget: set it whenever the API sits behind
    one, or one caller's failures lock out everyone else behind it too.
    """

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


def api_host(s: "Settings") -> str:
    """The effective `API_HOST`: loopback under `local` unless set explicitly."""
    if s.API_HOST:
        return s.API_HOST
    return "127.0.0.1" if s.APP_ENV == "local" else "0.0.0.0"


def trusted_proxies(s: "Settings") -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """`TRUSTED_PROXIES` parsed. Raises `RuntimeError` naming the entry that is
    not an address or a CIDR, so `validate` stops the boot on it rather than
    every sign-in failing with a 500."""
    networks = []
    for part in s.TRUSTED_PROXIES.split(","):
        entry = part.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise RuntimeError(f"TRUSTED_PROXIES entry {entry!r} is not an address or a CIDR") from exc
    return networks


def resolved_admin_auth(s: "Settings") -> str:
    """The effective `ADMIN_AUTH` mode: always `enabled` outside `local`, where
    `disabled` is the default unless set."""
    if s.APP_ENV != "local":
        return "enabled"
    return s.ADMIN_AUTH or "disabled"


def validate(s: "Settings") -> None:
    """Fails process boot on configuration that would otherwise fail open.

    Called once by each entrypoint (`app.api.app.create_app`, `app.hub.server.run`),
    never at import time, so tests can build arbitrary `Settings` without a live
    process refusing to start.

    Outside `local`, admin auth is always on and both of its secrets are
    required. `ADMIN_AUTH=disabled` is a developer convenience and nothing else:
    there is no perimeter a deployed plane can rely on to make it safe.
    """
    if s.ADMIN_AUTH not in ("", "enabled", "disabled"):
        raise RuntimeError(f"ADMIN_AUTH must be 'enabled' or 'disabled', got {s.ADMIN_AUTH!r}")
    trusted_proxies(s)

    if s.APP_ENV == "local":
        return

    hint = f"(APP_ENV is {s.APP_ENV!r}; a development checkout sets HANGAR_APP_ENV=local)"
    if s.ADMIN_AUTH == "disabled":
        raise RuntimeError(f"ADMIN_AUTH=disabled is allowed only when APP_ENV is 'local' {hint}")
    if not s.SESSION_SIGNING_SECRET:
        raise RuntimeError(f"SESSION_SIGNING_SECRET must be set when APP_ENV is not 'local' {hint}")
    if not s.ADMIN_PASSWORD_HASH:
        raise RuntimeError(f"ADMIN_PASSWORD_HASH must be set when APP_ENV is not 'local' {hint}")


def load(env: dict[str, str] | None = None) -> Settings:
    """Builds a Settings instance from the given environment mapping."""
    source = os.environ if env is None else env
    values: dict[str, Any] = {}
    for spec in fields(Settings):
        raw = source.get(f"{ENV_PREFIX}{spec.name}")
        if raw is None or raw == "":
            continue
        values[spec.name] = _coerce(raw, spec.type)
    return Settings(**values)


settings = load()
