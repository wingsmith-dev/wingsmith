"""A budget for failed credential checks, counted in this process's memory.

There is no general rate limiter in the plane and this is not one. It exists for
the one path where a caller may present a credential the plane did not issue —
agent token verification — and its job is to make a run of refusals cost the
caller time and show up in the logs, rather than to enforce a quota.

Deliberately per-process and in memory. What is being bounded is work *this*
process does, so a counter in Postgres would add a write to the path a refusal is
supposed to make cheap, and a Redis nobody deploys yet would add a dependency to
the path that authenticates the data plane. Two API replicas therefore each hold
their own budget: the effective fleet-wide limit is the limit times the replica
count, which is the tradeoff and is fine for a window whose purpose is to be
slow and loud rather than exact.
"""

import time

from collections import deque


class FailureBudget:
    """How many failures may land in a rolling window before callers are refused.

    `spent` is what a caller consults *before* doing the work, so the check stays
    cheaper than the thing it is protecting; `fail` is called only for an outcome
    that is somebody's fault. An outcome that is the fleet's own fault — a known
    credential belonging to an object that is out of service — must not be
    recorded here, or one misconfigured cluster's retry loop spends the budget
    every other cluster needs.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._failures: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._failures and now - self._failures[0] > self.window_seconds:
            self._failures.popleft()

    def spent(self) -> bool:
        now = time.monotonic()
        self._prune(now)
        return len(self._failures) >= self.limit

    def fail(self) -> None:
        self._failures.append(time.monotonic())

    def clear(self) -> None:
        self._failures.clear()
