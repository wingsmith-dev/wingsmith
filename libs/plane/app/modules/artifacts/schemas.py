import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class ArtifactLocationSchema(Struct):
    """One place an artifact's bytes can be fetched from.

    A child collection rather than a column, because locations are mutable and
    plural while the artifact they belong to is neither. There is no `id`: the
    table carries no prefixed identifier, and `uri` is unique within the
    artifact, so the uri is the handle.
    """

    scheme: str
    uri: str
    region: str | None
    preferred: bool


class ArtifactSchema(Struct):
    """Immutable, content-addressed bytes. Identity is the digest.

    A fine-tune output, a private checkpoint, a quantized derivative, and a LoRA
    adapter are all artifacts, which is why `kind` is a field here rather than
    the difference between this object and a catalog entry.
    """

    id: str
    digest: str
    verified: bool
    name: str
    kind: str
    format: str | None
    size_bytes: int
    licence: str | None
    parameter_count: int | None
    base_artifact_id: str | None
    project_id: str | None
    metadata: dict
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    locations: list[ArtifactLocationSchema]


class ArtifactUsageSchema(Struct):
    """What depends on this artifact, counted at read time and stored nowhere.

    `recipe_count` is the recipes that serve these weights (`Recipe.artifact_id`).
    `workload_count` is the distinct workloads reaching this artifact either
    through their recipe's weights or through their adapter set
    (`Workload.adapter_artifact_ids`). `downstream_count` is the artifacts
    derived from this one (`base_artifact_id`) — the quantizations and fine-tunes
    that name it as their base.
    """

    artifact_id: str
    recipe_count: int
    workload_count: int
    downstream_count: int


class ArtifactBaselineSchema(Struct):
    """One declared quality floor, per dataset and metric.

    A sub-resource rather than a field on the artifact: the set is genuinely
    plural, each row carries its own tolerance and its own provenance, and
    editing one is not a rewrite of the rest.

    `tolerance_pct` is the recorder's own slack and is not the acceptance floor —
    that belongs to the criteria version and is resolved when a verdict is
    derived (`app/modules/benchmarks/verdicts.py`). `value` and `tolerance_pct`
    are `Numeric` on the row and floats here, converted once at this seam so no
    `Decimal` meets a float threshold halfway through an expression.
    """

    id: str
    artifact_id: str
    dataset: str
    metric: str
    value: float
    tolerance_pct: float
    source_uri: str | None
    recorded_by: str
    recorded_at: dt.datetime
    note: str | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str


class ArtifactBaselineCreateRequest(Struct):
    """`dataset` and `metric` are the row's identity; one pair per artifact.

    `recorded_by` and `recorded_at` are declared here only so that a body still
    carrying them is accepted and discarded. Who asserted a floor is the
    authenticated principal and when is the write, and a caller that could type
    either could sign the assertion in somebody else's name — which is the one
    thing an acceptance argument rests on.
    """

    dataset: str
    metric: str
    value: float
    tolerance_pct: float = 3.0
    source_uri: str | None = None
    note: str | None = None
    recorded_by: str | None = None
    recorded_at: dt.datetime | None = None


class ArtifactBaselineUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `dataset` and `metric` are absent: they identify the row within the artifact,
    so moving a floor to another eval is a different baseline and not an edit to
    this one. `recorded_by` and `recorded_at` are ignored exactly as they are on
    create.
    """

    value: float | UnsetType = UNSET
    tolerance_pct: float | UnsetType = UNSET
    source_uri: str | None | UnsetType = UNSET
    note: str | None | UnsetType = UNSET
    recorded_by: str | None | UnsetType = UNSET
    recorded_at: dt.datetime | None | UnsetType = UNSET


class ArtifactLocationRequest(Struct):
    scheme: str
    uri: str
    region: str | None = None
    preferred: bool = False


class ArtifactCreateRequest(Struct):
    """`verified` defaults true because registering a digest is a claim about bytes.

    `source` is the alternative to `digest`, never a companion to it: give a
    `hf://owner/name` and the plane derives the digest from the Hub manifest, which
    is the only way two callers registering the same weights land on one artifact.
    Everything else the caller passes explicitly still wins over what was resolved.

    It is false only for weights adopted by reference — a `--model` read off a
    workload that was already running, where the digest is a name and nothing has
    hashed anything. There is no update field for it: verification is resolving
    the bytes, which is an operation, not an edit.
    """

    digest: str = ""
    source: str | None = None
    name: str = ""
    kind: str = "weights"
    format: str | None = None
    size_bytes: int = 0
    licence: str | None = None
    parameter_count: int | None = None
    base_artifact: str | None = None
    project: str | None = None
    metadata: dict = {}
    locations: list[ArtifactLocationRequest] = []
    verified: bool = True


class ArtifactUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `digest` is absent by design: the digest is the artifact's identity, and
    different bytes are a different artifact rather than an edit to this one.
    Supplying `locations` replaces the whole set — a child collection patched
    element-wise needs identifiers the table does not have.
    """

    name: str | UnsetType = UNSET
    kind: str | UnsetType = UNSET
    format: str | None | UnsetType = UNSET
    size_bytes: int | UnsetType = UNSET
    licence: str | None | UnsetType = UNSET
    parameter_count: int | None | UnsetType = UNSET
    project: str | None | UnsetType = UNSET
    metadata: dict | UnsetType = UNSET
    locations: list[ArtifactLocationRequest] | UnsetType = UNSET
