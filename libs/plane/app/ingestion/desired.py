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
from collections.abc import AsyncIterator

from msgspec import Struct
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pool
from app.db.models import Recipe
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import WarmSet
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import Component
from app.db.models import Deployment
from app.db.models import PriorityClass
from app.db.models import ArtifactLocation
from app.db.models import DiscoveredWorkload
from app.hub.messages import Adopt
from app.hub.messages import CacheDesired
from app.hub.messages import ArtifactSource
from app.hub.messages import CacheWarmEntry
from app.hub.messages import ArtifactDesired
from app.hub.messages import ComponentDesired
from app.hub.messages import DeploymentDeleted
from app.hub.messages import DeploymentDesired
from app.hub.messages import PlacementConstraints
from app.ingestion.time import now


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
    cluster_id = await db.scalar(select(Cluster.id).where(Cluster.uid == cluster_uid))
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
        cache = await _cache(db, cluster_uid, cluster_id)
        if cache is not None:
            yield "cache.desired", cache


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
    """
    cluster_id = await db.scalar(select(Cluster.id).where(Cluster.uid == cluster_uid))
    if cluster_id is None or kind != "deployment":
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
        select(Recipe.artifact_id)
        .join(Deployment, Deployment.recipe_id == Recipe.id)
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


def _placement(recipe: Recipe, pools: list[str]) -> PlacementConstraints:
    topology = "single_domain" if recipe.requires_single_topology_domain else "none"
    return PlacementConstraints(
        device_class=recipe.device_class,
        devices_per_replica=recipe.device_count,
        min_device_memory_mb=recipe.device_memory_mib_min,
        topology=topology,
        allowed_pools=pools,
        node_selector={},
    )


async def _deployments(
    db: AsyncSession, cluster_uid: str, cluster_id: UUID, only: str | None = None
) -> list[DeploymentDesired]:
    query = (
        select(
            Deployment,
            Workload,
            Recipe,
            Project.uid,
            PriorityClass.name,
            Artifact.uid,
            DiscoveredWorkload.namespace,
            DiscoveredWorkload.kind,
            DiscoveredWorkload.name,
        )
        .join(Workload, Deployment.workload_id == Workload.id)
        .join(Recipe, Deployment.recipe_id == Recipe.id)
        .join(Project, Workload.project_id == Project.id)
        .join(PriorityClass, Workload.priority_class_id == PriorityClass.id)
        .join(Artifact, Recipe.artifact_id == Artifact.id)
        .outerjoin(
            DiscoveredWorkload,
            (DiscoveredWorkload.cluster_id == Deployment.cluster_id)
            & (DiscoveredWorkload.uid == Deployment.adopted_workload_uid),
        )
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

    adapters = {
        artifact_id: uid
        for artifact_id, uid in (
            await db.execute(
                select(Artifact.id, Artifact.uid).where(
                    Artifact.id.in_({a for row in rows for a in (row[0].adapter_artifact_ids or [])})
                )
            )
        ).all()
    }

    out: list[DeploymentDesired] = []
    for row in rows:
        deployment, workload, recipe, project_uid, priority, artifact_uid = row[:6]
        artifact_ids = [artifact_uid] + [adapters[a] for a in (deployment.adapter_artifact_ids or []) if a in adapters]
        out.append(
            DeploymentDesired(
                deployment_id=deployment.uid,
                cluster_id=cluster_uid,
                project_id=project_uid,
                recipe_id=recipe.uid,
                recipe_revision=recipe.version,
                lifecycle=workload.kind,
                priority_class=priority,
                min_replicas=deployment.min_replicas,
                max_replicas=deployment.max_replicas,
                placement=_placement(recipe, allowed),
                artifact_ids=artifact_ids,
                generation=deployment.generation,
                updated_at=format_ts(deployment.updated_at or deployment.created_at),
                adopt=_adopt(deployment, row[6], row[7], row[8]),
            )
        )
    return out


def _adopt(deployment: Deployment, namespace: str | None, kind: str | None, name: str | None) -> Adopt | None:
    """The adoption block, when this deployment was promoted from a running workload.

    Both halves have to be present. The deployment carries the digest it was
    pinned to, and the sighting carries where the object lives; without the
    sighting there is nothing to name, and the frame goes down as an ordinary
    deployment — which is the safe direction only because a deployment the
    operator cannot identify as adopted is one it has no rendered spec for either.
    """
    if not deployment.adopted_workload_uid or namespace is None:
        return None
    return Adopt(
        namespace=namespace,
        kind=kind or "deployment",
        name=name or deployment.name,
        spec_fingerprint=deployment.adopted_fingerprint or "",
    )


async def _cache(db: AsyncSession, cluster_uid: str, cluster_id: UUID) -> CacheDesired | None:
    """The cluster's warm set as one frame.

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
    if not rows:
        return None
    policy: Any = rows[0][0].eviction_policy
    return CacheDesired(
        cluster_id=cluster_uid,
        entries=[CacheWarmEntry(artifact_id=uid, priority=warm.priority, pin=warm.pinned) for warm, uid in rows],
        eviction_policy=policy if policy in {"lru", "lfu", "none"} else "lru",
        capacity_bytes=0,
        updated_at=format_ts(None),
    )
