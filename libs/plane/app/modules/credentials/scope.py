"""Which credentials a private artifact is fetched with.

A credential is a reference (`secret_ref`) to a secret held outside the plane. The plane never
stores or forwards the value: the desired stream carries the reference, and the operator resolves
it on the cluster (a Secret an admin applied there).

A private artifact is fetched with a credential of its own project only. A location may name one;
otherwise a Hugging Face source falls back to the project's newest usable `hf_token`. A public
artifact carries no credential reference and the cluster reads its own fleet-wide token.
"""

import datetime as dt

from uuid import UUID
from collections.abc import Iterable

from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Credential


HF_TOKEN = "hf_token"
"""A Hugging Face access token."""

KINDS = (HF_TOKEN,)

PROJECT = "project"
"""The scope of a credential that belongs to one project."""


def usable(at: dt.datetime):
    """Not revoked and not expired at `at`."""
    return Credential.revoked_at.is_(None) & or_(Credential.expires_at.is_(None), Credential.expires_at > at)


class ProjectCredentials:
    """The usable credentials of a set of projects, loaded once for one desired-state pass."""

    def __init__(self, rows: Iterable[Credential]) -> None:
        self._by_id: dict[UUID, Credential] = {}
        self._hf: dict[UUID, Credential] = {}
        for row in rows:
            self._by_id[row.id] = row
            if row.kind == HF_TOKEN and row.scope_id is not None:
                current = self._hf.get(row.scope_id)
                if current is None or (row.created_at, row.seq) > (current.created_at, current.seq):
                    self._hf[row.scope_id] = row

    @classmethod
    async def load(cls, db: AsyncSession, projects: Iterable[UUID], at: dt.datetime) -> "ProjectCredentials":
        ids = set(projects)
        if not ids:
            return cls(())
        rows = await db.execute(
            select(Credential).where(Credential.scope_type == PROJECT, Credential.scope_id.in_(ids), usable(at))
        )
        return cls(rows.scalars())

    def ref(self, project_id: UUID, credential_id: UUID | None, scheme: str) -> str | None:
        """The secret reference a source of `project_id`'s private artifact is read with, or None.

        A credential the location names counts only if it belongs to the same project and is
        usable; one from another project is never forwarded, so a project cannot borrow it.
        """
        if credential_id is not None:
            named = self._by_id.get(credential_id)
            return named.secret_ref if named is not None and named.scope_id == project_id else None
        if scheme == "hf" and (default := self._hf.get(project_id)) is not None:
            return default.secret_ref
        return None
