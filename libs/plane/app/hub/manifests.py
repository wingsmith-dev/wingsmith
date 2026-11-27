"""Manifests a member cluster declares through its own Workload custom resources (ADR 0042).

The operator sends `manifest.apply` / `manifest.delete`; this applies them through the same
`ManifestService` the HTTP API uses, as field manager `k8s:<cluster>/<namespace>`, and answers with
`manifest.result`. A cluster applies only into projects linked to it (`supply_cluster_project`), and only from the
namespaces that link allows,
and only ever as its own manager: the manager is derived here, not read from the frame.
"""

import copy
import asyncio
import contextlib

from logging import getLogger
from collections.abc import AsyncIterator

import msgspec

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import changes
from app.db.engine import session_factory
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import ChangeLog
from app.db.models import ClusterProject
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.ingestion.statements import cluster_ref_clause
from app.modules.manifest.services import ManifestService

from .messages import ManifestApply
from .messages import ManifestDelete
from .messages import ManifestResult
from .messages import ManifestChanged


logger = getLogger(__name__)

ORIGIN_CLUSTER = "plane.wingsmith.dev/origin-cluster"
ORIGIN_NAMESPACE = "plane.wingsmith.dev/origin-namespace"
ORIGIN_UID = "plane.wingsmith.dev/origin-uid"
"""Where a cluster-declared object came from, kept in its last-applied record so the objects a
removed cluster leaves behind can be found."""


NAMESPACE_NOT_ALLOWED = "NamespaceNotAllowed"

POLL_SECONDS = 5.0
"""How often the change feed reads the log when no notification woke it first."""

MIN_INTERVAL = 1.0
"""The least time between two reads of the log by one cluster's feed. Every commit wakes every
connected cluster's feed, so on a busy plane this caps each to one read a second, not one a commit."""


def manager_for(cluster: str, namespace: str) -> str:
    return f"k8s:{cluster}/{namespace}"


class ManifestHandler:
    """One short session per frame, like the sink."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._sessions = sessions

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._sessions is None:
            self._sessions = session_factory()
        return self._sessions

    async def handle(self, cluster_ref: str, msg: ManifestApply | ManifestDelete) -> ManifestResult:
        generation = msg.generation if isinstance(msg, ManifestApply) else None
        try:
            async with self.sessions() as db:
                cluster = await db.scalar(select(Cluster).where(cluster_ref_clause(cluster_ref)))
                if cluster is None:
                    return ManifestResult(result="forbidden", generation=generation, error="unknown cluster")
                row = (
                    await db.execute(
                        select(Project, ClusterProject.namespaces)
                        .join(ClusterProject, ClusterProject.project_id == Project.id)
                        .where(ClusterProject.cluster_id == cluster.id, Project.slug == msg.project)
                    )
                ).first()
                if row is None:
                    return ManifestResult(
                        result="forbidden",
                        generation=generation,
                        error=f"project {msg.project!r} is not linked to cluster {cluster.name!r}",
                    )
                project, namespaces = row
                if msg.namespace not in namespaces:
                    return ManifestResult(
                        result="forbidden",
                        generation=generation,
                        reason=NAMESPACE_NOT_ALLOWED,
                        error=f"namespace {msg.namespace!r} may not declare into project {msg.project!r}: "
                        f"add it with PUT /v1/clusters/{cluster.name}/projects/{msg.project}",
                    )
                manager = manager_for(cluster.name, msg.namespace)
                svc = ManifestService(db, actor=f"cluster:{cluster.name}")
                workload = msg.kind == "Workload"
                if isinstance(msg, ManifestDelete):
                    delete = svc.delete_workload if workload else svc.delete_endpoint
                    result = await delete(project.slug, msg.name, manager)
                    return ManifestResult(result=result, manager=manager)
                put = svc.put_workload if workload else svc.put_endpoint
                m, result = await put(project.slug, msg.name, with_origin(msg, cluster.name), manager=manager)
                return ManifestResult(
                    result=result,
                    generation=generation,
                    uid=m.metadata.uid,
                    rv=m.metadata.resource_version,
                    plane_generation=m.metadata.generation,
                    manager=manager,
                    allowed_clusters=list(m.spec.placement.allowed_clusters) if workload else None,
                    status=msgspec.to_builtins(m.status) if m.status is not None else None,
                )
        except Conflict as exc:
            return ManifestResult(result="conflict", generation=generation, error=exc.message)
        except (Invalid, NotFound) as exc:
            return ManifestResult(result="invalid", generation=generation, error=exc.message)
        except Exception as exc:
            logger.exception("hub.manifest.failed kind=%s project=%s name=%s", msg.kind, msg.project, msg.name)
            return ManifestResult(result="error", generation=generation, error=str(exc) or type(exc).__name__)

    async def feed(
        self,
        cluster_ref: str,
        wakeup: asyncio.Event | None = None,
        poll: float = POLL_SECONDS,
        min_interval: float = MIN_INTERVAL,
    ) -> AsyncIterator[ManifestChanged]:
        """The objects this cluster declared, each time one changes: the change log the manifest
        watch reads (ADR 0039), from its head at the time of the call. Changes to objects other
        managers own are skipped, and so are deletes (the resource is gone or going already).

        `wakeup` is set on every committed change (the watch fanout); without it the log is polled.
        """
        async with self.sessions() as db:
            cluster = await db.scalar(select(Cluster).where(cluster_ref_clause(cluster_ref)))
            if cluster is None:
                return
            prefix = manager_for(cluster.name, "")
            cursor = await changes.current(db)
        while True:
            if wakeup is not None:
                wakeup.clear()
            async with self.sessions() as db:
                head = await changes.current(db)
                if head < cursor:
                    # Compacted past us and restarted: nothing to replay, the resync covers it.
                    cursor = head
                found = []
                for kind, model in ((changes.WORKLOAD, Workload), (changes.ENDPOINT, Endpoint)):
                    rows = await db.execute(
                        select(model.name, model.applied_by, Project.slug)
                        .join(ChangeLog, ChangeLog.uid == model.uid)
                        .join(Project, Project.id == model.project_id)
                        .where(
                            ChangeLog.seq > cursor,
                            ChangeLog.seq <= head,
                            ChangeLog.kind == kind,
                            ChangeLog.op != changes.DELETED,
                            model.applied_by.startswith(prefix, autoescape=True),
                        )
                        .distinct()
                    )
                    for name, applied_by, slug in rows.all():
                        found.append(
                            ManifestChanged(kind=kind, project=slug, name=name, namespace=applied_by[len(prefix) :])
                        )
            cursor = head
            for item in found:
                yield item
            if wakeup is None:
                await asyncio.sleep(poll)
            else:
                await asyncio.sleep(min_interval)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wakeup.wait(), poll)


def with_origin(msg: ManifestApply, cluster: str) -> dict:
    body = copy.deepcopy(msg.body)
    meta = body.setdefault("metadata", {})
    meta["name"] = msg.name
    meta["namespace"] = msg.project
    annotations = dict(meta.get("annotations") or {})
    annotations |= {ORIGIN_CLUSTER: cluster, ORIGIN_NAMESPACE: msg.namespace, ORIGIN_UID: msg.uid}
    meta["annotations"] = annotations
    return body
