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


class ArtifactLocationRequest(Struct):
    scheme: str
    uri: str
    region: str | None = None
    preferred: bool = False


class ArtifactCreateRequest(Struct):
    """`verified` defaults true because registering a digest is a claim about bytes.

    It is false only for weights adopted by reference — a `--model` read off a
    workload that was already running, where the digest is a name and nothing has
    hashed anything. There is no update field for it: verification is resolving
    the bytes, which is an operation, not an edit.
    """

    digest: str
    name: str
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
