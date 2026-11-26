"""The name a deployment's objects take inside its cluster.

A deployment used to render under its workload's name, unique per cluster, so two projects could
not both run a `qwen` on one cluster and the refusal told one that the other existed. The object
name now carries the workload's identity: a readable prefix from the workload name and the last
eight characters of the workload uid, which are random. Every deployment of one workload shares
it, so the name is the same in every cluster the workload runs in.

The shape is an RFC 1123 label of at most 32 characters, the budget the operator's renderer
gives a workload name (`maxWorkloadName` in `libs/operator/internal/render/dynamo.go`), so the
renderer passes it through unchanged. Nothing here is specific to one serving runtime.
"""

import re

from app.core.ids import is_uid
from app.db.models import Workload
from app.db.models import Deployment


MAX_LENGTH = 32
_SUFFIX = 8
_PREFIX = MAX_LENGTH - _SUFFIX - 1


def object_name(workload_name: str, workload_uid: str) -> str:
    prefix = re.sub(r"[^a-z0-9]+", "-", (workload_name or "").lower()).strip("-")
    prefix = prefix[:_PREFIX].strip("-") or "w"
    return f"{prefix}-{workload_uid[-_SUFFIX:].lower()}"


def by_ref(ref: str, project_ids=None):
    """The filter a deployment reference resolves through, never across projects.

    An id is exact. A bare name is a workload name, which two projects may share, so it only
    matches inside `project_ids` (a set or subquery of `bind_project.id`). Without a project
    the name is read as a cluster object name, which only one deployment per cluster holds.
    The caller joins `Workload` when it passes `project_ids`.
    """
    if is_uid(ref):
        return Deployment.uid == ref
    if project_ids is None:
        return Deployment.object_name == ref
    return (Deployment.name == ref) & Workload.project_id.in_(project_ids)
