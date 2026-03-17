from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import ARTIFACT
from app.core.ids import new_uid
from app.db.models import Artifact
from app.db.models import ArtifactLocation
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import ArtifactSchema
from .schemas import ArtifactCreateRequest
from .schemas import ArtifactUpdateRequest
from .schemas import ArtifactLocationSchema
from .schemas import ArtifactLocationRequest
from .repository import ArtifactRepository


KINDS = frozenset({"weights", "adapter", "dataset", "image"})


def _revision(artifact: Artifact) -> str:
    """The value `If-Match` is checked against, derived from the last write."""
    stamp = artifact.updated_at or artifact.created_at
    return str(int(stamp.timestamp() * 1_000_000))


def _location(row: ArtifactLocation) -> ArtifactLocationSchema:
    return ArtifactLocationSchema(
        scheme=row.scheme,
        uri=row.uri,
        region=row.region,
        preferred=row.preferred,
    )


def check_locations(locations: list[ArtifactLocationRequest]) -> None:
    """Rejects a location set that cannot be satisfied.

    A duplicate uri would fail on the table's unique constraint anyway, but as an
    integrity error at commit time rather than as a message naming the uri.
    """
    seen: set[str] = set()
    for item in locations:
        if not item.uri:
            raise Invalid("a location needs a uri")
        if not item.scheme:
            raise Invalid(f"location {item.uri!r} needs a scheme", {"uri": item.uri})
        if item.uri in seen:
            raise Invalid(f"duplicate location {item.uri!r}", {"uri": item.uri})
        seen.add(item.uri)


class ArtifactService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ArtifactRepository(db)

    def schema(self, artifact: Artifact, base_uid: str | None, project_uid: str | None) -> ArtifactSchema:
        return ArtifactSchema(
            id=artifact.uid,
            digest=artifact.digest,
            name=artifact.name,
            kind=artifact.kind,
            format=artifact.format,
            size_bytes=artifact.size_bytes,
            licence=artifact.licence,
            parameter_count=artifact.parameter_count,
            base_artifact_id=base_uid,
            project_id=project_uid,
            metadata=artifact.metadata_,
            created_at=artifact.created_at,
            updated_at=artifact.updated_at,
            revision=_revision(artifact),
            locations=[_location(loc) for loc in artifact.locations],
        )

    async def _one(self, ref: str) -> tuple[Artifact, str | None, str | None]:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no artifact {ref!r}")
        return row[0], row.base_uid, row.project_uid

    async def get(self, ref: str) -> ArtifactSchema:
        return self.schema(*await self._one(ref))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        kind: str | None = None,
        format: str | None = None,
        project: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[ArtifactSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            kind=kind,
            format=format,
            project=project,
            search=search,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self.schema(r[0], r.base_uid, r.project_uid) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: ArtifactCreateRequest) -> ArtifactSchema:
        if data.kind not in KINDS:
            raise Invalid(f"unknown kind {data.kind!r}", {"allowed": sorted(KINDS)})
        if data.size_bytes < 0:
            raise Invalid("size_bytes cannot be negative")
        if not data.digest.strip():
            raise Invalid("an artifact without a digest has no identity")
        check_locations(data.locations)

        base_uid = None
        base_id = None
        if data.base_artifact is not None:
            base = await self.repo.artifact_by_ref(data.base_artifact)
            if base is None:
                raise NotFound(f"no artifact {data.base_artifact!r}")
            base_id, base_uid = base.id, base.uid

        project_uid = None
        project_id = None
        if data.project is not None:
            project = await self.repo.project_by_ref(data.project)
            if project is None:
                raise NotFound(f"no project {data.project!r}")
            project_id, project_uid = project.id, project.uid

        artifact = Artifact(
            uid=new_uid(ARTIFACT),
            digest=data.digest,
            name=data.name,
            kind=data.kind,
            format=data.format,
            size_bytes=data.size_bytes,
            licence=data.licence,
            parameter_count=data.parameter_count,
            base_artifact_id=base_id,
            project_id=project_id,
            metadata_=data.metadata,
            locations=[
                ArtifactLocation(scheme=loc.scheme, uri=loc.uri, region=loc.region, preferred=loc.preferred)
                for loc in data.locations
            ],
        )
        try:
            await self.repo.add(artifact)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"artifact {data.digest!r} already exists") from exc
        await self.db.refresh(artifact, ["locations"])
        return self.schema(artifact, base_uid, project_uid)

    async def update(self, ref: str, data: ArtifactUpdateRequest, if_match: str | None = None) -> ArtifactSchema:
        artifact, base_uid, project_uid = await self._one(ref)
        if if_match is not None and if_match != _revision(artifact):
            raise Conflict("artifact changed since it was read", {"revision": _revision(artifact)})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        if "kind" in fields and fields["kind"] not in KINDS:
            raise Invalid(f"unknown kind {fields['kind']!r}", {"allowed": sorted(KINDS)})
        if fields.get("size_bytes", 0) < 0:
            raise Invalid("size_bytes cannot be negative")

        if "project" in fields:
            ref_value = fields.pop("project")
            if ref_value is None:
                artifact.project_id, project_uid = None, None
            else:
                project = await self.repo.project_by_ref(ref_value)
                if project is None:
                    raise NotFound(f"no project {ref_value!r}")
                artifact.project_id, project_uid = project.id, project.uid

        if "locations" in fields:
            locations = fields.pop("locations")
            check_locations(locations)
            artifact.locations = [
                ArtifactLocation(scheme=loc.scheme, uri=loc.uri, region=loc.region, preferred=loc.preferred)
                for loc in locations
            ]

        if "metadata" in fields:
            artifact.metadata_ = fields.pop("metadata")
        for key, value in fields.items():
            setattr(artifact, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("artifact conflicts with an existing one") from exc
        await self.db.refresh(artifact, ["locations"])
        return self.schema(artifact, base_uid, project_uid)

    async def delete(self, ref: str) -> None:
        """Refuses while a recipe still names the artifact.

        The foreign key is `RESTRICT`, so the database would refuse anyway; the
        point of catching it is that "a recipe still serves this" is an answer
        and an integrity error is not.
        """
        artifact, _, _ = await self._one(ref)
        try:
            await self.repo.delete(artifact)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"artifact {ref!r} is still referenced by a recipe") from exc
