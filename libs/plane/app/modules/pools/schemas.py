import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class PoolMembershipSchema(Struct):
    """The observed half of a pool: what is actually in it right now.

    Membership is a rollup, not a list. A pool with three hundred nodes still
    answers this question in one row, and a client that wants the nodes asks
    `GET /v1/nodes?pool=...` — the same reason sub-resources are addressed by
    filter rather than by nesting.
    """

    nodes_total: int
    nodes_ready: int
    nodes_cordoned: int
    devices_total: int
    devices_healthy: int
    devices_allocatable: int


class PoolSchema(Struct):
    """Declared spec and observed membership side by side, never merged.

    The declared half is the provisioning intent — device class, shape, and the
    node bounds a scaling loop reads. The observed half is `membership`.
    `within_declared_bounds` is computed here rather than left to each client,
    because both the console and `hgr` need the same answer to "is this pool
    outside what we asked for" and two clients deriving it independently is two
    chances to derive it differently.
    """

    id: str
    cluster_id: str
    name: str
    device_class: str
    devices_per_node: int
    node_shape: dict
    min_nodes: int
    max_nodes: int
    labels: dict
    taints: list
    enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    membership: PoolMembershipSchema
    within_declared_bounds: bool


class PoolCreateRequest(Struct):
    cluster: str
    name: str
    device_class: str
    devices_per_node: int = 0
    node_shape: dict = {}
    min_nodes: int = 0
    max_nodes: int = 0
    labels: dict = {}
    taints: list = []
    enabled: bool = True


class PoolUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `cluster` is absent by design: moving a pool between clusters is not an edit,
    it is a different pool, and the nodes underneath it would not move with it.
    """

    device_class: str | UnsetType = UNSET
    devices_per_node: int | UnsetType = UNSET
    node_shape: dict | UnsetType = UNSET
    min_nodes: int | UnsetType = UNSET
    max_nodes: int | UnsetType = UNSET
    labels: dict | UnsetType = UNSET
    taints: list | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
