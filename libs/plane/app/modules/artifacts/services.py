import msgspec

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
from app.core.exceptions import PlaneError
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.integrations.huggingface import HuggingFaceError
from app.integrations.huggingface import HuggingFaceGated
from app.integrations.huggingface import HuggingFaceClient
from app.integrations.huggingface import HuggingFaceNotFound
from app.integrations.huggingface import HuggingFaceUnavailable

from .resolve import ResolvedArtifact
from .resolve import slug_of
from .resolve import resolved
from .resolve import normalise_source
from .schemas import ArtifactSchema
from .schemas import ArtifactUsageSchema
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


def _apply(data: ArtifactCreateRequest, draft: ResolvedArtifact) -> ArtifactCreateRequest:
    """Overlays a resolved draft under whatever the caller stated.

    Only the fields the request still leaves at their default are filled. `kind`,
    `name`, `project` and `metadata` are the caller's opinion about what these
    weights are for, and resolving a manifest says nothing about that.
    """
    metadata = {**draft.metadata, **data.metadata}
    return msgspec.structs.replace(
        data,
        digest=draft.digest,
        name=data.name or draft.name,
        format=data.format or draft.format,
        size_bytes=data.size_bytes or draft.size_bytes,
        licence=data.licence or draft.licence,
        parameter_count=data.parameter_count if data.parameter_count is not None else draft.parameter_count,
        locations=data.locations or list(draft.locations),
        verified=draft.verified,
        metadata=metadata,
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
    def __init__(self, db: AsyncSession, hub: HuggingFaceClient | None = None) -> None:
        """`hub` is injected rather than built here so a test can substitute a fake.

        Constructing an `httpx` client inside the service would make every create
        path a network path, and the only way to test registration would be to
        reach the real Hub.
        """
        self.db = db
        self.hub = hub or HuggingFaceClient()
        self.repo = ArtifactRepository(db)

    async def resolve(self, source: str) -> ResolvedArtifact:
        """Reads a source manifest as a proposed artifact. Writes nothing.

        The Hub's failures are typed here rather than leaking: an unknown slug is a
        404 because the thing named does not exist, a gated repo is `Invalid`
        because the request is coherent but this plane holds no token that may read
        it, and the Hub being down is the base `PlaneError` — the contract has no
        502-shaped class, and a 500 saying the upstream failed is closer than
        claiming the caller asked for something wrong.
        """
        slug = slug_of(source)
        if not slug:
            raise Invalid(f"{source!r} is not a huggingface source", {"expected": "hf://owner/name"})
        try:
            model = await self.hub.inspect(slug)
        except HuggingFaceNotFound as exc:
            raise NotFound(f"no such model on huggingface: {slug}") from exc
        except HuggingFaceGated as exc:
            raise Invalid(
                f"huggingface repo {slug!r} is gated and the configured token cannot read it",
                {"source": normalise_source(source), "token": "HANGAR_HF_TOKEN"},
            ) from exc
        except HuggingFaceUnavailable as exc:
            raise PlaneError(f"huggingface did not answer for {slug!r}") from exc
        except HuggingFaceError as exc:
            raise PlaneError(f"huggingface refused the request for {slug!r}") from exc

        existing = await self.repo.artifact_by_digest(model.digest)
        return resolved(model, existing_id=existing.uid if existing is not None else None)

    def schema(self, artifact: Artifact, base_uid: str | None, project_uid: str | None) -> ArtifactSchema:
        return ArtifactSchema(
            id=artifact.uid,
            digest=artifact.digest,
            verified=artifact.verified,
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

    async def usage(self, ref: str) -> ArtifactUsageSchema:
        """What depends on this artifact, counted at read time."""
        artifact, _, _ = await self._one(ref)
        recipes, workloads, downstream = await self.repo.usage_counts(artifact)
        return ArtifactUsageSchema(
            artifact_id=artifact.uid,
            recipe_count=recipes,
            workload_count=workloads,
            downstream_count=downstream,
        )

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        kind: str | None = None,
        format: str | None = None,
        project: str | None = None,
        base_artifact: str | None = None,
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
            base_artifact=base_artifact,
            search=search,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self.schema(r[0], r.base_uid, r.project_uid) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: ArtifactCreateRequest) -> ArtifactSchema:
        """`source` and `digest` are alternatives, and the draft only ever fills blanks.

        Both given is refused rather than reconciled: two ways to state identity is
        two ways to disagree, and picking a winner would silently register bytes the
        caller did not name. Everything the caller passed explicitly survives the
        draft, which is a default and not an override.
        """
        if data.source and data.digest.strip():
            raise Invalid(
                "an artifact is identified by a source or by a digest, not both",
                {"digest": data.digest, "source": data.source},
            )
        if data.source:
            data = _apply(data, await self.resolve(data.source))

        if data.kind not in KINDS:
            raise Invalid(f"unknown kind {data.kind!r}", {"allowed": sorted(KINDS)})
        if data.size_bytes < 0:
            raise Invalid("size_bytes cannot be negative")
        if not data.digest.strip():
            raise Invalid("an artifact without a digest has no identity")
        if not data.name.strip():
            raise Invalid("an artifact without a name cannot be found again")
        check_locations(data.locations)

        base_id = None
        if data.base_artifact is not None:
            base = await self.repo.artifact_by_ref(data.base_artifact)
            if base is None:
                raise NotFound(f"no artifact {data.base_artifact!r}")
            base_id = base.id

        project_id = None
        if data.project is not None:
            project = await self.repo.project_by_ref(data.project)
            if project is None:
                raise NotFound(f"no project {data.project!r}")
            project_id = project.id

        artifact = Artifact(
            uid=new_uid(ARTIFACT),
            digest=data.digest,
            verified=data.verified,
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
        return await self.get(artifact.uid)

    async def update(self, ref: str, data: ArtifactUpdateRequest, if_match: str | None = None) -> ArtifactSchema:
        """Replaces the declared half, and the location set as a whole when it is given.

        The replacement empties the collection and flushes before attaching the new
        rows: `uri` is unique, and a single flush orders every insert before the
        deletes, so keeping a location while changing its region would collide with
        the row it is about to replace.
        """
        artifact, _, _ = await self._one(ref)
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
                artifact.project_id = None
            else:
                project = await self.repo.project_by_ref(ref_value)
                if project is None:
                    raise NotFound(f"no project {ref_value!r}")
                artifact.project_id = project.id

        if "locations" in fields:
            locations = fields.pop("locations")
            check_locations(locations)
            artifact.locations = []
            await self.db.flush()
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
        return await self.get(artifact.uid)

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
