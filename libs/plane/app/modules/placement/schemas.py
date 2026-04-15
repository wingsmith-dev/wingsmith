from app.core.schemas import Struct


class PlacementPreviewRequest(Struct):
    """A placement question asked without creating anything.

    The same three inputs a deployment supplies — what to run, who for, and at
    what priority — because a preview that took a different shape from the real
    call would be a second scheduler to keep in step.
    """

    recipe: str
    project: str
    priority_class: str | None = None
    replicas: int = 1


class PlacementExpandRequest(Struct):
    """The split an existing workload would get, asked without writing a row.

    Takes a workload rather than a recipe because a split is a workload question:
    the totals, the strategy, the floors and the fan-out cap all live there, and a
    preview built from anything less would answer about a different workload.
    """

    workload: str
