import datetime as dt

from app.core.schemas import Struct


class BenchmarkProvenanceSchema(Struct):
    """What was running when the run was taken, snapshotted rather than joined.

    A recipe is mutable and a deployment is disposable, so a run that resolved
    these by join would start describing something else the moment either moved
    on. Copied at creation, these are what make a table of runs a tuning record
    instead of a pile of unattributed numbers.
    """

    model: str
    deployment_generation: int
    recipe_id: str | None
    recipe_version: int | None
    engine_args: dict


class BenchmarkParameterSchema(Struct):
    """One knob the harness accepts, so a client can render a form without a copy of the table."""

    name: str
    type: str
    default: str | int | bool
    description: str


class BenchmarkHarnessSchema(Struct):
    """A load generator this plane can point at a deployment."""

    name: str
    image: str
    description: str
    parameters: list[BenchmarkParameterSchema]


class BenchmarkRunSchema(Struct):
    """A measurement of one deployment, and what the harness said about it.

    `summary` is the harness's own output, verbatim and unparsed. Two runs are
    comparable when `harness` and `harness_version` agree; across harnesses, or
    across a version that redefined a percentile, they are not, and this plane
    does not pretend otherwise by flattening them into shared columns.

    `operation_id` names the errand that carried the run out. It is nullable
    because the record outlives it.
    """

    id: str
    deployment_id: str
    cluster_id: str
    operation_id: str | None
    harness: str
    harness_version: str | None
    image: str
    parameters: dict
    requested_by: str
    reason: str | None
    provenance: BenchmarkProvenanceSchema
    phase: str
    summary: dict | None
    artifacts_uri: str | None
    error: str | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
    started_at: dt.datetime | None
    finished_at: dt.datetime | None


class BenchmarkRunCreateRequest(Struct):
    """`POST /v1/benchmarks` with the deployment named in the body.

    The deployment is a field rather than a path segment for the same reason an
    operation's target is: the run is the object being created, and hanging a
    creation off `/deployments/{id}/benchmarks` would make the collection a
    sub-resource of one of the several things it can be filtered by.

    `parameters` is whatever the named harness accepts. Anything omitted takes
    the harness default, and anything it does not accept is a 400 here rather
    than a job that fails in a cluster ten minutes from now.

    `image` overrides the pinned harness image for this run alone. Omitted, the
    run takes the harness default, which is what almost every run should do —
    a table of runs is comparable because the tool that produced them did not
    move. Set it to reach a newer release without waiting on a plane deploy, and
    accept that a run pinned elsewhere is its own comparison group.
    """

    deployment: str
    harness: str = "aiperf"
    image: str | None = None
    reason: str | None = None
    parameters: dict = {}
    deadline: dt.datetime | None = None
