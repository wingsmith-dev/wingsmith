import datetime as dt

from app.core.schemas import Struct


class BenchmarkProvenanceSchema(Struct):
    """What was running when the run was taken, snapshotted, not joined.

    A workload is mutable and a deployment is disposable, so a run that resolved
    these by join would start describing something else the moment either moved
    on. Copied at creation, these are what make a table of runs a tuning record
    instead of a pile of unattributed numbers.

    `serving_digest` is what the run is about, and it is the one field here that
    cannot go stale by construction: it is the content of the configuration
    instead of a handle on it. The pair it replaced, a recipe id and a version,
    named a row that could be edited under the run.

    Every field is nullable-or-empty because an endpoint-level run has none of
    them. A request to a public name is routed by weight across every enabled
    upstream, so a run dispatched "for" one deployment measures whatever the
    gateway chose, usually not that one. An accurate empty snapshot beats a
    confident wrong attribution, and `level` on the request records why it is
    empty.

    `serving` is the workload's declared serving block beside its engine
    arguments: which protocols answer, whether thinking mode is on, what the
    context ceiling is. Without it a conformance result cannot be attributed to
    what the surface claimed to be.
    """

    model: str
    deployment_generation: int
    serving_digest: str | None
    engine_args: dict
    serving: dict


class BenchmarkParameterSchema(Struct):
    """One knob the harness accepts, so a client can render a form without a copy of the table."""

    name: str
    type: str
    default: str | int | float | bool
    description: str
    levels: list[str]


class BenchmarkHarnessSchema(Struct):
    """A harness this plane can point at a serving surface, and where it may be pointed.

    `levels` is served, not assumed by the console for the same reason the
    parameter table is: a second copy of it is a second answer to what a probe
    suite may be run against. It lists only the levels something executes today
    (`harnesses.RUNNABLE_LEVELS`), so `endpoint` is absent until its executor exists.
    """

    name: str
    image: str
    description: str
    levels: list[str]
    parameters: list[BenchmarkParameterSchema]
    max_probes: int | None = None


class BenchmarkCheckSchema(Struct):
    """One graded assertion, with the number it graded and the number it graded against.

    `observed` and `threshold` are both carried so a reader can see how close a
    pass was, which is the question asked immediately after a pass.
    """

    id: str
    label: str
    outcome: str
    observed: float | None
    threshold: float | None
    comparator: str
    detail: str | None


class BenchmarkVerdictSchema(Struct):
    """Whether a run met the standard, derived at read against a pinned criteria version.

    Never stored. `criteria_version` is what makes deriving it accurate: a verdict
    computed fresh against a table somebody may edit is a verdict that silently
    changes meaning, which is worse than a stored one because a stored one at
    least does not lie about the past.
    """

    outcome: str
    criteria_version: str
    reason: str | None
    checks: list[BenchmarkCheckSchema]


class BenchmarkBandSchema(Struct):
    """One input-length band of a request, re-aggregated from the runs that landed in it.

    The standard sets a time-to-first-token target per band, so a single
    aggregate over a whole sweep evidences none of them. `p90` and `p50` are the
    worst of the band because percentiles do not average; `avg` is weighted by
    request count, which does. `incomplete` names the runs that reported no
    time-to-first-token at all, because a band that quietly measured half of
    what it claims is the failure this whole read exists to surface.
    """

    band: str
    runs: int
    requests: int
    ttft_p90_ms: float | None
    ttft_p50_ms: float | None
    ttft_avg_ms: float | None
    incomplete: list[str]


class BenchmarkReadingSchema(Struct):
    """One number of one run, with the path it was read from and the unit it is in.

    A reading that did not resolve is a null value beside the path that was
    looked for: a named miss, never a silent zero. A curve with a zero at one
    step draws a cliff the machine did not have, and a client cannot tell that
    zero from a measured one.
    """

    value: float | None
    unit: str
    path: str


class BenchmarkCurvePointSchema(Struct):
    """One step of a request, with every reading a chart draws.

    `readings` carries graded and display-only numbers side by side.
    `output_tps_total` and `request_throughput` are display-only: they are
    reached through the half of the field map no verdict can see, because an
    aggregate rate clears a per-stream floor under every possible condition and
    a check that could reach it would be a check that can never fail.

    A discarded run is still a point, flagged, not dropped, a warm-up is
    part of what was run, and it is excluded from the knee. A run still open is
    a point too, with null readings, so the console can draw a partial curve
    while the ramp is still climbing.
    """

    step_index: int
    step_value: int | None
    run: str
    phase: str
    discard: bool
    readings: dict[str, BenchmarkReadingSchema]


class BenchmarkKneeSchema(Struct):
    """Where the anchored metric first crossed its ceiling, derived at read and stored nowhere.

    Always an object and never absent: `found` is false far more often than it
    is true, and `reason` says which false it is. A ramp that never crossed, a
    ramp that started above the ceiling and a ramp too short to have a crossing
    are three different answers, and an operator who cannot tell them apart
    cannot tell whether to ramp further.

    `threshold` comes from the pinned criteria version, through the band the
    steps landed in. It is not a verdict and rolls up into none: a knee is a
    property of the machine, and whether the machine met the standard is
    `/verdict`.
    """

    found: bool
    step_index: int | None
    step_value: int | None
    method: str
    metric: str
    threshold: float | None
    reason: str


class BenchmarkCurveSchema(Struct):
    """A stepped request's runs ordered by step, with the knee derived now.

    Nothing here is stored. The curve and the knee are derived at read against
    the criteria version the request was created under, exactly as the verdict
    is, because a stored knee cannot answer the question a criteria revision
    immediately raises, where would the elbow have been under the new rules.

    `serving_digest` is the configuration the curve is about, and it is null
    when the request's runs did not all measure the same one: a curve drawn
    through two configurations describes a machine that does not exist.
    """

    request: str
    serving_digest: str | None
    step_field: str
    criteria_version: str
    points: list[BenchmarkCurvePointSchema]
    knee: BenchmarkKneeSchema


class BenchmarkCoverageSchema(Struct):
    """One thing that was behind the target when a request expanded.

    Snapshotted at expansion and allowed to go stale loudly: a workload rescaled
    mid-run does not retroactively change what was measured, and a reader who
    compares this to the target's current resolution can see the drift.
    """

    deployment_id: str | None
    cluster_id: str | None
    serving_digest: str | None
    artifact_id: str | None


class BenchmarkRunSchema(Struct):
    """A measurement of one target, and what the harness said about it.

    `summary` is the harness's own output, verbatim and unparsed. Two runs are
    comparable when `harness` and `harness_version` agree; across harnesses, or
    across a version that redefined a percentile, they are not, and this plane
    does not pretend otherwise by flattening them into shared columns.

    `operation_id` names the errand that carried the run out. It is nullable
    because the record outlives it.

    `deployment_id` and `cluster_id` are nullable because an endpoint-level run
    has neither; `request_id` names the ask it was expanded from and
    `step_index` orders the runs of one sweep. `discard` marks a warm-up: a row
    that can be looked at when the warm-up was itself the thing that failed, and
    that no verdict rollup counts.
    """

    id: str
    request_id: str | None
    deployment_id: str | None
    cluster_id: str | None
    endpoint_id: str | None
    operation_id: str | None
    harness: str
    harness_version: str | None
    image: str
    parameters: dict
    step_index: int
    vantage: str | None
    discard: bool
    requested_by: str
    reason: str | None
    provenance: BenchmarkProvenanceSchema
    phase: str
    summary: dict | None
    artifacts_uri: str | None
    error: str | None
    log_tail: str | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
    started_at: dt.datetime | None
    finished_at: dt.datetime | None


class BenchmarkRequestSchema(Struct):
    """What was asked for, at which level, and against which frozen standard.

    Carries no phase, no summary and no verdict of its own. Its phase is derived
    from its runs and so is its verdict, because a request that stored either
    would be an operation modelled as a status field, and because a stored
    verdict cannot answer "would this have passed under the new rules", which is
    the question a criteria revision immediately raises.
    """

    id: str
    level: str
    target_id: str
    harness: str
    image: str
    parameters: dict
    mode: str
    criteria_version: str
    tier: str | None
    homogeneous: bool
    coverage: list[BenchmarkCoverageSchema]
    requested_by: str
    reason: str | None
    phase: str
    runs: list[str]
    created_at: dt.datetime
    updated_at: dt.datetime | None


class BenchmarkRunCreateRequest(Struct):
    """`POST /v1/benchmarks` with the deployment named in the body.

    The deployment is a field instead of a path segment for the same reason an
    operation's target is: the run is the object being created, and hanging a
    creation off `/deployments/{id}/benchmarks` would make the collection a
    sub-resource of one of the several things it can be filtered by.

    `parameters` is whatever the named harness accepts. Anything omitted takes
    the harness default, and anything it does not accept is a 400 here rather
    than a job that fails in a cluster ten minutes from now.

    `image` overrides the pinned harness image for this run alone. Omitted, the
    run takes the harness default, which is what almost every run should do:
    a table of runs is comparable because the tool that produced them did not
    move. Set it to reach a newer release without waiting on a plane deploy, and
    accept that a run pinned elsewhere is its own comparison group.

    This is the deployment-level shortcut and it is kept because it is the shape
    every existing caller posts. It expands into the same request-and-runs pair
    a levelled request does; there is one creation path, not two.
    """

    deployment: str
    harness: str = "aiperf"
    image: str | None = None
    reason: str | None = None
    parameters: dict = {}
    criteria: str | None = None
    tier: str | None = None
    deadline: dt.datetime | None = None


class BenchmarkRequestCreateRequest(Struct):
    """`POST /v1/benchmarks/requests`: what to measure, and at which level.

    `target` is a deployment, a workload or an endpoint, and `level` says which,
    because the three are different claims that must not be averaged: a
    deployment run measures one cluster's configuration, a workload run measures
    the serving configuration, and an endpoint run measures what a caller
    actually gets. A
    harness is refused at a level it has nothing to say at.

    `criteria` pins the standard the runs will be graded against. It is
    snapshotted here for the same reason `image` is: a verdict derived against a
    table that has since moved is a verdict that quietly changed its mind.

    `tier` is the service class the buyer assigned, L1 or L2. It is not derivable
    from anything this plane holds and an unstated one is `inconclusive` rather
    than the lenient branch.

    `mode` is the shape of the runs, and `steps` are the load points a stepped
    one walks: concurrencies for a `ramp`, prompt lengths for a `sweep`. A
    sweep is what makes a per-band time-to-first-token claim evidenceable: one
    invocation measures one load point, and the standard sets a target for each
    of six input-length bands.
    """

    level: str
    target: str
    harness: str = "aiperf"
    image: str | None = None
    mode: str = "single"
    steps: list[int] = []
    criteria: str | None = None
    tier: str | None = None
    reason: str | None = None
    parameters: dict = {}
    deadline: dt.datetime | None = None


class ServingBenchmarkSummarySchema(Struct):
    """Offline benchmark measurement rolled up per `serving_digest`, computed at read time.

    Keyed on the digest, not on a workload, because the digest is what a
    measurement is actually about. Two workloads that declare the same engine,
    the same parallelism and the same cache settings against the same artifact
    produce the same running process, so a number taken against one of them is a
    number about the other; rolling up per workload would split one population in
    two and report each half as the peak. It also survives what a workload does
    not (renaming, rescaling, deletion) because the digest is content rather
    than identity, and a run keeps naming the configuration it measured after the
    row that asked for it has gone. This is what the recipe rollup was reaching
    for when it keyed on a mutable recipe id and version.

    `peak_output_tokens_per_second` is the highest per-stream output-token
    throughput any graded run of this configuration reported, read through the
    same harness-and-version adapter the verdicts use and never through a
    normalised metric schema. It is `None` when no run carried a reading this
    plane knows how to find: a named absence, not a zero. This is held-in-plane
    offline measurement, never live serving telemetry.
    """

    serving_digest: str
    run_count: int
    graded_run_count: int
    peak_output_tokens_per_second: float | None
