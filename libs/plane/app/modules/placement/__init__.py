"""Placement: which cluster a workload goes to.

Intentionally a pure function over observed state: it takes a workload's
requirements plus a snapshot of capacity and returns a decision. It performs no
I/O, opens no session, and imports nothing from `app.db` or `app.api`, so it is
testable without a cluster and without a database.

Callers read the snapshot, call in, and persist the result themselves.

The purity rule is about `engine.py`, `snapshot.py` and `decision.py` (the three
this file re-exports), not about the directory. `repository.py` and `services.py`
sit beside them because placement has the usual module layout (see
`app/modules/__init__.py` for it and its exceptions), and they are the callers:
they do the reading, and none of the three imports them back. Nothing in this package's public surface touches a session.
"""

from .engine import place
from .engine import expand
from .decision import Share
from .decision import Rejection
from .decision import ClusterVerdict
from .decision import PreemptionPlan
from .decision import PreemptionVictim
from .decision import ExpansionDecision
from .decision import PlacementDecision
from .snapshot import DeviceHold
from .snapshot import ReservedUnit
from .snapshot import QuotaSnapshot
from .snapshot import DeviceSnapshot
from .snapshot import PolicySnapshot
from .snapshot import WorkloadDemand
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
    "ExpansionDecision",
    "PlacementDecision",
    "PlacementRequest",
    "PolicySnapshot",
    "PreemptionPlan",
    "PreemptionVictim",
    "QuotaSnapshot",
    "Rejection",
    "ReservedUnit",
    "Share",
    "TopologyDomainSnapshot",
    "WorkloadDemand",
    "expand",
    "place",
]
