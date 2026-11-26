"""Which artifacts a reference can reach: the public catalogue and the caller's own project.

An artifact with no `owner_project_id` is public, shared by every project. One with an owner is
private to that project: its handle and digest are unique only there, it is resolved before a
public artifact of the same handle, and no other project's reference ever reaches it.
"""

from uuid import UUID

from sqlalchemy import or_
from sqlalchemy import case

from app.db.models import Artifact


def _owned_by(project):
    """`project` is a project id or a subquery of them."""
    if isinstance(project, UUID):
        return Artifact.owner_project_id == project
    return Artifact.owner_project_id.in_(project)


def visible(project=None):
    """Public artifacts, and the private ones of `project` when one is given."""
    if project is None:
        return Artifact.owner_project_id.is_(None)
    return or_(Artifact.owner_project_id.is_(None), _owned_by(project))


def private_first():
    """An ordering that puts the caller's private artifacts ahead of public ones."""
    return case((Artifact.owner_project_id.is_(None), 1), else_=0)


def same_scope(owner_project_id: UUID | None):
    """Artifacts in exactly this scope, the one a handle or digest is unique within."""
    if owner_project_id is None:
        return Artifact.owner_project_id.is_(None)
    return Artifact.owner_project_id == owner_project_id
