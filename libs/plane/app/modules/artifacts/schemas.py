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


class ArtifactUpstreamSchema(Struct):
    """What the model's publisher says these weights are. Asserted, never observed.

    `metadata`'s opposite in the way `imported_args` is `engine_args`: a claim
    somebody signed, against a scratchpad. Nothing here reaches a cluster and
    nothing here is checked — a wrong value changes only what we told somebody.

    `reasoning` and `tool_calling` are tri-state on purpose. Null is "the
    publisher did not say", which is not `false`: a workload may still claim the
    capability on the strength of an engine recipe that names a parser for it,
    and collapsing the two would turn silence into a denial that blocks it.

    The provenance triple — `recorded_by`, `recorded_at`, `source_uri` — is what
    separates this from a guess. A resolve fills it with the Hub; a person
    editing the block replaces it with themselves.
    """

    provider: str | None = None
    provider_model_id: str | None = None
    context_length: int | None = None
    max_output_tokens: int | None = None
    input_modalities: list[str] = []
    reasoning: bool | None = None
    tool_calling: bool | None = None
    structured_output_modes: list[str] = []
    licence: str | None = None
    released_at: str | None = None
    source_uri: str | None = None
    recorded_by: str | None = None
    recorded_at: str | None = None


ArtifactUpstreamRequest = ArtifactUpstreamSchema
"""The write shape is the read shape. Replaced wholesale, the way locations are.

One struct in both directions because the block is a single assertion: a caller
that could send half of it would produce a record whose provenance triple belongs
to one claim and whose capability fields belong to another.
"""


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
    slug: str
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
    upstream: ArtifactUpstreamSchema


INFO = "info"
WARN = "warn"


class ResolveNote(Struct):
    """One remark about a resolved source, carrying how much it should alarm.

    A draft produces two kinds of remark and a flat list of strings makes them
    look alike: that a repo ships a second copy of its weights under `original/`
    is a packaging fact we handled, while weights the Hub cannot address mean the
    digest is a name rather than a hash. Rendering the first as a warning trains
    people to ignore the second.
    """

    level: str
    message: str


class ArtifactRefreshSchema(Struct):
    """The result of re-reading an artifact's own source. See `ArtifactService.refresh`.

    `changed` names the fields the re-read actually moved, dotted for metadata keys
    (`metadata.shape`). It is the point of the response: an artifact registered before
    the plane knew to extract an attention shape looks identical afterwards except for
    that one key, and a console that could only say "refreshed" would leave the person
    guessing whether anything happened. An empty list means the source still says
    exactly what we already recorded, which is a successful refresh and not a failure.

    `notes` is the draft's own remarks, unfiltered, because they are about the source
    and not about the diff — a repo that has since become gated says so here.
    """

    artifact: ArtifactSchema
    source: str
    changed: list[str]
    notes: list[ResolveNote]


class ArtifactUsageSchema(Struct):
    """What depends on this artifact, counted at read time and stored nowhere.

    `workload_count` is the distinct workloads reaching this artifact either as the
    weights they serve (`Workload.artifact_id`) or through their adapter set
    (`Workload.adapter_artifact_ids`). There is no second count beside it any more:
    a recipe used to stand between the two, and with the serving configuration on
    the workload there is one kind of dependant rather than two.

    `downstream_count` is the artifacts derived from this one (`base_artifact_id`)
    — the quantizations and fine-tunes that name it as their base.
    """

    artifact_id: str
    workload_count: int
    downstream_count: int


FLEET = "fleet"
CATALOG = "catalog"
FITMENT_SCOPES = (FLEET, CATALOG)
"""The two questions a fitment matrix can answer, and they are not one question.

`fleet` is *what can I run right now* — every class a cluster has actually reported,
and a no-fit row there is a dead end. `catalog` is *what hardware would I need* —
every class the contract table names as well, where a fit on a class we own none of
is a procurement answer rather than a deployable one. A single filtered list would
make the second look like a bug in the first.
"""


class ArtifactFitLayoutSchema(Struct):
    """One way to lay this artifact out on one class of card, of the several that work.

    A row's own fields are the recommendation — the fewest cards carrying the full
    window. These are the rest of the choices on the same class, ascending, and they are
    a real choice rather than a rounding: four H200 at 32k and eight at 131k are both
    deployable, and which one is right depends on whether the window or the free half of
    the node is worth more today.

    Counts run from the smallest that serves up to one node, or up to the recommendation
    when a replica already spans several. Nothing here that cannot serve is listed, so
    every entry is a shape a workload may declare as it stands.

    `reduced` is what the trade costs: true means this count buys its cards back by
    serving a shorter window than the one asked for, and `context_length` says which.
    `supply_short` is the fleet saying it holds fewer devices of the class than this
    layout needs — a shape that is arithmetically fine and cannot be placed today.

    `weights_gb` and `kv_counted` are absent because they belong to the artifact and not
    to the layout; they are on the row and are the same for every entry here.
    """

    device_count: int
    node_count: int
    tensor_parallel: int
    pipeline_parallel: int
    aggregate_gb: float
    required_gb: float
    context_length: int
    reduced: bool
    supply_short: bool
    reason: str


class ArtifactFitSchema(Struct):
    """Whether this artifact serves on one class of card, and what we have of it.

    The fit half is durable: it changes when the weights or the asked-for context
    change, and not otherwise. The three counts are the fleet half and move whenever
    a cluster reports. They are returned side by side rather than merged, because
    `fits` with `devices_allocatable` at zero is a capacity problem and `fits` false
    is a physics problem, and one is fixed by waiting.

    `devices_allocatable` counts the cards that are claimable and held by no live
    allocation. It is not a promise that a replica of `device_count` devices can be
    placed: contiguity within a topology domain decides that, and
    `POST /placement/preview` is what answers it.

    `node_count` is the unit to act on: "four nodes of B300" is an order, "32 devices" is
    a number to divide. `devices_per_node` is how big a node was taken to be — read from
    `supply_topology_domain.device_count` for a class the fleet runs, the caller's figure
    or the default otherwise. It is eight on an HGX board and seventy-two on a GB300
    NVL72, because the interconnect is what makes a node a node and the rack has one.

    Nodes carrying one replica sit on the same backend fabric — one connected unit for
    placement to find, not a set of islands. `tensor_parallel` is bounded by the node
    because a tensor group over the fabric is the one split that does not work;
    `pipeline_parallel` crosses freely, which is why a multi-node replica is normal.

    `aggregate_gb` is what the devices *offer*, not what the boxes say on their lids: net
    of the utilization envelope and the per-device CUDA reserve. Multiply
    `memory_mib_per_device` by `device_count` for the nameplate figure. Comparing
    `required_gb` against the nameplate would over-promise by about fifteen percent.

    `supply_short` is the fleet half saying no. It is true when the artifact fits on
    this class and the fleet holds some of it but fewer devices than one replica needs
    — a fit nobody can run today. It is deliberately not the same as `devices_total` at
    zero, which is a class we own none of and the honest procurement answer, and not the
    same as `devices_allocatable` being low, which waiting fixes. This one is fixed by
    buying cards.

    `layouts` is the rest of the answer. The fields above are the recommendation — the
    fewest cards carrying the full window — and `layouts` carries every count on this
    class that serves, so a caller can trade context for cards without asking a second
    question. The recommended count is one of them.

    `possible` is the half of `fits` a caller may refuse over, and the split matters
    because the two halves have opposite failure costs. `possible` false is arithmetic:
    the weights do not sit on the cards, or the attention heads do not divide across the
    tensor group. `fits` false with `possible` true is the estimate speaking — the window
    and the batch, sized against a flat envelope — and that is advice. A console that
    refused over it would eventually refuse a shape that serves, which is a model nobody
    can deploy and nothing to argue against; being wrong the other way is a worker that
    fails to start and says why.

    `kv_counted` false means the artifact published no attention shape, so
    `required_gb` is the weights and nothing else and `context_length` is zero. That
    row is a floor — the true requirement is strictly larger — and rendering it as a
    full sizing would promise a window nobody costed.
    """

    device_class: str
    label: str
    fits: bool
    possible: bool
    reduced: bool
    device_count: int
    node_count: int
    devices_per_node: int
    tensor_parallel: int
    pipeline_parallel: int
    memory_mib_per_device: int
    aggregate_gb: float
    weights_gb: float
    required_gb: float
    context_length: int
    kv_counted: bool
    reason: str
    devices_total: int
    devices_allocatable: int
    cluster_count: int
    supply_short: bool
    in_catalog: bool
    layouts: list[ArtifactFitLayoutSchema]


class ArtifactFitmentSchema(Struct):
    """The device classes an artifact could be served on, ranked, and the terms asked.

    Every input is echoed because a fit is meaningless without them — the same
    artifact fits on very different hardware at 8k and at 131k, and a matrix that
    did not say which would be read as a property of the weights.

    `sizable` is false only when the artifact carries no parameter count at all, in
    which case `items` is empty and `note` says why. A missing attention shape is not
    that case: the weights still bound which cards are too small, so the matrix is
    returned with every row's `kv_counted` false and `note` saying the windows are
    uncosted. Withholding the certain half because the uncertain half is absent was
    the earlier behaviour and it made the panel look broken.
    """

    artifact_id: str
    scope: str
    sizable: bool
    quantization: str
    context_length: int
    concurrency: int
    max_devices: int
    considered: int
    note: str | None
    items: list[ArtifactFitSchema]


class ArtifactFitmentRequest(Struct):
    """What to fit, and against what. Every default is defensible on its own.

    `context_length` defaults to the artifact's own declared window rather than to a
    number chosen here. An artifact that publishes 131k is fitted at 131k, and
    quietly trimming it to make more cards look workable is the inversion
    `resolve.suggest_serving` already refuses to make.

    `quantization` defaults to however the weights ship. Overriding it is how the
    question "what if we quantized this" gets asked without registering anything.

    `devices_per_node` states how many devices share one node, and applies only
    to classes the fleet has never reported — those come with an observed figure that a
    guess should not override. Stating it is how "what would an NVL72 rack of these do"
    gets asked about hardware nobody has racked.
    """

    scope: str = FLEET
    context_length: int | None = None
    concurrency: int = 1
    quantization: str | None = None
    devices_per_node: int | None = None
    max_devices: int | None = None
    limit: int | None = None


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
    slug: str = ""
    """The model id this artifact answers to. Empty derives it from the preferred location.

    Deriving is the normal case and declaring one is for weights whose location does not name
    them — a checkpoint in a bucket, a fine-tune whose path is a build id. It is lowercased on the
    way in, because a slug that differs from another only by case is two names for one model.
    """
    kind: str = "weights"
    format: str | None = None
    size_bytes: int = 0
    licence: str | None = None
    parameter_count: int | None = None
    base_artifact: str | None = None
    project: str | None = None
    metadata: dict = {}
    locations: list[ArtifactLocationRequest] = []
    upstream: ArtifactUpstreamRequest | None = None
    verified: bool = True


class ArtifactUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `digest` is absent by design: the digest is the artifact's identity, and
    different bytes are a different artifact rather than an edit to this one.
    Supplying `locations` replaces the whole set — a child collection patched
    element-wise needs identifiers the table does not have.
    """

    name: str | UnsetType = UNSET
    slug: str | UnsetType = UNSET
    kind: str | UnsetType = UNSET
    format: str | None | UnsetType = UNSET
    size_bytes: int | UnsetType = UNSET
    licence: str | None | UnsetType = UNSET
    parameter_count: int | None | UnsetType = UNSET
    project: str | None | UnsetType = UNSET
    metadata: dict | UnsetType = UNSET
    locations: list[ArtifactLocationRequest] | UnsetType = UNSET
    upstream: ArtifactUpstreamRequest | UnsetType = UNSET
