"""Manifests a member cluster declares through its own Workload custom resources (ADR 0042).

The operator sends `manifest.apply` / `manifest.delete`; this applies them through the same
`ManifestService` the HTTP API uses, as field manager `k8s:<cluster>/<namespace>`, and answers with
`manifest.result`. A cluster applies only into projects linked to it (`supply_cluster_project`),
and only ever as its own manager: the manager is derived here, not read from the frame.
"""

import copy

from logging import getLogger

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import session_factory
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import ClusterProject
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.ingestion.statements import cluster_ref_clause
from app.modules.manifest.services import ManifestService

from .messages import ManifestApply
from .messages import ManifestDelete
from .messages import ManifestResult


logger = getLogger(__name__)

ORIGIN_CLUSTER = "plane.wingsmith.dev/origin-cluster"
ORIGIN_NAMESPACE = "plane.wingsmith.dev/origin-namespace"
ORIGIN_UID = "plane.wingsmith.dev/origin-uid"
"""Where a cluster-declared object came from, kept in its last-applied record so the objects a
removed cluster leaves behind can be found."""


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
                project = await db.scalar(
                    select(Project)
                    .join(ClusterProject, ClusterProject.project_id == Project.id)
                    .where(ClusterProject.cluster_id == cluster.id, Project.slug == msg.project)
                )
                if project is None:
                    return ManifestResult(
                        result="forbidden",
                        generation=generation,
                        error=f"project {msg.project!r} is not linked to cluster {cluster.name!r}",
                    )
                manager = manager_for(cluster.name, msg.namespace)
                svc = ManifestService(db, actor=f"cluster:{cluster.name}")
                if isinstance(msg, ManifestDelete):
                    result = await svc.delete_workload(project.slug, msg.name, manager)
                    return ManifestResult(result=result, manager=manager)
                m, result = await svc.put_workload(
                    project.slug, msg.name, with_origin(msg, cluster.name), manager=manager
                )
                return ManifestResult(
                    result=result,
                    generation=generation,
                    uid=m.metadata.uid,
                    rv=m.metadata.resource_version,
                    plane_generation=m.metadata.generation,
                    manager=manager,
                    allowed_clusters=list(m.spec.placement.allowed_clusters),
                )
        except Conflict as exc:
            return ManifestResult(result="conflict", generation=generation, error=exc.message)
        except (Invalid, NotFound) as exc:
            return ManifestResult(result="invalid", generation=generation, error=exc.message)
        except Exception as exc:
            logger.exception("hub.manifest.failed kind=%s project=%s name=%s", msg.kind, msg.project, msg.name)
            return ManifestResult(result="error", generation=generation, error=str(exc) or type(exc).__name__)


def with_origin(msg: ManifestApply, cluster: str) -> dict:
    body = copy.deepcopy(msg.body)
    meta = body.setdefault("metadata", {})
    meta["name"] = msg.name
    meta["namespace"] = msg.project
    annotations = dict(meta.get("annotations") or {})
    annotations |= {ORIGIN_CLUSTER: cluster, ORIGIN_NAMESPACE: msg.namespace, ORIGIN_UID: msg.uid}
    meta["annotations"] = annotations
    return body
