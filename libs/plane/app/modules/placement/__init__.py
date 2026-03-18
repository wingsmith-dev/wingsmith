"""Placement — which cluster a workload goes to.

Deliberately a pure function over observed state: it takes a workload's
requirements plus a snapshot of capacity and returns a decision. It performs no
I/O, opens no session, and imports nothing from `app.db` or `app.api`, so it is
testable without a cluster and without a database.

Callers read the snapshot, call in, and persist the result themselves.
"""

from .engine import place
from .decision import Rejection
from .decision import ClusterVerdict
from .decision import PreemptionPlan
from .decision import PreemptionVictim
from .decision import PlacementDecision
from .snapshot import DeviceHold
from .snapshot import QuotaSnapshot
from .snapshot import DeviceSnapshot
from .snapshot import PolicySnapshot
from .snapshot import ClusterSnapshot
from .snapshot import CapacitySnapshot
from .snapshot import PlacementRequest
from .snapshot import TopologyDomainSnapshot


__all__ = [
    "CapacitySnapshot",
    "ClusterSnapshot",
    "ClusterVerdict",
    "DeviceHold",
    "DeviceSnapshot",
    "PlacementDecision",
    "PlacementRequest",
    "PolicySnapshot",
    "PreemptionPlan",
    "PreemptionVictim",
    "QuotaSnapshot",
    "Rejection",
    "TopologyDomainSnapshot",
    "place",
]
