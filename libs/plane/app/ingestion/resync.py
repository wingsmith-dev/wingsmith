"""The prune guard, kept pure so the dangerous decision is testable on its own.

The mirror of `libs/operator/internal/reconcile/store.go` on the plane side, and
it fails the same way: every path that is not "the snapshot arrived whole"
results in no prune. Deleting a cluster's entire observed inventory because a
truncated snapshot looked authoritative is the only outcome this code exists to
prevent, so an epoch lost to a crash, a disconnect, or a failed apply can only
ever cost a stale row.
"""

from typing import NamedTuple
from dataclasses import field
from dataclasses import dataclass


NODE = "node"
DEVICE = "device"
TOPOLOGY = "topology"
WORKLOAD = "workload"
REPLICA = "replica"
ALLOCATION = "allocation"


@dataclass
class ResyncEpoch:
    """One open snapshot, accumulated in memory between `resync.begin` and `stream.end`.

    In memory and nowhere else, deliberately: the epoch is the sole authority
    for pruning, so losing it is indistinguishable from never having opened one.
    """

    cluster_uid: str
    scope: str
    request_id: str
    present: dict[str, set[str]] = field(
        default_factory=lambda: {
            NODE: set(),
            DEVICE: set(),
            TOPOLOGY: set(),
            WORKLOAD: set(),
            REPLICA: set(),
            ALLOCATION: set(),
        }
    )
    received: int = 0
    poisoned: bool = False

    def record(self, kind: str, uid: str) -> None:
        self.received += 1
        self.present[kind].add(uid)

    def poison(self) -> None:
        self.poisoned = True


class PruneDecision(NamedTuple):
    prune: bool
    reason: str


def decide(epoch: ResyncEpoch | None, count: int, complete: bool) -> PruneDecision:
    """Whether a closing snapshot may be pruned against.

    Four ways to say no and one to say yes. The count check is what catches a
    stream that ended cleanly but lost frames on the way: `complete` is the
    sender's claim, `received` is what actually landed, and only their agreement
    makes absence from the snapshot evidence of anything.
    """
    if epoch is None:
        return PruneDecision(False, "no open epoch")
    if epoch.poisoned:
        return PruneDecision(False, "an apply failed during the snapshot")
    if not complete:
        return PruneDecision(False, "snapshot did not complete")
    if count != epoch.received:
        return PruneDecision(False, f"snapshot truncated: declared {count}, received {epoch.received}")
    return PruneDecision(True, "snapshot complete")
