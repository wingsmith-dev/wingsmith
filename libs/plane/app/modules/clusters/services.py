import hmac
import shlex
import datetime as dt

from uuid import UUID
from typing import NoReturn
from typing import get_args
from logging import getLogger
from collections import OrderedDict

import yaml

from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthManager
from app.config import settings
from app.core.ids import CLUSTER
from app.core.ids import new_uid
from app.db.models import Cluster
from app.db.models import ClusterServing
from app.db.models import ClusterSyncState
from app.ingestion import statements
from app.core.schemas import ListResponse
from app.core.throttle import FailureBudget
from app.utils.schemas import Struct
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.exceptions import NotAuthorized
from app.core.exceptions import TooManyRequests
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import ClusterPhase
from .schemas import ClusterSchema
from .schemas import ClusterSyncSchema
from .schemas import ClusterSetupSchema
from .schemas import ClusterRollupSchema
from .schemas import ClusterCreateRequest
from .schemas import ClusterServingSchema
from .schemas import ClusterUpdateRequest
from .schemas import TunnelIdentitySchema
from .schemas import ClusterHeadroomSchema
from .schemas import ClusterServingRequest
from .repository import ClusterRepository


logger = getLogger(__name__)

PHASES = frozenset(get_args(ClusterPhase))

RESERVED_NAMES = frozenset({"rollups", "tunnel-auth"})
"""Fixed segments under `/v1/clusters`. A cluster called `rollups` would be
unreachable by name, because `GET /v1/clusters/rollups` is the rollup list."""


class KeyedFailureBudget:
    """One `FailureBudget` per key, so one source's failures spend only its own.

    Bounded: past `max_keys` the least recently charged key is forgotten, which
    errs towards answering, not towards holding memory for every address
    that ever sent a bad token.
    """

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 4096) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._budgets: OrderedDict[str, FailureBudget] = OrderedDict()

    def spent(self, key: str) -> bool:
        budget = self._budgets.get(key)
        return budget is not None and budget.spent()

    def fail(self, key: str) -> None:
        budget = self._budgets.pop(key, None) or FailureBudget(self.limit, self.window_seconds)
        budget.fail()
        self._budgets[key] = budget
        while len(self._budgets) > self.max_keys:
            self._budgets.popitem(last=False)

    def clear(self) -> None:
        self._budgets.clear()


TUNNEL_AUTH_GUESSES = KeyedFailureBudget(limit=20, window_seconds=60.0)
"""Failed agent verifications answered per source in a rolling minute.

Keyed by the agent's address as the gateway saw it (`TunnelAuthRequest.source`),
or by the calling principal when the gateway does not say. A single budget for
the whole process let anyone able to send failures spend it for everybody, and
an exhausted budget used to answer with a bare 429 that a gateway could not
tell from an outage, so a token that had just been rotated out kept working
for as long as the gateway's cache grace lasted.

Twenty is well above what a healthy agent produces (verification happens once
per tunnel, at connection setup) and far below what guessing a 256-bit token
needs. Spent only by failures and consulted only on the failure path, after the
lookup, so a valid token is never refused by it, and a refusal that is
throttled still carries the verdict (`unknown_token`) in its detail.
"""


def _revision(cluster: Cluster) -> str:
    """The value `If-Match` is checked against.

    Derived from the last write, not stored, because `updated_at` already
    moves on every update and a second column tracking the same fact is a second
    column that can disagree with it.
    """
    stamp = cluster.updated_at or cluster.created_at
    return str(int(stamp.timestamp() * 1_000_000))


STALE_AFTER = dt.timedelta(minutes=40)
"""How long a connected cluster may go without sending anything before its link
is reported `stale` instead of `connected`.

The failure this exists for is an operator whose observation loop stopped while
its socket stayed up: `reachable` is true, because the socket is the only thing
it ever measured, and every number the console draws from that cluster is frozen
with nothing saying so.

The threshold is tied to the operator's resync interval and must stay above it.
`last_sync_at` moves only when a frame is *ingested*, and the observation loop
sends nothing when nothing has changed, so on a quiet cluster the
only thing that reliably bumps it is the periodic resync, currently 15 minutes
(`HANGAR_RESYNC_INTERVAL`). Forty minutes is two missed resyncs plus room for a
slow one. A threshold below the resync interval would mark every healthy idle
cluster stale, which is worse than the silence it is meant to catch: an
indicator that cries wolf on a working fleet is one nobody reads when it is
right. Heartbeats are intentionally not counted: `last_seen_at` tracks those,
and a live socket carrying no observations is precisely the state this reports."""


def _sync(state: ClusterSyncState | None) -> ClusterSyncSchema:
    if state is None or state.connected_at is None:
        return ClusterSyncSchema(
            state="never_connected",
            last_sync_at=None,
            last_seen_at=None,
            operator_version=state.operator_version if state else None,
            lag_ms=None,
            last_error=state.last_error if state else None,
            distribution=state.distribution if state else None,
            cloud=state.cloud if state else None,
            observed_region=state.observed_region if state else None,
            read_only=state.read_only if state else False,
            multinode=state.multinode if state else False,
        )
    lag = None
    stale = False
    if state.last_sync_at is not None:
        now = dt.datetime.now(dt.UTC)
        since = now - state.last_sync_at
        lag = max(0, int(since.total_seconds() * 1000))
        stale = since > STALE_AFTER

    if not state.reachable:
        link = "disconnected"
    elif stale:
        link = "stale"
    else:
        link = "connected"

    return ClusterSyncSchema(
        state=link,
        last_sync_at=state.last_sync_at,
        last_seen_at=state.last_seen_at,
        operator_version=state.operator_version,
        lag_ms=lag,
        last_error=state.last_error,
        distribution=state.distribution,
        cloud=state.cloud,
        observed_region=state.observed_region,
        read_only=state.read_only,
        multinode=state.multinode,
    )


def _rollup(cluster_uid: str, row: dict | None) -> ClusterRollupSchema:
    """Turns one keyed rollup dict into a schema, flooring the free block.

    A cluster the repository never returned a row for has no observed supply, so
    every number is zero. `largest_contiguous_block` is floored at one whenever a
    cluster has any free device: a device outside every topology domain is still
    a placeable block of one, which the domain-only query cannot see.
    """
    row = row or {}
    total = row.get("devices_total", 0)
    allocatable = row.get("devices_allocatable", 0)
    block = row.get("largest_block", 0)
    if block == 0 and allocatable > 0:
        block = 1
    return ClusterRollupSchema(
        cluster_id=cluster_uid,
        nodes_total=row.get("nodes_total", 0),
        nodes_ready=row.get("nodes_ready", 0),
        devices_total=total,
        devices_allocatable=allocatable,
        devices_allocated=total - allocatable,
        pool_count=row.get("pool_count", 0),
        deployment_count=row.get("deployment_count", 0),
        headroom=ClusterHeadroomSchema(
            free_devices=allocatable,
            whole_nodes_free=row.get("whole_nodes_free", 0),
            largest_contiguous_block=block,
        ),
    )


def _hub_url() -> str:
    """The `wss://` URL the operator dials, written into the setup values.

    Only ever `HUB_PUBLIC_URL`. It used to fall back to the `Host` header of the
    request asking for the setup, which let whoever could reach the API with a
    forged header decide where a new cluster's operator sends its enrolment
    token. A plane that has not said where its hub is gets a refusal naming the
    setting instead of a guess, and the refusal comes before anything is minted.

    `APP_ENV=local` is the exception: unset there means the compose hub on this
    machine, built from settings instead of the request, so `just dev` and
    `just fleet` work without extra configuration.
    """
    url = settings.HUB_PUBLIC_URL.strip()
    if not url and settings.APP_ENV == "local":
        return f"ws://localhost:{settings.HUB_PORT}{settings.HUB_PATH}"
    if not url:
        raise Invalid(
            "this plane has no public hub URL; set HANGAR_HUB_PUBLIC_URL to the wss:// address clusters dial",
            {"setting": "HANGAR_HUB_PUBLIC_URL"},
        )
    if not url.startswith(("wss://", "ws://")):
        raise Invalid(
            "HANGAR_HUB_PUBLIC_URL must be a ws:// or wss:// URL",
            {"setting": "HANGAR_HUB_PUBLIC_URL"},
        )
    return url


def _registry_host(chart_ref: str) -> str:
    """The registry host to `helm registry login`, from an OCI chart reference.

    `oci://ghcr.io/hangar-dev/charts/cluster-setup` → `ghcr.io`.
    """
    return chart_ref.removeprefix("oci://").split("/", 1)[0]


def values_filename(cluster_name: str) -> str:
    """What the console saves the values as and the helm command reads.

    Named after the cluster so it neither overwrites the chart's own
    `values.yaml` when saved next to it nor gets mistaken for another cluster's.
    The `.local.yaml` suffix is what the repository's `.gitignore` and the
    chart's `.helmignore` already drop, so a copy that carries the enrolment
    token and lands in a checkout is neither committed nor packaged.
    """
    return f"values.{cluster_name}.local.yaml"


class SetupValues(Struct):
    """Everything the plane fills into a cluster's values file.

    A struct instead of a dozen positional arguments, and the fields are exactly
    the plane's half of the contract with the chart. Notably absent: any
    credential for the metrics write ingress. The chart derives the basicAuth
    password from the enrolment-token Secret it already creates, so username and
    password are the chart's business and never travel through this file (ADR 0012).

    `pull_secret_token` is empty unless the plane is explicitly configured to
    share its own registry token (`IMAGE_PULL_SECRET_SHARE_TOKEN`) and this is a
    mint; otherwise a named pull secret is rendered as a blank for the cluster's
    own read-only token.
    """

    cluster_name: str
    hub_url: str
    region: str
    operator_image: str = ""
    agent_image: str = ""
    gateway: str = ""
    remote_write_url: str = ""
    enrolment_token: str | None = None
    pull_secret_name: str = ""
    pull_secret_registry: str = ""
    pull_secret_username: str = ""
    pull_secret_token: str = ""


def _dump(mapping: dict) -> list[str]:
    """One top-level block, serialised. Every value goes through here and never
    through string formatting, so a name or a region cannot add a key."""
    return yaml.safe_dump(mapping, sort_keys=False, default_flow_style=False, width=1_000_000).splitlines()


def _image(reference: str) -> dict | None:
    repository, _, tag = reference.rpartition(":")
    if not (repository and tag) or "/" in tag:
        return None
    return {"repository": repository, "tag": tag}


def _setup_values(values: SetupValues) -> str:
    """The full editable values file for the chart, as YAML text.

    The top block is filled by the plane: cluster identity, `region`, `readOnly`,
    and (at mint only) the enrolment token. The pinned operator image goes in the
    one `operator:` block below the divider, beside the image allowlists the
    cluster owner fills in, because a second `operator:` mapping would shadow the first. `region` and
    `readOnly` sit there, not below the divider because they must share the
    one `cluster:` key; a second mapping would silently shadow the identity it
    carries. Below a divider is the cluster's own infra facts left blank with a
    comment each: pool labels, the RWX storage class, the HF token, serving, and
    the registry token when the images are private. The operator downloads this
    one file, edits the blanks, and runs helm against it. The chart refuses an
    install with any of those blanks still empty, so a half-edited file fails at
    `helm upgrade`, not at the pod.

    Each block is a dict passed through `yaml.safe_dump`; only the comments
    between blocks are literal text.

    The HF token is the one blank that is opt-in, not required: whether a
    cluster needs one depends on which models land there, and the operator mounts
    `hf-token-secret` with `Optional: true` in all three of its renders. So
    `create` is emitted false.

    The operator image is pinned from `OPERATOR_IMAGE` when set so an install
    records what it ran; unset leaves the chart's own appVersion. When
    `enrolment_token` is given (mint only) it is folded in as
    `enrolmentToken.value` so the chart creates the operator Secret; it is never
    persisted, so a later render omits it and the chart then requires either a
    fresh mint or an explicit `enrolmentToken.existing=true`.

    The `agent:` block is plane-filled when `TUNNEL_GATEWAY` is configured,
    because the gateway's address is a fact about the plane's own deployment and
    not about the cluster. One address and not a list of replicas, so that
    scaling the gateway never reaches this file. It is still emitted off, because
    the chart refuses an agent with no frontend to forward to, and the frontend
    cannot go on in a first install: its CRD is applied at runtime by the Dynamo
    operator. With none configured it falls below the divider as a blank.

    The monitoring block is `enabled` plus the fleet's one write endpoint, and
    nothing else. When no endpoint is configured it renders `enabled: false`
    with no url, because the chart refuses an enabled agent without one.
    """
    lines = [
        "# Filled by the plane: cluster identity. Leave as-is, except `readOnly`.",
        "#",
        "# `readOnly` is whether the operator may act on what the plane declares. False",
        "# is the working setting: a read-only operator enrols, reports, stays healthy",
        "# and quietly applies nothing. Set true to park a cluster as an observer; the",
        "# plane will say so on its sync card.",
        "#",
        "# A managed cluster (readOnly false) carries out every operation the plane",
        "# sends: cordon, drain, and benchmark, which runs a harness image from",
        "# `operator.benchmark.images` below as a Job in the workload namespace. To",
        "# allow only some of them, set readOnly true and list the ones to keep as",
        '# `allowedOperations`, e.g. "benchmark"; that list is ignored otherwise.',
        *_dump(
            {
                "cluster": {
                    "id": values.cluster_name,
                    "hubUrl": values.hub_url,
                    "region": values.region,
                    "readOnly": False,
                }
            }
        ),
    ]
    operator: dict = {}
    operator_image = _image(values.operator_image) if values.operator_image else None
    if operator_image:
        operator["image"] = operator_image
    operator["images"] = {"allowedPrefixes": []}
    operator["benchmark"] = {"images": []}
    if values.gateway:
        agent: dict = {"enabled": False, "gateway": {"address": values.gateway}}
        agent_image = _image(values.agent_image) if values.agent_image else None
        if agent_image:
            agent["image"] = agent_image
        lines += [
            "",
            "# How inference reaches this cluster. The agent dials out to the address below",
            "# and nothing dials in, so there is no port to open here. One address however",
            "# many gateway replicas the plane runs: each handshake says how many there",
            "# are, and the agent redials until it holds a tunnel to every one.",
            "#",
            "# Off, because the agent has nothing to forward to yet. It proxies every",
            "# request to a Dynamo frontend in this cluster, whose CRD the Dynamo operator",
            "# only applies at runtime, so no frontend can go on in the first install and",
            "# the chart refuses an agent without one. Install once as rendered, then",
            "# upgrade with `agent.enabled: true` and `frontend.mode` set, to start serving.",
            *_dump({"agent": agent}),
        ]
    if values.pull_secret_name and values.pull_secret_token:
        lines += [
            "",
            "# The registry credential this plane is configured to share with every",
            "# cluster (HANGAR_IMAGE_PULL_SECRET_SHARE_TOKEN). Shown at mint only.",
            "# Prefer replacing it with a read-only token issued for this cluster alone.",
            *_dump(
                {
                    "imagePullSecret": {
                        "create": True,
                        "name": values.pull_secret_name,
                        "registry": values.pull_secret_registry,
                        "username": values.pull_secret_username,
                        "token": values.pull_secret_token,
                    }
                }
            ),
        ]
    if values.enrolment_token:
        lines += [
            "",
            "# This cluster's own enrolment token, minted for it and shown once. The chart",
            "# creates the operator Secret from it. The hub opens a session only for a",
            "# per-cluster enrolment token, so no other credential belongs here.",
            *_dump({"enrolmentToken": {"value": values.enrolment_token}}),
        ]
    if values.remote_write_url:
        lines += [
            "",
            "# Telemetry. The agent scrapes DCGM and Dynamo here and pushes to the central",
            "# store; the credential is the enrolment token, which the chart already holds.",
            *_dump({"monitoring": {"enabled": True, "remoteWrite": {"url": values.remote_write_url}}}),
        ]
    else:
        lines += [
            "",
            "# Telemetry is off: this plane has no central metrics store configured",
            "# (HANGAR_METRICS_REMOTE_WRITE_URL), and an agent with nowhere to push",
            "# would scrape the cluster and discard every sample.",
            *_dump({"monitoring": {"enabled": False}}),
        ]
    lines += [
        "",
        "# ── Edit below: this cluster's own infra facts ─────────────────────────",
        "",
        "# The frontend topology this cluster serves through. Three values:",
        "#",
        "#   per-model  one frontend per model, on that model's own image, rendered by",
        "#              the operator into the model's own graph. The default, and what a",
        "#              cluster ends on.",
        "#   shared     one frontend for the cluster; every model must be servable by",
        "#              its single image.",
        "#   none       no frontend. Required for THIS first install, whichever topology",
        "#              you want: the CRD is not registered until the Dynamo operator",
        "#              this install brings up has run once.",
        "#",
        "# Left at none for that reason; set per-model on the upgrade that turns the",
        "# agent on. Changing it later, once models are running, is not a config change:",
        "# every graph is deleted and re-rendered and every model reloads its weights.",
        *_dump({"frontend": {"mode": "none"}}),
        "",
        "# How this cluster labels its node pools, e.g. labelKey example.com/pool,",
        "# gpuValue gpu (the pool holding the GPU nodes), cacheValue system (the pool",
        "# prefetch jobs run on). `cacheValue` must name a pool that exists, or the",
        "# model-prefetch jobs stay Pending.",
        "# `cacheTaintKey` is optional: the taint on the cache pool that prefetch jobs",
        "# must tolerate. Empty takes the operator's default, hangar.dev/cache.",
        *_dump({"pools": {"labelKey": "", "gpuValue": "", "cacheValue": "", "cacheTaintKey": ""}}),
        "",
        "# The shared model-weight cache. Must be a ReadWriteMany class, e.g. an",
        "# EFS-backed one: a default class is usually RWO and breaks the shared mount.",
        *_dump({"modelCache": {"storageClass": "", "accessMode": "ReadWriteMany", "size": "500Gi"}}),
        "",
        "# Hugging Face token, only for gated weights. The operator mounts",
        "# hf-token-secret optionally, so leaving this off costs nothing until a model",
        "# needs it; flip `create` and fill `value` then.",
        *_dump({"hfToken": {"create": False, "value": ""}}),
        "",
        "# Which images the plane may have this cluster run. Empty `allowedPrefixes`",
        "# allows any engine, frontend or prefetch image; list registries or repos,",
        "# e.g. nvcr.io/nvidia/ai-dynamo, to refuse the rest. Empty benchmark `images`",
        "# keeps the operator's default, the harness images the plane pins."
        + (" `image` is pinned by the plane; leave it." if operator_image else ""),
        *_dump({"operator": operator}),
    ]
    if values.pull_secret_name and not values.pull_secret_token:
        lines += [
            "",
            "# The operator and agent images are private. Issue a read-only token for THIS",
            "# cluster (pull access to those packages and nothing else, with an expiry)",
            "# and fill it in: one token per cluster means a leaked one is revoked without",
            "# touching the rest of the fleet. The chart refuses an install with it blank.",
            *_dump(
                {
                    "imagePullSecret": {
                        "create": True,
                        "name": values.pull_secret_name,
                        "registry": values.pull_secret_registry,
                        "username": values.pull_secret_username,
                        "token": "",
                    }
                }
            ),
        ]
    if not values.gateway:
        lines += [
            "",
            "# How inference reaches this cluster. The agent dials out to the plane's",
            "# gateway and nothing dials in, so there is nothing to open here, but a",
            "# cluster with no agent serves nothing. This plane has no gateway address",
            "# configured (HANGAR_TUNNEL_GATEWAY), so fill it in by hand as host:port,",
            "# e.g. hangar.example.com:443; ask for it instead of guessing, because a",
            "# wrong one is a tunnel that never establishes.",
            "#",
            "# Enable it together with `frontend.mode` on a later upgrade: the chart",
            "# refuses an agent with no frontend to forward to, and no frontend can go on",
            "# in the first install because its CRD is applied at runtime.",
            *_dump({"agent": {"enabled": False, "gateway": {"address": ""}}}),
        ]
    lines += [
        "",
        "# Serving ingress. Leave disabled until a Dynamo frontend Service exists to",
        "# point the IngressRoute at; the plane can render it later from ClusterServing.",
        *_dump({"serving": {"enabled": False}}),
    ]
    return "\n".join(lines) + "\n"


def _serving(row: ClusterServing | None) -> ClusterServingSchema | None:
    if row is None:
        return None
    return ClusterServingSchema(
        url=row.url,
        credential_id=str(row.credential_id) if row.credential_id else None,
        enabled=row.enabled,
    )


class ClusterService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ClusterRepository(db)

    def schema(self, cluster: Cluster) -> ClusterSchema:
        state = cluster.sync_state
        return ClusterSchema(
            id=cluster.uid,
            name=cluster.name,
            display_name=cluster.display_name,
            region=cluster.region,
            zone=cluster.zone,
            residency=cluster.residency,
            phase=cluster.phase,
            labels=cluster.labels,
            capabilities=cluster.capabilities,
            enrolled_at=cluster.enrolled_at,
            created_at=cluster.created_at,
            updated_at=cluster.updated_at,
            revision=_revision(cluster),
            reachable=bool(state and state.reachable),
            sync=_sync(state),
            serving=_serving(cluster.serving),
            metrics_label=cluster.metrics_label,
        )

    async def get(self, ref: str) -> ClusterSchema:
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        return self.schema(cluster)

    def _render_setup(self, cluster: Cluster, hub_url: str, enrolment_token: str | None) -> ClusterSetupSchema:
        """Builds the setup payload for a cluster, with or without a fresh token.

        The values file carries the plane-filled identity (cluster id, hub URL,
        region, operator image, and the fleet's one metrics write endpoint), plus
        a commented skeleton of the cluster's own infra facts (pool labels,
        storage class, HF token, registry token, serving) for the operator to
        edit in place. It is one downloadable file, not a merge. Helm pulls the
        chart from its own registry, so the instructions work even where the
        plane's ingress is not reachable from the operator's laptop.

        The command names `--kube-context` after the cluster instead of leaving
        the target implicit. Omitted, helm installs CRDs, webhooks and a
        privileged DaemonSet into whatever `kubectl` last pointed at. The
        cluster's name is a guess at the local context alias, but a wrong guess
        is `context "…" does not exist` before anything is applied. Every
        argument is shell-quoted, so a name the schema would refuse today but an
        older row still carries cannot add a flag or a command.

        `enrolment_token` is the freshly minted plaintext (mint only); it is
        folded into the values so the chart creates the operator Secret, and is
        never returned again. `enrolment_token_set` reflects the stored hash.

        The plane's own registry token is never rendered on a plain read. At mint
        it is rendered only when `IMAGE_PULL_SECRET_SHARE_TOKEN` opts in; by
        default a named pull secret is a blank the cluster fills with a
        read-only token of its own, so one cluster's values leaking does not hand
        out a credential every other cluster depends on.
        """
        share_token = bool(enrolment_token) and settings.IMAGE_PULL_SECRET_SHARE_TOKEN
        values_yaml = _setup_values(
            SetupValues(
                cluster_name=cluster.name,
                hub_url=hub_url,
                region=cluster.region,
                operator_image=settings.OPERATOR_IMAGE,
                agent_image=settings.AGENT_IMAGE,
                gateway=settings.TUNNEL_GATEWAY,
                remote_write_url=settings.METRICS_REMOTE_WRITE_URL,
                enrolment_token=enrolment_token,
                pull_secret_name=settings.IMAGE_PULL_SECRET_NAME,
                pull_secret_registry=settings.IMAGE_PULL_SECRET_REGISTRY,
                pull_secret_username=settings.IMAGE_PULL_SECRET_USERNAME,
                pull_secret_token=settings.IMAGE_PULL_SECRET_TOKEN if share_token else "",
            )
        )

        filename = values_filename(cluster.name)
        version = settings.SETUP_CHART_VERSION or None
        parts = [
            "helm upgrade --install hangar-cluster",
            shlex.quote(settings.SETUP_CHART_REF),
        ]
        if version:
            parts.append(f"--version {shlex.quote(version)}")
        parts += [
            f"--kube-context {shlex.quote(cluster.name)}",
            "--namespace hangar-system --create-namespace",
            f"-f {shlex.quote(filename)}",
        ]
        helm_command = " \\\n  ".join(parts)

        registry_login = None
        registry_login_gh = None
        if settings.SETUP_CHART_PRIVATE:
            host_ = shlex.quote(_registry_host(settings.SETUP_CHART_REF))
            registry_login = f"helm registry login {host_}"
            if host_ == "ghcr.io":
                registry_login_gh = (
                    f'gh auth token | helm registry login {host_} -u "$(gh api user -q .login)" --password-stdin'
                )

        return ClusterSetupSchema(
            helm_command=helm_command,
            values_yaml=values_yaml,
            values_filename=filename,
            chart_ref=settings.SETUP_CHART_REF,
            chart_version=version,
            registry_login=registry_login,
            registry_login_gh=registry_login_gh,
            enrolment_token=enrolment_token,
            enrolment_token_set=cluster.token_hash is not None,
        )

    async def setup_instructions(self, ref: str) -> ClusterSetupSchema:
        """The chart install a person runs to make `ref` hangar-ready (no token).

        The enrolment token is minted separately (`issue_enrolment_token`) and
        shown once; a plain read never carries it: `enrolment_token_set` tells
        the screen whether to offer "generate" or "rotate".
        """
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        return self._render_setup(cluster, _hub_url(), enrolment_token=None)

    async def issue_enrolment_token(self, ref: str) -> ClusterSetupSchema:
        """Mints (or rotates) `ref`'s enrolment token and returns it once.

        Stores only hashes; the plaintext is folded into the returned values and
        never recoverable afterwards. Rotating invalidates the previous token
        immediately: a hub session opened with it is closed within one ownership
        poll (`HUB_OWNERSHIP_CHECK_SECONDS`), and it can open no new one.

        Audited both ways: a `cluster.enrolment_token_issued` or `_rotated` event on
        the cluster's timeline, written in the same commit, and a warning log line.

        Two hashes, one credential. `token_hash` is the sha256 the hub looks the
        token up by and compares in constant time; `remote_write_hash` is a
        bcrypt verifier over the same string, which is the only shape Traefik's
        `basicAuth` middleware in front of the central metrics store can check
        (ADR 0012). Both are written here so rotation stays a single act.

        The hub URL is resolved before anything is written: a plane with no
        `HUB_PUBLIC_URL` refuses here instead of rotating a working cluster's
        token and then failing to say where to use the new one.
        """
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        hub_url = _hub_url()

        event = "enrolment_token_rotated" if cluster.token_hash else "enrolment_token_issued"
        token, token_hash = AuthManager.mint_enrolment_token()
        cluster.token_hash = token_hash
        cluster.remote_write_hash = AuthManager.hash_remote_write_token(token)
        await self.db.execute(
            statements.cluster_lifecycle(
                cluster.id,
                event,
                f"cluster {cluster.name} was given a new enrolment token; any earlier one stopped working",
                {"cluster_id": cluster.uid},
                dt.datetime.now(dt.UTC),
            )
        )
        await self.db.commit()
        logger.warning("cluster.%s cluster=%s", event, cluster.uid)
        await self.db.refresh(cluster)
        return self._render_setup(cluster, hub_url, enrolment_token=token)

    def _spend_tunnel_guess(self, source: str, message: str) -> NoReturn:
        """Charges one failed verification to `source`'s budget and refuses.

        Only failures reach here, which is the point: the budget is read on
        the way to a refusal and never on the way to an identity, so a cluster
        presenting a good token is never rate-limited by somebody else's bad one.

        A spent budget answers `429 rate_limited` instead of `401`, so a caller
        can slow down, but the detail still says `verdict: unknown_token`: the
        lookup has already happened, and a gateway must be able to drop a cached
        identity for a rotated token whether or not this source is throttled.
        """
        if TUNNEL_AUTH_GUESSES.spent(source):
            raise TooManyRequests(
                "too many failed agent verifications from this source; retry shortly",
                {"verdict": "unknown_token", "retry_after_seconds": int(TUNNEL_AUTH_GUESSES.window_seconds)},
            )
        TUNNEL_AUTH_GUESSES.fail(source)
        raise NotAuthorized(message, {"verdict": "unknown_token"})

    async def verify_tunnel_token(self, token: str, source: str = "") -> TunnelIdentitySchema:
        """Resolves a serving agent's enrolment token to the cluster it belongs to.

        A third verifier over the same credential the hub and the metrics write
        ingress already check (`issue_enrolment_token`), for the third thing a
        cluster dials out to: the gateway's tunnel listener. It
        mints nothing and stores nothing: the gateway asks who just
        connected, gets an identity or a refusal, and never holds a cluster's
        token material or a hash of it.

        The lookup is by digest and the digest is then compared in constant time.
        The second step is not redundant paranoia about the index: it keeps the
        decision that admits a cluster from depending on how the database compares
        a secret-derived value, which is the only comparison in this path that
        matters.

        Refusals are all `not_authorized` so the gateway branches on one code, but
        the messages differ because a person reading an agent's logs needs to
        know whether the token is wrong or the cluster is out of service. Only
        `disabled` is refused: a `draining` cluster is being emptied and its
        in-flight streams still need the tunnel they arrived on (its upstreams are
        already gone from `GET /v1/endpoints/resolved`), and a `pending` one may
        bring its agent up before its operator has completed a handshake.

        A wrong or unknown token spends from `source`'s failure budget (see
        `TUNNEL_AUTH_GUESSES`); a known token belonging to a disabled cluster
        does not. The budget is never consulted before the lookup: a cluster
        holding a stale token must not be able to refuse a cluster holding a
        good one.
        """
        if not AuthManager.looks_like_enrolment(token):
            self._spend_tunnel_guess(source, "not a cluster enrolment token")

        digest = AuthManager.hash_enrolment_token(token)
        cluster = await self.repo.by_token_hash(digest)
        if cluster is None or not hmac.compare_digest(cluster.token_hash or "", digest):
            logger.warning("cluster.tunnel_auth.refused reason=unknown_token")
            self._spend_tunnel_guess(source, "unknown cluster enrolment token")

        if cluster.phase == "disabled":
            logger.warning("cluster.tunnel_auth.refused cluster=%s reason=disabled", cluster.uid)
            raise NotAuthorized(f"cluster {cluster.name!r} is disabled", {"verdict": "cluster_disabled"})

        return TunnelIdentitySchema(cluster_id=cluster.uid, cluster_name=cluster.name)

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        region: str | None = None,
        phase: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[ClusterSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            region=region,
            phase=phase,
            search=search,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda c: c.seq)
        items = [self.schema(c) for c in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def rollup(self, ref: str) -> ClusterRollupSchema:
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        rows = await self.repo.rollup([cluster.id])
        return _rollup(cluster.uid, rows.get(cluster.id))

    async def headroom(self, ref: str) -> ClusterHeadroomSchema:
        return (await self.rollup(ref)).headroom

    async def rollups(self, limit: int | None = None, cursor: str | None = None) -> ListResponse[ClusterRollupSchema]:
        size = clamp_limit(limit)
        pairs = await self.repo.all_ids(limit=size + 1, after_seq=decode_cursor(cursor))
        kept, next_cursor = page(pairs, size, lambda pair: pair.seq)
        rows = await self.repo.rollup([pair.id for pair in kept])
        items = [_rollup(pair.uid, rows.get(pair.id)) for pair in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: ClusterCreateRequest) -> ClusterSchema:
        """Registers a cluster, labelling it for the metrics store by its own name.

        The label is not asked for a second time because it is not a second fact:
        the setup values the plane renders carry this name as `cluster.id`, and the
        chart's metrics agent stamps `external_labels.cluster` from it. An explicit
        null is still accepted, for a cluster whose store label is not yet known.
        """
        if data.phase not in PHASES:
            raise Invalid(f"unknown phase {data.phase!r}", {"allowed": sorted(PHASES)})
        if data.name in RESERVED_NAMES:
            raise Invalid(f"{data.name!r} is a reserved name", {"reserved": sorted(RESERVED_NAMES)})
        metrics_label = data.name if data.metrics_label is UNSET else data.metrics_label
        cluster = Cluster(
            uid=new_uid(CLUSTER),
            name=data.name,
            display_name=data.display_name,
            region=data.region,
            zone=data.zone,
            residency=data.residency,
            phase=data.phase,
            labels=data.labels,
            capabilities=data.capabilities,
            metrics_label=metrics_label or None,
        )
        try:
            await self.repo.add(cluster)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"cluster {data.name!r} already exists") from exc
        return await self.get(cluster.uid)

    async def update(self, ref: str, data: ClusterUpdateRequest, if_match: str | None = None) -> ClusterSchema:
        cluster = await self.repo.get(ref, lock=True)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        if if_match is not None and if_match != _revision(cluster):
            raise Conflict("cluster changed since it was read", {"revision": _revision(cluster)})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        if "phase" in fields and fields["phase"] not in PHASES:
            raise Invalid(f"unknown phase {fields['phase']!r}", {"allowed": sorted(PHASES)})
        if fields.get("metrics_label") == "":
            fields["metrics_label"] = None
        for key, value in fields.items():
            setattr(cluster, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("cluster conflicts with an existing one") from exc
        return await self.get(cluster.uid)

    async def delete(self, ref: str, if_match: str | None = None, force: bool = False) -> None:
        """Removes a cluster's registry row and everything it reported about itself.

        Nodes, devices, pools and sync state go with the row. Operations, benchmark
        runs, events and alerts stay, with no cluster, because they are the history
        of why it was drained. Operations and runs still open are cancelled with
        the delete, since no cluster can report on them afterwards.

        Two guards, because those foreign keys cascade and nothing else would ask.
        The caller must name the revision it looked at (`If-Match`) or say
        `force`, so a delete aimed at what a stale screen showed is refused. And
        a cluster that still has deployments, unreleased allocations or pinned
        workloads refuses either way, `force` included: those are running
        things, and the way to remove them is to drain the cluster first. The
        refusal names each count.
        """
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        if if_match is None and not force:
            raise Invalid(
                "deleting a cluster needs If-Match with its revision, or force=true",
                {"revision": _revision(cluster)},
            )
        if if_match is not None and if_match != _revision(cluster):
            raise Conflict("cluster changed since it was read", {"revision": _revision(cluster)})
        holds = await self.repo.holds(cluster.id)
        if any(holds.values()):
            held = ", ".join(f"{count} {kind.replace('_', ' ')}" for kind, count in holds.items() if count)
            raise Conflict(f"cluster {cluster.name!r} still has {held}; drain it first", holds)
        await self.repo.delete(cluster)
        await self.db.commit()

    async def set_serving(self, ref: str, data: ClusterServingRequest) -> ClusterSchema:
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")

        url = data.url.strip()
        if not url:
            raise Invalid("serving url is required")

        credential_id = None
        if data.credential_id is not None:
            try:
                credential_id = UUID(data.credential_id)
            except ValueError as exc:
                raise Invalid(f"malformed credential id {data.credential_id!r}") from exc
            credential = await self.repo.credential(credential_id)
            if credential is None:
                raise Invalid(f"no credential {data.credential_id!r}")
            if credential.revoked_at is not None:
                raise Invalid(f"credential {data.credential_id!r} is revoked")

        row = cluster.serving
        if row is None:
            row = ClusterServing(cluster_id=cluster.id, url=url, credential_id=credential_id, enabled=data.enabled)
            cluster.serving = row
            await self.repo.set_serving(cluster, row)
        else:
            row.url = url
            row.credential_id = credential_id
            row.enabled = data.enabled

        await self.db.commit()
        return await self.get(cluster.uid)

    async def clear_serving(self, ref: str) -> None:
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        if cluster.serving is None:
            return
        await self.repo.clear_serving(cluster.serving)
        cluster.serving = None
        await self.db.commit()
