"""Declared state, read out of Postgres and shaped into wire payloads.

The other direction of the same seam. Ingestion writes what a cluster observed;
this reads what the plane declared for it, and the hub streams it down on
connect or resync. It is read-only by construction: nothing in this module
opens a transaction or writes a row.

A generator rather than a list. A full resync of a large cluster is thousands of
objects and the session emits them one at a time under backpressure, so
materialising the snapshot first would cost the memory and buy nothing.
"""

import datetime as dt

from uuid import UUID
from typing import Any
from typing import Literal
from logging import getLogger
from collections.abc import Sequence
from collections.abc import AsyncIterator

from msgspec import Struct
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pool
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import WarmSet
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import Component
from app.db.models import Deployment
from app.db.models import WorkloadRole
from app.db.models import PriorityClass
from app.db.models import ArtifactLocation
from app.core.serving import declared_router
from app.core.serving import output_modality
from app.core.serving import declared_served_model
from app.hub.messages import Adopt
from app.hub.messages import Overlay
from app.hub.messages import RoleSpec
from app.hub.messages import Resources
from app.hub.messages import RouterSpec
from app.hub.messages import Parallelism
from app.hub.messages import ServingSpec
from app.hub.messages import CacheDesired
from app.hub.messages import DiffusionSpec
from app.hub.messages import ArtifactSource
from app.hub.messages import CacheWarmEntry
from app.hub.messages import ArtifactDesired
from app.hub.messages import ComponentDesired
from app.hub.messages import DeploymentDeleted
from app.hub.messages import DeploymentDesired
from app.hub.messages import PlacementConstraints
from app.ingestion.time import now
from app.ingestion.statements import cluster_ref_clause
from app.modules.workloads.services import resolved_engine_args


logger = getLogger(__name__)


SUPPLY_SCOPES = frozenset({"supply", "all"})
DEMAND_SCOPES = frozenset({"demand", "all"})

SOURCE_KINDS = {"s3": "s3", "https": "https", "http": "https", "oci": "oci", "hf": "hf"}


def format_ts(moment: dt.datetime | None) -> str:
    """The wire timestamp form: RFC 3339 UTC with exactly milliseconds."""
    value = (moment or now()).astimezone(dt.UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


async def stream_desired(db: AsyncSession, cluster_uid: str, scope: str) -> AsyncIterator[tuple[str, Struct]]:
    """Yields `(type, payload)` covering the declared state one cluster needs.

    Order matters to the receiver: artifacts precede the deployments that
    reference them, so a cluster applying the stream in order never holds a
    deployment pointing at weights it has not been told about.
    """
    cluster_id = await db.scalar(select(Cluster.id).where(cluster_ref_clause(cluster_uid)))
    if cluster_id is None:
        return

    if scope in SUPPLY_SCOPES:
        for payload in await _components(db, cluster_uid, cluster_id):
            yield "component.desired", payload

    if scope in DEMAND_SCOPES:
        for payload in await _artifacts(db, cluster_uid, cluster_id):
            yield "artifact.desired", payload
        for payload in await _deployments(db, cluster_uid, cluster_id):
            yield "deployment.desired", payload
        yield "cache.desired", await _cache(db, cluster_uid, cluster_id)


async def desired_object(
    db: AsyncSession, cluster_uid: str, kind: str, uid: str, generation: int = 0
) -> tuple[str, Struct] | None:
    """One declared object, rendered exactly the way a full resync renders it.

    Live dispatch needs a single frame where resync needs the whole snapshot, and
    the two must never disagree: a deployment that reaches a cluster one way on
    connect and a different way on a write is a divergence that only shows up
    once something is already serving the wrong thing. So this narrows the same
    query the snapshot runs rather than standing a second renderer beside it.

    Absence is an answer, not a failure. A deployment that is gone — deleted, or
    disabled, or moved to another cluster — renders as `deployment.deleted`,
    because from the cluster's side those are one fact: stop running this. The
    generation is the caller's, since there is no row left to read it from.

    Returns `None` only when there is nothing this seam can say — an unknown
    cluster, or a kind live dispatch does not carry yet.

    This renders the changed object alone. `desired_frames` is what dispatch
    calls, because an object is not always sendable on its own.
    """
    cluster_id = await db.scalar(select(Cluster.id).where(cluster_ref_clause(cluster_uid)))
    if cluster_id is None:
        return None

    if kind != "deployment":
        return None

    found = await _deployments(db, cluster_uid, cluster_id, only=uid)
    if found:
        return "deployment.desired", found[0]
    return "deployment.deleted", DeploymentDeleted(
        deployment_id=uid,
        cluster_id=cluster_uid,
        generation=generation,
        deleted_at=format_ts(None),
    )


async def desired_frames(
    db: AsyncSession, cluster_uid: str, kind: str, uid: str, generation: int = 0
) -> list[tuple[str, Struct]]:
    """One changed object, preceded by whatever it cannot be understood without.

    A deployment names an artifact whose weights have to be resident before a
    replica may start. A resync sends both in that order and says why in
    `snapshot`; live dispatch used to send the deployment alone, on the reasoning
    that everything else was already there. It is not, and the first deployment of
    a newly registered artifact is exactly when it is not: an artifact is
    announced to the clusters that need it, and no cluster needed it until this
    deployment existed.

    So a hint about a deployment resolves to its dependency chain, in the same
    order and from the same queries the snapshot uses. Re-sending an object the
    cluster already has is free — the stream is full-object and idempotent by
    construction (PROTOCOL §6) — and is the cheap side of the trade against a
    deployment stranded until the next reconnect.

    The chain is one hop shorter than it was. A deployment used to name a recipe
    as well, which is where the sharper version of this failure lived: the recipe
    was announced to no cluster when it was created, nothing re-announced it once
    a deployment named it, and a resume could strand a deployment on a recipe it
    was never sent. The serving configuration is inlined on the deployment now, so
    there is nothing left to arrive out of order.

    A `deployment.deleted` carries the warm set after it, and only the warm set.
    The artifact frame is deliberately not withdrawn — an artifact is a
    description of some weights and describing them costs a cluster nothing, so
    there is no frame that unsays one and no reason to want one. The warm set is
    different: it is the statement that this cluster *must hold* those weights,
    the operator turns it into a running download, and the download is the thing
    that outlives the workload if nobody says otherwise.

    It used to travel alone, on the reading that the deletion was the whole of
    what the cluster needed to hear. It was not. Deleting a workload removed the
    deployment and left the prefetch job pulling weights for it, visible in
    `kubectl get pods` under the name of a model nothing had asked for since,
    until the next reconnect happened to resend a warm set — which for the last
    workload on a cluster resent nothing at all.
    """
    cluster_id = await db.scalar(select(Cluster.id).where(cluster_ref_clause(cluster_uid)))
    if cluster_id is None:
        return []

    if kind != "deployment":
        one = await desired_object(db, cluster_uid, kind, uid, generation)
        return [one] if one else []

    found = await _deployments(db, cluster_uid, cluster_id, only=uid)
    if not found:
        gone: list[tuple[str, Struct]] = [
            (
                "deployment.deleted",
                DeploymentDeleted(
                    deployment_id=uid,
                    cluster_id=cluster_uid,
                    generation=generation,
                    deleted_at=format_ts(None),
                ),
            )
        ]
        gone.append(("cache.desired", await _cache(db, cluster_uid, cluster_id)))
        return gone

    deployment = found[0]
    frames: list[tuple[str, Struct]] = []

    artifact_uid = deployment.artifact_ids[0] if deployment.artifact_ids else None
    if artifact_uid is not None:
        for artifact in await _artifacts(db, cluster_uid, cluster_id):
            if artifact.artifact_id == artifact_uid:
                frames.append(("artifact.desired", artifact))
                break

    frames.append(("cache.desired", await _cache(db, cluster_uid, cluster_id)))

    frames.append(("deployment.desired", deployment))
    return frames


async def _components(db: AsyncSession, cluster_uid: str, cluster_id: UUID) -> list[ComponentDesired]:
    rows = await db.execute(select(Component).where(Component.cluster_id == cluster_id).order_by(Component.seq))
    return [
        ComponentDesired(
            component_id=row.uid,
            cluster_id=cluster_uid,
            name=row.name,
            version=row.desired_version,
            enabled=row.enabled,
            config={key: str(value) for key, value in (row.config or {}).items()},
            updated_at=format_ts(row.updated_at or row.created_at),
        )
        for row in rows.scalars()
    ]


def _artifact_ids_query(cluster_id: UUID):
    """Every artifact a cluster needs: its warm set, plus what its deployments serve."""
    warm = select(WarmSet.artifact_id).where(WarmSet.cluster_id == cluster_id)
    served = (
        select(Workload.artifact_id)
        .join(Deployment, Deployment.workload_id == Workload.id)
        .where(Deployment.cluster_id == cluster_id)
    )
    return warm.union(served)


async def _artifacts(db: AsyncSession, cluster_uid: str, cluster_id: UUID) -> list[ArtifactDesired]:
    needed = _artifact_ids_query(cluster_id).subquery()
    rows = await db.execute(select(Artifact).where(Artifact.id.in_(select(needed.c[0]))).order_by(Artifact.seq))
    artifacts = list(rows.scalars())
    if not artifacts:
        return []

    located = await db.execute(
        select(ArtifactLocation)
        .where(ArtifactLocation.artifact_id.in_([a.id for a in artifacts]))
        .order_by(ArtifactLocation.preferred.desc(), ArtifactLocation.seq)
    )
    sources: dict[UUID, list[ArtifactSource]] = {}
    for location in located.scalars():
        bucket = sources.setdefault(location.artifact_id, [])
        bucket.append(
            ArtifactSource(
                kind=SOURCE_KINDS.get(location.scheme, "https"),
                uri=location.uri,
                priority=0 if location.preferred else len(bucket),
                credential_ref=str(location.credential_id) if location.credential_id else None,
            )
        )

    return [
        ArtifactDesired(
            artifact_id=artifact.uid,
            cluster_id=cluster_uid,
            kind=artifact.kind,
            digest=artifact.digest,
            format=artifact.format or "",
            size_bytes=artifact.size_bytes,
            licence=artifact.licence or "",
            sources=sources.get(artifact.id, []),
            updated_at=format_ts(artifact.updated_at or artifact.created_at),
        )
        for artifact in artifacts
    ]


def _serving(workload: Workload, artifact: Artifact, roles: Sequence[WorkloadRole] = ()) -> ServingSpec:
    """The engine configuration as the cluster is told it, read off the workload.

    Inlined on the deployment rather than sent as an object of its own; the
    reasoning lives on `ServingSpec` in `app.hub.messages`.

    The device shape is deliberately not here. It reaches the cluster on the
    deployment's `placement`, derived from these same columns, and putting it in
    both places would be two copies of one fact on one wire.

    `frontend_image` has an order too, and it is one step long: the workload's own
    declaration when it has one, otherwise empty. Empty means the cluster
    decides, and it does not mean the engine image — a frontend and a worker are
    separately versioned binaries, which is the entire reason the field exists, so
    falling back to `engine_runtime_image` here would hand every model the one image the Kimi
    K3 frontend fix exists to avoid. The default belongs to the operator because only
    the operator knows what the cluster it runs in serves through.
    """
    fraction = workload.kv_cache_fraction
    return ServingSpec(
        engine=workload.engine,
        engine_runtime_version=workload.engine_runtime_version,
        engine_runtime_image=workload.engine_runtime_image,
        # The same two values under the names an operator older than the rename reads. It
        # is a silent stall rather than a rejected frame if it finds neither, so both
        # spellings travel until no such operator is left.
        engine_version=workload.engine_runtime_version,
        engine_image=workload.engine_runtime_image,
        frontend_image=workload.frontend_image,
        engine_args=resolved_engine_args(workload, artifact),
        quantization=workload.quantization,
        dtype=workload.dtype,
        parallelism=Parallelism(
            tensor=workload.tensor_parallel,
            pipeline=workload.pipeline_parallel,
            expert=workload.expert_parallel,
        ),
        context_length=workload.context_length,
        max_batch_tokens=workload.max_batch_tokens,
        kv_cache_dtype=workload.kv_cache_dtype,
        kv_cache_fraction=float(fraction) if fraction is not None else None,
        prefix_cache_enabled=workload.prefix_cache_enabled,
        prefix_cache_config={key: str(value) for key, value in (workload.prefix_cache_config or {}).items()},
        resources=Resources(
            cpu_cores=workload.cpu_cores,
            memory_mib=workload.memory_mib,
            disk_gib=workload.disk_gib,
        ),
        digest=workload.serving_digest,
        topology=workload.serving_topology if workload.serving_topology == "disaggregated" else "aggregated",
        roles=[
            RoleSpec(
                role=role.role,
                max_batch_tokens=role.max_batch_tokens,
                engine_args={key: str(value) for key, value in (role.engine_args or {}).items()},
            )
            for role in roles
        ],
        overlay=Overlay(
            pod_spec=(workload.overlay or {}).get("pod_spec", {}) or {},
            dynamo=(workload.overlay or {}).get("dynamo", {}) or {},
        ),
        router=_router(workload),
        output_modality=_output_modality(workload),
        diffusion=_diffusion(workload),
    )


def _output_modality(workload: Workload) -> Literal["image", "video"] | None:
    """What an omni worker emits, or None for text."""
    modality = output_modality(workload.serving)
    if modality == "image":
        return "image"
    if modality == "video":
        return "video"
    return None


def _diffusion(workload: Workload) -> DiffusionSpec | None:
    """The stage configuration as the cluster is told it, or None when none is declared."""
    declared = (workload.serving or {}).get("diffusion")
    if not declared:
        return None
    return DiffusionSpec(
        model_class_name=declared.get("model_class_name") or None,
        default_sampling_params=dict(declared.get("default_sampling_params") or {}),
    )


def _router(workload: Workload) -> RouterSpec | None:
    """The frontend router settings as the cluster is told them, or None when none are declared.

    None rather than an empty block, so a workload that declares nothing sends what it sent before
    the block existed and "declared nothing" has one spelling on the wire.
    """
    declared = declared_router(workload.router)
    return RouterSpec(**declared) if declared else None


def _role_replicas(deployment: Deployment, roles: Sequence[WorkloadRole]) -> dict[str, int]:
    """This cluster's share, multiplied out into an absolute count per role.

    **The plane multiplies and the operator never does.** Under a disaggregated topology
    `min_replicas` counts groups, and a group is the per-role weights taken together, so a role's
    worker count is `min_replicas x weight`. The operator is forbidden this arithmetic by its own
    contract — `Replicas()` says it "renders exactly the number it is sent, and must never sum,
    scale, or re-derive it against anything else" — and that invariant is currently absolute and
    cheap to check. Spending it to save one map on a frame the plane already sends would be a bad
    trade, and it is the kind of exception that is never the last one.

    Empty for an aggregated deployment, where `min_replicas` is the whole answer and an empty map
    is what says so.
    """
    if not roles:
        return {}
    share = max(deployment.min_replicas, 0)
    return {role.role: share * max(role.weight, 0) for role in roles}


def _placement(workload: Workload, pools: list[str]) -> PlacementConstraints:
    """The constraints one cluster is told to honour when it places replicas.

    Read entirely off the workload, which is now the only declarer of any of it.
    `requires_single_topology_domain` used to be a nullable override layered over
    a recipe's own requirement, so a null here meant "the recipe decides" rather
    than "no". There is one level left, so the column is the answer.
    """
    topology = "single_domain" if workload.requires_single_topology_domain else "none"
    return PlacementConstraints(
        device_class=workload.device_class,
        devices_per_replica=workload.device_count,
        min_device_memory_mb=workload.device_memory_mib_min,
        topology=topology,
        allowed_pools=pools,
        node_selector={},
    )


async def _deployments(
    db: AsyncSession, cluster_uid: str, cluster_id: UUID, only: str | None = None
) -> list[DeploymentDesired]:
    """The deployments one cluster owns, each carrying only that cluster's share.

    Serving configuration, adapters, lifecycle, project and priority are facts
    about what was asked for and live on the parent `Workload`, so every one of
    them is a join away rather than a column here.

    `min_replicas` and `max_replicas` are read straight off the deployment and
    are THIS CLUSTER'S SHARE of the workload's total. The total is never on the
    wire and must not become derivable from it: a cluster sees one deployment
    and cannot tell a whole workload from a fragment of one, which is the point.
    """
    query = (
        select(
            Deployment,
            Workload,
            Project.uid,
            PriorityClass.name,
            Artifact,
        )
        .join(Workload, Deployment.workload_id == Workload.id)
        .join(Project, Workload.project_id == Project.id)
        .join(PriorityClass, Workload.priority_class_id == PriorityClass.id)
        .join(Artifact, Workload.artifact_id == Artifact.id)
        .where(Deployment.cluster_id == cluster_id, Deployment.enabled == True)
        .order_by(Deployment.seq)
    )
    if only is not None:
        query = query.where(Deployment.uid == only)
    rows = (await db.execute(query)).all()
    if not rows:
        return []

    pools = await db.execute(select(Pool.name).where(Pool.cluster_id == cluster_id, Pool.enabled == True))
    allowed = list(pools.scalars())

    # One query for the page, not one per deployment. Ordered by role so two builds of the same
    # frame agree: the operator renders the components in the order they arrive, and Dynamo hashes
    # the components together — a re-ordering here would rename every worker without anything
    # having changed.
    role_rows = (
        await db.execute(
            select(WorkloadRole)
            .where(WorkloadRole.workload_id.in_({row[1].id for row in rows}))
            .order_by(WorkloadRole.role)
        )
    ).scalars()
    roles_by_workload: dict[UUID, list[WorkloadRole]] = {}
    for role in role_rows:
        roles_by_workload.setdefault(role.workload_id, []).append(role)

    adapters = {
        artifact_id: uid
        for artifact_id, uid in (
            await db.execute(
                select(Artifact.id, Artifact.uid).where(
                    Artifact.id.in_({a for row in rows for a in (row[1].adapter_artifact_ids or [])})
                )
            )
        ).all()
    }

    out: list[DeploymentDesired] = []
    for row in rows:
        deployment, workload, project_uid, priority, artifact = row[:5]
        artifact_ids = [artifact.uid] + [adapters[a] for a in (workload.adapter_artifact_ids or []) if a in adapters]
        out.append(
            DeploymentDesired(
                deployment_id=deployment.uid,
                name=deployment.name,
                cluster_id=cluster_uid,
                project_id=project_uid,
                lifecycle=workload.kind,
                priority_class=priority,
                min_replicas=deployment.min_replicas,
                max_replicas=deployment.max_replicas,
                placement=_placement(workload, allowed),
                serving=_serving(workload, artifact, roles_by_workload.get(workload.id, [])),
                role_replicas=_role_replicas(deployment, roles_by_workload.get(workload.id, [])),
                artifact_ids=artifact_ids,
                served_model_name=declared_served_model(workload, artifact),
                generation=deployment.generation,
                updated_at=format_ts(deployment.updated_at or deployment.created_at),
                authority=deployment.authority if deployment.authority in ("observed", "managed") else "observed",
                adopt=_adopt(deployment),
            )
        )
    return out


def _adopt(deployment: Deployment) -> Adopt | None:
    """The adoption block, when this deployment was promoted from a running workload.

    Read entirely off the deployment. It used to be assembled from an outer join
    onto the `DiscoveredWorkload` the promotion came from, and that made a
    declared fact depend on an observed row: the supply snapshot prunes a sighting
    the moment a cluster stops mentioning it, so any frame rendered inside that
    window carried no `adopt` block at all. An operator receiving one stops
    recognising the deployment as adopted, takes the `NoClusterWork` branch, and
    never reads a rollout for it again — which reads in the console as a serving
    workload stuck at `placed · 0/1` forever, because nothing re-dispatches the
    deployment when the sighting comes back.

    Absence now means one thing only: this deployment was never promoted. A row
    that carries `adopted_workload_uid` and nothing to name it with is a promotion
    that predates those columns and could not be backfilled, and it is logged
    rather than silently rendered as an ordinary deployment — silence is what let
    the failure above run for hours without a line anywhere saying so.
    """
    if not deployment.adopted_workload_uid:
        return None
    if not deployment.adopted_namespace:
        logger.warning(
            "deployment %s is adopted from %s but names no object; rendering it without an adopt block",
            deployment.uid,
            deployment.adopted_workload_uid,
        )
        return None
    return Adopt(
        namespace=deployment.adopted_namespace,
        kind=deployment.adopted_kind or "deployment",
        name=deployment.adopted_name or deployment.name,
        spec_fingerprint=deployment.adopted_fingerprint or "",
    )


async def _cache(db: AsyncSession, cluster_uid: str, cluster_id: UUID) -> CacheDesired:
    """What this cluster must have on disk: its warm set, plus what its deployments serve.

    The union is the same one `_artifact_ids_query` applies, and for the same
    reason. Deploying a model is a statement that its weights must be resident —
    the operator will not start a replica against a cold cache, and it has no
    other way to be told to fetch them. A warm set that was the only source made
    residency a second thing somebody had to declare by hand, in a table with no
    writer, so every deployment of an uncached artifact waited forever on a
    download nothing had started.

    A derived entry is unpinned and at priority zero: it is warm because
    something deploys it, so it should stop being warm when nothing does. A warm
    set row for the same artifact wins, which is what makes the warm set still
    worth having — it is how weights are pre-fetched before a deployment exists
    and how a model is pinned against eviction once it is.

    Only `managed` deployments derive one. An `observed` deployment is one the
    plane has explicitly said it does not run, so declaring what its weights
    should do is the plane acting on a workload it does not own — and pointless
    besides, because something is already serving those weights and they are
    already on the node. The first version of this union left authority out and
    put three adopted models into a cluster's warm set, which downloaded a second
    copy of what was running on it.

    `capacity_bytes` is zero because the plane declares no cache size: the size
    of a cluster's disk is the cluster's business, and inventing a number here
    would be the plane declaring something it does not know.
    """
    rows = (
        await db.execute(
            select(WarmSet, Artifact.uid)
            .join(Artifact, WarmSet.artifact_id == Artifact.id)
            .where(WarmSet.cluster_id == cluster_id)
            .order_by(WarmSet.priority.desc(), WarmSet.seq)
        )
    ).all()
    entries = [CacheWarmEntry(artifact_id=uid, priority=warm.priority, pin=warm.pinned) for warm, uid in rows]
    declared = {entry.artifact_id for entry in entries}

    served = (
        await db.execute(
            select(Artifact.seq, Artifact.uid)
            .join(Workload, Workload.artifact_id == Artifact.id)
            .join(Deployment, Deployment.workload_id == Workload.id)
            .where(
                Deployment.cluster_id == cluster_id,
                Deployment.enabled.is_(True),
                Deployment.authority == "managed",
            )
            .order_by(Artifact.seq)
            .distinct()
        )
    ).all()
    entries.extend(CacheWarmEntry(artifact_id=uid, priority=0, pin=False) for _, uid in served if uid not in declared)

    # An empty warm set is a statement and not a silence. Returning nothing here read
    # as "no opinion", so the last workload on a cluster was deleted, the derived entry
    # disappeared, and no frame went out to say so — leaving the operator converging on
    # the warm set it was last told about, for ever. "Hold nothing" is the answer, and
    # it has to be said out loud for anything to act on it.
    policy: Any = rows[0][0].eviction_policy if rows else "lru"
    return CacheDesired(
        cluster_id=cluster_uid,
        entries=entries,
        eviction_policy=policy if policy in {"lru", "lfu", "none"} else "lru",
        capacity_bytes=0,
        updated_at=format_ts(None),
    )
