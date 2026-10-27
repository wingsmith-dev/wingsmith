import datetime as dt

from dataclasses import replace

import msgspec

from msgspec import UNSET
from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import devices
from app.core.ids import ARTIFACT
from app.core.ids import ARTIFACT_BASELINE
from app.core.ids import new_uid
from app.core.vram import MAX_DEVICES
from app.core.vram import DEFAULT_QUANTIZATION
from app.core.vram import DEFAULT_DEVICES_PER_NODE
from app.core.vram import Fit
from app.core.vram import Intent
from app.core.vram import Sizing
from app.core.vram import fit
from app.core.vram import ladder
from app.core.vram import sizing_of
from app.db.models import Artifact
from app.db.models import ArtifactBaseline
from app.db.models import ArtifactLocation
from app.core.devices import DeviceClass
from app.core.devices import DeviceTable
from app.core.schemas import ListResponse
from app.core.serving import artifact_slug
from app.core.revision import revision
from app.core.revision import check_if_match
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

from .resolve import FALLBACK_CONTEXT_LENGTH
from .resolve import ResolvedArtifact
from .resolve import slug_of
from .resolve import resolved
from .resolve import source_of
from .resolve import normalise_source
from .schemas import FLEET
from .schemas import FITMENT_SCOPES
from .schemas import ArtifactSchema
from .schemas import ArtifactFitSchema
from .schemas import ArtifactUsageSchema
from .schemas import ArtifactCreateRequest
from .schemas import ArtifactFitmentSchema
from .schemas import ArtifactRefreshSchema
from .schemas import ArtifactUpdateRequest
from .schemas import ArtifactBaselineSchema
from .schemas import ArtifactFitmentRequest
from .schemas import ArtifactLocationSchema
from .schemas import ArtifactUpstreamSchema
from .schemas import ArtifactFitLayoutSchema
from .schemas import ArtifactLocationRequest
from .schemas import ArtifactUpstreamRequest
from .schemas import ArtifactBaselineCreateRequest
from .schemas import ArtifactBaselineUpdateRequest
from .repository import ArtifactRepository


KINDS = frozenset({"weights", "adapter", "dataset", "image"})

DEFAULT_FITMENT_LIMIT = 5
"""Ranked rows a fitment returns unless asked for more.

Nineteen device classes is a wall, and the answer to "what do I run this on" is in the
first few. The cap is on the response, not on the ranking: every candidate is
still fitted and ranked, so the head is genuinely the best five and not the first five
that happened to be looked at.
"""

IGNORED_PROVENANCE = ("recorded_by", "recorded_at")
"""Body fields a caller may send and the plane refuses to read. See the create schema."""

_UPSTREAM_FIELDS = frozenset(ArtifactUpstreamSchema.__struct_fields__)
"""What the claim column is allowed to hold. Anything else in it is ignored on read."""

REFRESHED = ("format", "size_bytes", "licence", "parameter_count", "verified")
"""The scalar half a re-read of the source may move. Everything absent here is deliberate.

`digest` is identity: different bytes are a different artifact, which is why a refresh
that resolves to another digest is refused, not applied. `name`, `kind`,
`project` and `base_artifact_id` are somebody's opinion about what these weights are
for, and the repo has none. `verified` *is* here, and it is the one field
`ArtifactUpdateRequest` refuses: verification is resolving the bytes, and this is
where the bytes get resolved.
"""


def _upstream(row: Artifact) -> ArtifactUpstreamSchema:
    """The claim block as a struct, keyed defensively.

    Read key by key, not converted wholesale: the column predates the
    schema and holds `{}` on every artifact registered before this, so a strict
    decode would turn every one of those reads into a 500.
    """
    stored = row.upstream or {}
    return msgspec.convert(
        {k: v for k, v in stored.items() if k in _UPSTREAM_FIELDS},
        ArtifactUpstreamSchema,
        strict=False,
    )


def _location(row: ArtifactLocation) -> ArtifactLocationSchema:
    return ArtifactLocationSchema(
        scheme=row.scheme,
        uri=row.uri,
        region=row.region,
        preferred=row.preferred,
    )


def _declared_context(artifact: Artifact, sizing: Sizing | None) -> int:
    """The window to fit at when the caller named none: the artifact's own.

    Read from `metadata.context_length` first because that is what the repo
    published, then from the persisted attention shape, then a floor that only
    exists so the arithmetic has a number. An artifact that declares 131k is fitted
    at 131k: choosing a smaller window here would make more cards look workable by
    quietly answering a different question.
    """
    declared = artifact.metadata_.get("context_length")
    if isinstance(declared, int) and declared > 0:
        return declared
    if sizing is not None and sizing.shape is not None and sizing.shape.context_length:
        return sizing.shape.context_length
    return FALLBACK_CONTEXT_LENGTH


def _observed_class(row: Row) -> DeviceClass:
    """A device class built from what a cluster reported, for hardware the table lacks.

    `app.core.devices` slugifies an unrecognised vendor string instead of dropping
    it, so the fleet can legitimately hold classes the contract table has never
    named. Those still have an observed memory size, which is all a fit needs: the
    label falls back to the class itself, exactly as `label_for` does.
    """
    return DeviceClass(
        name=row.device_class,
        label=row.device_class,
        architecture="",
        vendor="",
        memory_mib=row.memory_mib or 0,
        form_factor="",
        aliases={},
    )


def _candidates(scope: str, fleet: dict[str, Row], catalog: DeviceTable) -> list[DeviceClass]:
    """The classes to fit against, per scope.

    `fleet` is what clusters have reported, preferring the contract table's entry for
    a class it names and falling back to the observed one for a class it does not.
    `catalog` is that set plus every class the table carries, so a fit on hardware we
    own none of is still shown: that is the procurement answer, and filtering it out
    is what makes people ask us for the number by hand.
    """
    observed = [catalog.get(name) or _observed_class(row) for name, row in fleet.items()]
    if scope == FLEET:
        return observed
    named = {entry.name for entry in observed}
    return observed + [entry for entry in catalog.classes if entry.name not in named]


def _note(scope: str, candidates: list[DeviceClass], weights_only: bool) -> str | None:
    """The one caveat worth putting above the table, or nothing.

    An empty candidate set outranks the weights-only caveat: a note about uncosted
    windows is unhelpful next to a table with no rows in it.
    """
    if not candidates:
        if scope == FLEET:
            return "no cluster has reported a device yet, so there is no fleet to fit against"
        return "the device table could not be read, so there are no classes to fit against"
    if weights_only:
        return (
            "this artifact publishes no attention shape, so these rows size the weights only "
            "and no context window is costed: every requirement shown is a floor. Re-resolving "
            "its source records the shape and prices the window."
        )
    return None


def _short(result: Fit, row: Row | None) -> bool:
    """Whether the fleet holds this class but too few of it to run one replica.

    Three different noes, and they must not be flattened into one. `fits` false is
    physics and no amount of hardware fixes it. Zero devices of the class is a
    purchasing question, which is what the catalog scope is for. This is the third: we
    own the right card and not enough of them, so the row is arithmetically a fit and
    nothing anyone can run today.

    Counted against `devices_total` instead of `devices_allocatable` because the two
    are different problems. Cards busy with somebody else's work come back; cards that
    were never bought do not.
    """
    return bool(result.fits and row is not None and 0 < row.devices_total < result.device_count)


def _rank(result: Fit, short: bool) -> tuple:
    """Fits first, then fits we own the hardware for, then full windows, then the fewest nodes.

    Supply outranks the window because a truncated fit that runs today beats a full one
    that needs cards nobody has bought. Nodes outrank devices for the same reason of
    acting on it: nodes are what gets bought, and thirty-two devices in four nodes is a
    smaller order than thirty-two across eight.
    """
    return (
        not result.fits,
        short,
        result.reduced,
        result.node_count,
        result.device_count,
        result.aggregate_gb,
        result.device_class,
    )


def _layout_schema(option: Fit, row: Row | None) -> ArtifactFitLayoutSchema:
    return ArtifactFitLayoutSchema(
        device_count=option.device_count,
        node_count=option.node_count,
        tensor_parallel=option.tensor_parallel,
        pipeline_parallel=option.pipeline_parallel,
        aggregate_gb=round(option.aggregate_gb, 2),
        required_gb=round(option.required_gb, 2),
        context_length=option.context_length,
        reduced=option.reduced,
        supply_short=_short(option, row),
        reason=option.reason,
    )


def _fit_schema(
    result: Fit,
    devices_per_node: int,
    row: Row | None,
    catalog: DeviceTable,
    options: list[Fit],
) -> ArtifactFitSchema:
    return ArtifactFitSchema(
        device_class=result.device_class,
        label=catalog.label_for(result.device_class),
        fits=result.fits,
        possible=result.possible,
        reduced=result.reduced,
        device_count=result.device_count,
        node_count=result.node_count,
        devices_per_node=devices_per_node,
        tensor_parallel=result.tensor_parallel,
        pipeline_parallel=result.pipeline_parallel,
        memory_mib_per_device=result.memory_mib_per_device,
        aggregate_gb=round(result.aggregate_gb, 2),
        weights_gb=round(result.weights_gb, 2),
        required_gb=round(result.required_gb, 2),
        context_length=result.context_length,
        kv_counted=result.kv_counted,
        reason=result.reason,
        devices_total=row.devices_total if row else 0,
        devices_allocatable=(row.devices_allocatable or 0) if row else 0,
        cluster_count=row.cluster_count if row else 0,
        supply_short=_short(result, row),
        in_catalog=catalog.get(result.device_class) is not None,
        layouts=[_layout_schema(option, row) for option in options],
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
        upstream=data.upstream or _recorded(draft.publisher),
        verified=draft.verified,
        metadata=metadata,
    )


def _recorded(claims: ArtifactUpstreamRequest) -> ArtifactUpstreamRequest:
    """Stamps a resolved claim block with when it was recorded.

    Here, not in `resolve`, because resolving is a read anybody may do and
    a `recorded_at` on a draft nobody registered dates a record that does not
    exist.
    """
    return msgspec.structs.replace(claims, recorded_at=dt.datetime.now(dt.UTC).isoformat())


def _reread(artifact: Artifact, draft: ResolvedArtifact) -> list[str]:
    """Lays a fresh draft over an artifact's derived half, naming what actually moved.

    Metadata is overlaid key by key, not replaced, so labels a person put on the
    artifact survive a refresh and the derived keys are the only ones rewritten, including
    to null, because a key the repo no longer publishes is a fact about the repo. The dict
    is rebuilt, not mutated: `JSONB` is not change-tracked in place, and an
    in-place update is a refresh that reports success and writes nothing.
    """
    changed = []
    for field in REFRESHED:
        value = getattr(draft, field)
        if getattr(artifact, field) != value:
            setattr(artifact, field, value)
            changed.append(field)

    metadata = dict(artifact.metadata_)
    for key, value in draft.metadata.items():
        if metadata.get(key) != value:
            metadata[key] = value
            changed.append(f"metadata.{key}")
    artifact.metadata_ = metadata

    claims = dict(artifact.upstream or {})
    for key, value in msgspec.to_builtins(draft.publisher).items():
        # Only the blanks. A re-read is the Hub speaking again, and the Hub does not
        # get to overwrite a claim a person asserted, including the `false` that a
        # publisher_claims block always leaves null.
        if value in (None, [], "") or claims.get(key) not in (None, [], ""):
            continue
        claims[key] = value
        changed.append(f"upstream.{key}")
    artifact.upstream = claims
    return changed


def check_locations(locations: list[ArtifactLocationRequest]) -> None:
    """Rejects a location set that cannot be satisfied.

    A duplicate uri would fail on the table's unique constraint anyway, but as an
    integrity error at commit time, not as a message naming the uri.
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
        """`hub` is injected, not built here so a test can substitute a fake.

        Constructing an `httpx` client inside the service would make every create
        path a network path, and the only way to test registration would be to
        reach the real Hub.
        """
        self.db = db
        self.hub = hub or HuggingFaceClient()
        self.repo = ArtifactRepository(db)

    async def resolve(self, source: str) -> ResolvedArtifact:
        """Reads a source manifest as a proposed artifact. Writes nothing.

        The Hub's failures are typed here instead of leaking: an unknown slug is a
        404 because the thing named does not exist, a gated repo is `Invalid`
        because the request is coherent but this plane holds no token that may read
        it, and the Hub being down is the base `PlaneError`: the contract has no
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
        return resolved(
            model,
            existing_id=existing.uid if existing is not None else None,
            ceiling_mib=await self.repo.largest_device_memory_mib(),
        )

    async def refresh(self, ref: str) -> ArtifactRefreshSchema:
        """Re-reads a registered artifact's own source and updates what is derived from it.

        The case this exists for: the plane learns to extract something it did not
        extract before, an attention shape, a native quantization, an active parameter
        count, and every artifact registered until then is missing it. Without this
        route the only way to record it is to delete the artifact and register it again,
        which breaks every workload that names it, or to `PATCH` the metadata by hand,
        which is a person retyping what the plane can read.

        It is a `POST` and not a `PATCH` because it is an operation: it reaches the
        network, it can fail for reasons that have nothing to do with the request, and
        the caller supplies no body: there is nothing here for them to state.

        A source resolving to a different digest is refused. That is not a stale record
        to be corrected, it is different bytes, and different bytes are a different
        artifact: applying the new size and parameter count over this digest would leave
        a row describing weights nobody can fetch. Register the new source instead:
        `GET /artifacts/resolve` will say whether it already exists.

        No commit happens when nothing moved, so a refresh that finds the source
        unchanged leaves `updated_at` alone and nobody's `If-Match` goes stale.
        """
        artifact, _, _ = await self._one(ref)
        source = source_of(artifact.metadata_, tuple(loc.uri for loc in artifact.locations))
        if source is None:
            raise Invalid(
                f"artifact {artifact.uid} records no source to re-read; only huggingface artifacts can be refreshed",
                {"artifact": artifact.uid, "expected": "metadata.huggingface_id or an hf:// location"},
            )

        draft = await self.resolve(source)
        if draft.digest != artifact.digest:
            raise Conflict(
                f"{source} now resolves to different bytes, which is a new artifact instead of a change to this one",
                {
                    "source": source,
                    "digest": artifact.digest,
                    "resolved_digest": draft.digest,
                    "existing_id": draft.existing_id,
                },
            )

        changed = _reread(artifact, draft)
        if changed:
            await self.db.commit()
        return ArtifactRefreshSchema(
            artifact=await self.get(artifact.uid),
            source=source,
            changed=changed,
            notes=list(draft.notes),
        )

    def schema(self, artifact: Artifact, base_uid: str | None, project_uid: str | None) -> ArtifactSchema:
        return ArtifactSchema(
            id=artifact.uid,
            digest=artifact.digest,
            verified=artifact.verified,
            name=artifact.name,
            slug=artifact.slug,
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
            revision=revision(artifact),
            locations=[_location(loc) for loc in artifact.locations],
            upstream=_upstream(artifact),
        )

    async def _one(self, ref: str, lock: bool = False) -> tuple[Artifact, str | None, str | None]:
        row = await self.repo.get(ref, lock=lock)
        if row is None:
            raise NotFound(f"no artifact {ref!r}")
        return row[0], row.base_uid, row.project_uid

    async def get(self, ref: str) -> ArtifactSchema:
        return self.schema(*await self._one(ref))

    async def usage(self, ref: str) -> ArtifactUsageSchema:
        """What depends on this artifact, counted at read time."""
        artifact, _, _ = await self._one(ref)
        workloads, downstream = await self.repo.usage_counts(artifact)
        return ArtifactUsageSchema(
            artifact_id=artifact.uid,
            workload_count=workloads,
            downstream_count=downstream,
        )

    async def fitment(self, ref: str, query: ArtifactFitmentRequest) -> ArtifactFitmentSchema:
        """Every device class this artifact could be served on, most useful first.

        Reads nothing but the artifact and the observed device counts. The sizing is
        `app.core.vram`'s and the fleet half is three aggregates, so this is a view
        instead of a decision: nothing here reserves anything, and a class it calls
        a fit still has to survive placement.
        """
        if query.scope not in FITMENT_SCOPES:
            raise Invalid(
                f"scope must be one of {', '.join(FITMENT_SCOPES)}",
                {"scope": query.scope, "allowed": list(FITMENT_SCOPES)},
            )
        for name, value in (
            ("concurrency", query.concurrency),
            ("max_devices", query.max_devices),
            ("devices_per_node", query.devices_per_node),
        ):
            if value is not None and value < 1:
                raise Invalid(f"{name} must be at least 1", {name: value})
        if query.context_length is not None and query.context_length < 1:
            raise Invalid("context_length must be at least 1", {"context_length": query.context_length})

        artifact, _, _ = await self._one(ref)
        sizing = sizing_of(artifact.metadata_, artifact.parameter_count)
        if sizing is not None and query.quantization:
            sizing = replace(sizing, quantization=query.quantization)

        context_length = query.context_length or _declared_context(artifact, sizing)
        max_devices = min(query.max_devices or MAX_DEVICES, MAX_DEVICES)
        header = {
            "artifact_id": artifact.uid,
            "scope": query.scope,
            "context_length": context_length,
            "concurrency": query.concurrency,
            "max_devices": max_devices,
        }
        if sizing is None:
            return ArtifactFitmentSchema(
                sizable=False,
                considered=0,
                quantization=query.quantization or DEFAULT_QUANTIZATION,
                note=(
                    "this artifact publishes no parameter count, so not even its weights can be "
                    "sized; re-resolving its source records what a fit needs"
                ),
                items=[],
                **header,
            )

        fleet = {row.device_class: row for row in await self.repo.fleet_classes()}
        observed_nodes = await self.repo.devices_per_node()
        catalog = devices.table()
        candidates = _candidates(query.scope, fleet, catalog)
        fallback_node = query.devices_per_node or DEFAULT_DEVICES_PER_NODE
        """Precedence for how big a node is, most authoritative first: what the fleet has
        observed, then what the contract says the part is packaged as, then whatever the
        caller assumed, then the default. The caller's figure fills gaps instead of
        overruling either: an NVL72 is 72 devices whether or not somebody said eight."""

        results: list[tuple[Fit, int, DeviceClass, Intent]] = []
        for candidate in candidates:
            devices_per_node = observed_nodes.get(candidate.name) or candidate.devices_per_node or fallback_node
            intent = Intent(
                context_length=context_length,
                concurrency=query.concurrency,
                devices_per_node=devices_per_node,
                max_devices=max_devices,
            )
            results.append((fit(sizing, candidate, intent), devices_per_node, candidate, intent))
        results.sort(key=lambda row: _rank(row[0], _short(row[0], fleet.get(row[0].device_class))))
        limit = DEFAULT_FITMENT_LIMIT if query.limit is None else query.limit
        head = results if limit <= 0 else results[:limit]
        """The ladder is walked for the rows that are returned and not for every class
        ranked. It is a handful of divisions per class, but it answers a question nobody
        asked of the twenty rows the limit dropped."""

        return ArtifactFitmentSchema(
            sizable=True,
            quantization=sizing.quantization,
            considered=len(results),
            note=_note(query.scope, candidates, sizing.shape is None),
            items=[
                _fit_schema(
                    result,
                    devices_per_node,
                    fleet.get(result.device_class),
                    catalog,
                    ladder(sizing, candidate, intent),
                )
                for result, devices_per_node, candidate, intent in head
            ],
            **header,
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

        Both given is refused, not reconciled: two ways to state identity is
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
            slug=data.slug.strip().lower() or artifact_slug(data.digest, data.locations),
            kind=data.kind,
            format=data.format,
            size_bytes=data.size_bytes,
            licence=data.licence,
            parameter_count=data.parameter_count,
            base_artifact_id=base_id,
            project_id=project_id,
            metadata_=data.metadata,
            upstream=msgspec.to_builtins(data.upstream) if data.upstream else {},
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
        artifact, _, _ = await self._one(ref, lock=True)
        check_if_match(artifact, if_match, "artifact")

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

        if "upstream" in fields:
            artifact.upstream = msgspec.to_builtins(fields.pop("upstream"))
        if "metadata" in fields:
            artifact.metadata_ = fields.pop("metadata")
        if "slug" in fields:
            # Lowercased on the way in for the reason the create path does it: a slug differing
            # from another only by case is two names for one model, and the gateway routes on it.
            fields["slug"] = str(fields["slug"]).strip().lower()
        for key, value in fields.items():
            setattr(artifact, key, value)

        # Re-derived when the locations moved and nobody said what the model is called. The slug
        # is read off a location, so replacing the set can change the answer, and an artifact left
        # holding the previous repository's name is a model the gateway rewrites requests to and
        # no frontend has registered.
        if not (artifact.slug or "").strip():
            artifact.slug = artifact_slug(artifact.digest, artifact.locations)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("artifact conflicts with an existing one") from exc
        return await self.get(artifact.uid)

    async def delete(self, ref: str) -> None:
        """Refuses while a workload still names the artifact.

        The foreign key is `RESTRICT`, so the database would refuse anyway; the
        point of catching it is that "a workload still serves this" is an answer
        and an integrity error is not.
        """
        artifact, _, _ = await self._one(ref)
        try:
            await self.repo.delete(artifact)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"artifact {ref!r} is still referenced by a workload") from exc


class ArtifactBaselineService:
    """The declared quality floors under one artifact.

    Declared state, so this is ordinary CRUD and this service owns the commit:
    a baseline is somebody's assertion about these weights, not a number the
    plane observed. `actor` is the authenticated principal and is the only source
    of `recorded_by`: the whole use of the table is that an acceptance review
    argues with a signed assertion, and a signature a caller can type is not one.
    """

    def __init__(self, db: AsyncSession, actor: str = "unknown") -> None:
        self.db = db
        self.actor = actor
        self.repo = ArtifactRepository(db)

    def schema(self, row: ArtifactBaseline, artifact_uid: str) -> ArtifactBaselineSchema:
        return ArtifactBaselineSchema(
            id=row.uid,
            artifact_id=artifact_uid,
            dataset=row.dataset,
            metric=row.metric,
            value=float(row.value),
            tolerance_pct=float(row.tolerance_pct),
            source_uri=row.source_uri,
            recorded_by=row.recorded_by,
            recorded_at=row.recorded_at,
            note=row.note,
            created_at=row.created_at,
            updated_at=row.updated_at,
            revision=revision(row),
        )

    async def _artifact(self, ref: str) -> Artifact:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no artifact {ref!r}")
        return row[0]

    async def _one(self, artifact: Artifact, baseline: str, lock: bool = False) -> ArtifactBaseline:
        row = await self.repo.baseline(artifact.id, baseline, lock=lock)
        if row is None:
            raise NotFound(f"no baseline {baseline!r} on artifact {artifact.uid!r}")
        return row

    async def list(
        self,
        ref: str,
        limit: int | None = None,
        cursor: str | None = None,
        dataset: str | None = None,
        metric: str | None = None,
    ) -> ListResponse[ArtifactBaselineSchema]:
        artifact = await self._artifact(ref)
        size = clamp_limit(limit)
        rows = await self.repo.baseline_list(
            artifact_id=artifact.id,
            limit=size,
            after_seq=decode_cursor(cursor),
            dataset=dataset,
            metric=metric,
        )
        kept, next_cursor = page(rows, size, lambda r: r.seq)
        items = [self.schema(row, artifact.uid) for row in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def get(self, ref: str, baseline: str) -> ArtifactBaselineSchema:
        artifact = await self._artifact(ref)
        return self.schema(await self._one(artifact, baseline), artifact.uid)

    async def create(self, ref: str, data: ArtifactBaselineCreateRequest) -> ArtifactBaselineSchema:
        """One floor per dataset and metric, recorded in the principal's name.

        A second row for the same pair is refused, not reconciled: two
        declared floors for one eval is two standards, and a verdict would grade
        against whichever the query returned first.
        """
        artifact = await self._artifact(ref)
        if not data.dataset.strip():
            raise Invalid("a baseline without a dataset is not about any eval")
        if not data.metric.strip():
            raise Invalid("a baseline without a metric declares no number")
        if data.tolerance_pct < 0:
            raise Invalid("tolerance_pct cannot be negative")

        baseline = ArtifactBaseline(
            uid=new_uid(ARTIFACT_BASELINE),
            artifact_id=artifact.id,
            dataset=data.dataset,
            metric=data.metric,
            value=data.value,
            tolerance_pct=data.tolerance_pct,
            source_uri=data.source_uri,
            recorded_by=self.actor,
            note=data.note,
        )
        try:
            await self.repo.add_baseline(baseline)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(
                f"artifact {artifact.uid} already declares a baseline for {data.dataset!r}/{data.metric!r}",
                {"dataset": data.dataset, "metric": data.metric},
            ) from exc
        return await self.get(artifact.uid, baseline.uid)

    async def update(
        self,
        ref: str,
        baseline: str,
        data: ArtifactBaselineUpdateRequest,
        if_match: str | None = None,
    ) -> ArtifactBaselineSchema:
        """Revises the number, never which eval it is about.

        `recorded_by` and `recorded_at` are left as first written: they say who
        declared this floor, and an edit does not transfer the assertion.
        """
        artifact = await self._artifact(ref)
        row = await self._one(artifact, baseline, lock=True)
        check_if_match(row, if_match, "baseline")

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        for name in IGNORED_PROVENANCE:
            fields.pop(name, None)
        if fields.get("tolerance_pct", 0) < 0:
            raise Invalid("tolerance_pct cannot be negative")
        for key, value in fields.items():
            setattr(row, key, value)

        await self.db.commit()
        return await self.get(artifact.uid, row.uid)

    async def delete(self, ref: str, baseline: str) -> None:
        artifact = await self._artifact(ref)
        row = await self._one(artifact, baseline)
        await self.repo.delete_baseline(row)
        await self.db.commit()
