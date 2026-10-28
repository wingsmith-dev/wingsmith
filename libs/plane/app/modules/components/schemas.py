import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class ComponentStatusSchema(Struct):
    """The observed half of a component: what is actually running.

    Read-only over the API. Its single writer is ingestion, from
    `component.observed`, which is why it is a struct of its own instead of a
    handful of nullable fields hanging off the declared side.
    """

    observed_version: str | None
    rollout_phase: str
    healthy: bool
    message: str | None
    observed_at: dt.datetime | None


class ComponentSchema(Struct):
    """Declared version and observed version as a pair, never one nullable column.

    `desired_version` is what we asked a cluster to run; `status.observed_version`
    is what it reports running. `behind` is derived here so that the console and
    `wings` cannot disagree about what "behind" means, and a component nobody has
    reported on yet counts as behind: silence is not evidence of a finished
    rollout.
    """

    id: str
    cluster_id: str
    name: str
    kind: str
    desired_version: str
    config: dict
    enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    status: ComponentStatusSchema
    behind: bool


class ComponentCreateRequest(Struct):
    cluster: str
    name: str
    kind: str
    desired_version: str
    config: dict = {}
    enabled: bool = True


class ComponentUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `cluster` and `name` are absent by design. Together they are the component's
    natural key and the only thing `component.observed` carries to join a status
    back onto its declaration, so renaming one would silently orphan its observed
    half, not move it.
    """

    kind: str | UnsetType = UNSET
    desired_version: str | UnsetType = UNSET
    config: dict | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
