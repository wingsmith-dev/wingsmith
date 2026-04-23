import datetime as dt

from uuid import UUID

from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.ids import CLUSTER
from app.core.ids import new_uid
from app.core.auth import mint_enrolment_token
from app.db.models import Cluster
from app.db.models import ClusterServing
from app.db.models import ClusterSyncState
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import ClusterSchema
from .schemas import ClusterSyncSchema
from .schemas import ClusterSetupSchema
from .schemas import ClusterRollupSchema
from .schemas import ClusterCreateRequest
from .schemas import ClusterServingSchema
from .schemas import ClusterUpdateRequest
from .schemas import ClusterHeadroomSchema
from .schemas import ClusterServingRequest
from .repository import ClusterRepository


PHASES = frozenset({"pending", "active", "draining", "disabled"})


def _revision(cluster: Cluster) -> str:
    """The value `If-Match` is checked against.

    Derived from the last write rather than stored, because `updated_at` already
    moves on every update and a second column tracking the same fact is a second
    column that can disagree with it.
    """
    stamp = cluster.updated_at or cluster.created_at
    return str(int(stamp.timestamp() * 1_000_000))


STALE_AFTER = dt.timedelta(minutes=40)
"""How long a connected cluster may go without sending anything before its link
is reported `stale` rather than `connected`.

The failure this exists for is an operator whose observation loop stopped while
its socket stayed up: `reachable` is true, because the socket is the only thing
it ever measured, and every number the console draws from that cluster is frozen
with nothing saying so.

The threshold is tied to the operator's resync interval and must stay above it.
`last_sync_at` moves only when a frame is *ingested*, and the observation loop
deliberately sends nothing when nothing has changed — so on a quiet cluster the
only thing that reliably bumps it is the periodic resync, currently 15 minutes
(`HANGAR_RESYNC_INTERVAL`). Forty minutes is two missed resyncs plus room for a
slow one. A threshold below the resync interval would mark every healthy idle
cluster stale, which is worse than the silence it is meant to catch: an
indicator that cries wolf on a working fleet is one nobody reads when it is
right. Heartbeats are deliberately not counted — `last_seen_at` tracks those,
and a live socket carrying no observations is precisely the state this reports."""


def _sync(state: ClusterSyncState | None) -> ClusterSyncSchema:
    if state is None or state.connected_at is None:
        return ClusterSyncSchema(
            state="never_connected",
            last_sync_at=None,
            last_seen_at=None,
            agent_version=state.agent_version if state else None,
            lag_ms=None,
            last_error=state.last_error if state else None,
            distribution=state.distribution if state else None,
            cloud=state.cloud if state else None,
            observed_region=state.observed_region if state else None,
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
        agent_version=state.agent_version,
        lag_ms=lag,
        last_error=state.last_error,
        distribution=state.distribution,
        cloud=state.cloud,
        observed_region=state.observed_region,
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


def _hub_url(host: str) -> str:
    """The `wss://` URL the operator dials, written into the setup values.

    An explicit `HUB_PUBLIC_URL` wins; otherwise `wss://{PUBLIC_HOST or host}` +
    the hub path, so a command rendered behind a port-forward still names the real
    origin while local and tests fall back to the request host.
    """
    if settings.HUB_PUBLIC_URL:
        return settings.HUB_PUBLIC_URL
    public = settings.PUBLIC_HOST or host
    return f"wss://{public}{settings.HUB_PATH}"


def _registry_host(chart_ref: str) -> str:
    """The registry host to `helm registry login`, from an OCI chart reference.

    `oci://ghcr.io/hangar-dev/charts/cluster-setup` → `ghcr.io`.
    """
    return chart_ref.removeprefix("oci://").split("/", 1)[0]


def _setup_values(cluster_name: str, hub_url: str, operator_image: str, enrolment_token: str | None = None) -> str:
    """The full editable `values.yaml` for the chart, as YAML text.

    The top block is filled by the plane — cluster identity, `readOnly`, the
    pinned operator image, and (at mint only) the enrolment token. `readOnly`
    sits there rather than below the divider because it must share the one
    `cluster:` key; a second mapping would silently shadow the identity it
    carries. Below a divider is the cluster's own infra facts left blank with a
    one-line comment each: pool labels, the RWX storage class, the HF token,
    serving. The operator downloads this one file, edits the blanks, and runs
    helm against it — no separate local values file and no need to know the
    chart's schema. The chart refuses an install with any of those blanks still
    empty, so a half-edited file fails at `helm upgrade` rather than at the pod.

    The HF token is the one blank that is opt-in rather than required: whether a
    cluster needs one depends on which models land there, and the operator mounts
    `hf-token-secret` with `Optional: true` in all three of its renders. So
    `create` is emitted false — asking for the secret is what makes the token
    mandatory, and a cluster serving open weights never has to think about it.

    The operator image is pinned from `OPERATOR_IMAGE` when set so an install
    records what it ran; unset leaves the chart's own appVersion. When
    `enrolment_token` is given (mint only) it is folded in as
    `enrolmentToken.value` so the chart creates the operator Secret; it is never
    persisted, so a later render omits it and the chart then requires either a
    fresh mint or an explicit `enrolmentToken.existing=true`.
    """
    lines = [
        "# Filled by the plane — cluster identity. Leave as-is, except `readOnly`.",
        "cluster:",
        f"  id: {cluster_name}",
        f"  hubUrl: {hub_url}",
        "  # Whether the operator may act on what the plane declares. It installs",
        "  # read-only, reconciling nothing; set false once this cluster is managed.",
        "  readOnly: true",
        "  allowedOperations: benchmark",
    ]
    if operator_image:
        repository, _, tag = operator_image.rpartition(":")
        if repository and tag:
            lines += [
                "operator:",
                "  image:",
                f"    repository: {repository}",
                f"    tag: {tag}",
            ]
    if enrolment_token:
        lines += [
            "",
            "# Minted for this cluster and shown once. The chart creates the operator Secret from it.",
            "enrolmentToken:",
            f"  value: {enrolment_token}",
        ]
    lines += [
        "",
        "# ── Edit below: this cluster's own infra facts ─────────────────────────",
        "",
        "# How this cluster labels its node pools. `cacheValue` must name a pool that",
        "# exists, or the model-prefetch jobs stay Pending.",
        "pools:",
        '  labelKey: ""     # e.g. example.com/pool',
        '  gpuValue: ""     # the pool holding the GPU nodes, e.g. gpu',
        '  cacheValue: ""   # the pool prefetch jobs run on, e.g. system',
        "",
        "# The shared model-weight cache. Must be a ReadWriteMany class (a default",
        "# class is usually RWO and breaks the shared mount) — e.g. an EFS-backed one.",
        "modelCache:",
        '  storageClass: ""   # e.g. efs-models',
        "  accessMode: ReadWriteMany",
        "  size: 500Gi",
        "",
        "# Hugging Face token, only for gated weights. The operator mounts",
        "# hf-token-secret optionally, so leaving this off costs nothing until a model",
        "# needs it — flip `create` and fill `value` then.",
        "hfToken:",
        "  create: false",
        '  value: ""   # a Hugging Face token',
        "",
        "# Serving ingress. Leave disabled until a Dynamo frontend Service exists to",
        "# point the IngressRoute at; the plane can render it later from ClusterServing.",
        "serving:",
        "  enabled: false",
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
        )

    async def get(self, ref: str) -> ClusterSchema:
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        return self.schema(cluster)

    def _render_setup(self, cluster: Cluster, host: str, enrolment_token: str | None) -> ClusterSetupSchema:
        """Builds the setup payload for a cluster, with or without a fresh token.

        The `values.yaml` carries the plane-filled identity — cluster id, hub
        URL, operator image — plus a commented skeleton of the cluster's own
        infra facts (pool labels, storage class, HF token, serving) for the
        operator to edit in place. It is one downloadable file, not a merge.
        Helm pulls the chart from its own registry, so the instructions work
        even where the plane's ingress is not reachable from the operator's laptop.

        The command names `--kube-context` after the cluster rather than leaving
        the target implicit. Omitted, helm installs a tier-2 — CRDs, webhooks, a
        privileged DaemonSet — into whatever `kubectl` last pointed at, which is
        as likely to be the plane's own cluster as the intended one, and nothing
        about the output says which it was. The cluster's name is a guess at the
        local context alias, but a wrong guess is `context "…" does not exist`
        before anything is applied, which is the failure worth having.

        `enrolment_token` is the freshly minted plaintext (mint only); it is
        folded into the values so the chart creates the operator Secret, and is
        never returned again. `enrolment_token_set` reflects the stored hash.
        """
        hub_url = _hub_url(host)
        values_yaml = _setup_values(cluster.name, hub_url, settings.OPERATOR_IMAGE, enrolment_token)

        version = settings.SETUP_CHART_VERSION or None
        parts = [
            "helm upgrade --install hangar-cluster",
            settings.SETUP_CHART_REF,
        ]
        if version:
            parts.append(f"--version {version}")
        parts += [
            f"--kube-context {cluster.name}",
            "--namespace hangar-system --create-namespace",
            "-f values.yaml",
        ]
        helm_command = " \\\n  ".join(parts)

        registry_login = None
        registry_login_gh = None
        if settings.SETUP_CHART_PRIVATE:
            host_ = _registry_host(settings.SETUP_CHART_REF)
            registry_login = f"helm registry login {host_}"
            if host_ == "ghcr.io":
                registry_login_gh = (
                    f'gh auth token | helm registry login {host_} -u "$(gh api user -q .login)" --password-stdin'
                )

        return ClusterSetupSchema(
            helm_command=helm_command,
            values_yaml=values_yaml,
            chart_ref=settings.SETUP_CHART_REF,
            chart_version=version,
            registry_login=registry_login,
            registry_login_gh=registry_login_gh,
            enrolment_token=enrolment_token,
            enrolment_token_set=cluster.token_hash is not None,
        )

    async def setup_instructions(self, ref: str, host: str) -> ClusterSetupSchema:
        """The chart install a person runs to make `ref` hangar-ready (no token).

        The enrolment token is minted separately (`issue_enrolment_token`) and
        shown once; a plain read never carries it — `enrolment_token_set` tells
        the screen whether to offer "generate" or "rotate".
        """
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        return self._render_setup(cluster, host, enrolment_token=None)

    async def issue_enrolment_token(self, ref: str, host: str) -> ClusterSetupSchema:
        """Mints (or rotates) `ref`'s enrolment token and returns it once.

        Stores only the hash; the plaintext is folded into the returned values
        and never recoverable afterwards. Rotating invalidates the previous
        token immediately — an operator still holding it stops authenticating on
        its next reconnect, which is the point.
        """
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")

        token, token_hash = mint_enrolment_token()
        cluster.token_hash = token_hash
        await self.db.commit()
        await self.db.refresh(cluster)
        return self._render_setup(cluster, host, enrolment_token=token)

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

    async def rollups(self) -> ListResponse[ClusterRollupSchema]:
        pairs = await self.repo.all_ids()
        rows = await self.repo.rollup([pair.id for pair in pairs])
        items = [_rollup(pair.uid, rows.get(pair.id)) for pair in pairs]
        return ListResponse(items=items, count=len(items), next_cursor=None)

    async def create(self, data: ClusterCreateRequest) -> ClusterSchema:
        if data.phase not in PHASES:
            raise Invalid(f"unknown phase {data.phase!r}", {"allowed": sorted(PHASES)})
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
        )
        try:
            await self.repo.add(cluster)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"cluster {data.name!r} already exists") from exc
        return await self.get(cluster.uid)

    async def update(self, ref: str, data: ClusterUpdateRequest, if_match: str | None = None) -> ClusterSchema:
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        if if_match is not None and if_match != _revision(cluster):
            raise Conflict("cluster changed since it was read", {"revision": _revision(cluster)})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        if "phase" in fields and fields["phase"] not in PHASES:
            raise Invalid(f"unknown phase {fields['phase']!r}", {"allowed": sorted(PHASES)})
        for key, value in fields.items():
            setattr(cluster, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("cluster conflicts with an existing one") from exc
        return await self.get(cluster.uid)

    async def delete(self, ref: str) -> None:
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
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
