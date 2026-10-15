"""Budgets for failed credential checks, counted in this process's memory.

There is no general rate limiter in the plane and this is not one. It exists for
the paths where a caller may present a credential the plane did not issue — the
sign-in form and agent token verification — and its job is to make a run of
refusals cost the caller time and show up in the logs, rather than to enforce a
quota.

The sign-in budgets are keyed by who is failing (`KeyedFailureBudget`, keyed by
`client_address` and the account named), so one caller spending its budget
locks out itself and not everyone else.

Deliberately per-process and in memory. What is being bounded is work *this*
process does, so a counter in Postgres would add a write to the path a refusal is
supposed to make cheap, and a Redis nobody deploys yet would add a dependency to
the path that authenticates the data plane. Two API replicas therefore each hold
their own budget: the effective fleet-wide limit is the limit times the replica
count, which is the tradeoff and is fine for a window whose purpose is to be
slow and loud rather than exact.
"""

import time
import ipaddress

from collections import OrderedDict
from collections import deque

from litestar.connection import ASGIConnection

from app.config import settings


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


class KeyedFailureBudget:
    """A `FailureBudget` per key, so failures are charged to whoever made them.

    Keys are evicted oldest-first past `max_keys`, which bounds the memory an
    anonymous caller can make this hold by inventing addresses. An evicted key
    forgets its failures, which costs at most one more window of guesses from a
    caller already spreading itself across more keys than the cap.
    """

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._budgets: OrderedDict[str, FailureBudget] = OrderedDict()

    def spent(self, key: str) -> bool:
        budget = self._budgets.get(key)
        if budget is None:
            return False
        if budget.spent():
            return True
        if not budget._failures:
            del self._budgets[key]
        return False

    def fail(self, key: str) -> None:
        budget = self._budgets.get(key)
        if budget is None:
            budget = self._budgets[key] = FailureBudget(self.limit, self.window_seconds)
            while len(self._budgets) > self.max_keys:
                self._budgets.popitem(last=False)
        else:
            self._budgets.move_to_end(key)
        budget.fail()

    def clear(self) -> None:
        self._budgets.clear()


def _networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(part.strip(), strict=False) for part in raw.split(",") if part.strip()]


def _trusted(address: str, networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def _bucket(address: str) -> str:
    """The address a budget is charged to: an IPv6 caller is its /64, because
    one host is routinely handed a whole /64 and could otherwise mint a fresh
    budget per request."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def client_address(connection: ASGIConnection) -> str:
    """The address of whoever is calling, for charging failures to.

    The socket peer, unless the peer is one of `TRUSTED_PROXIES`. Then the
    `X-Forwarded-For` chain is read right to left, skipping trusted hops, and the
    first address no trusted proxy vouches for is the caller. Reading it from
    the left would let the caller name any address it likes. With no trusted
    proxies configured the header is ignored entirely.
    """
    peer = connection.client.host if connection.client else ""
    trusted = _networks(settings.TRUSTED_PROXIES)
    if trusted and _trusted(peer, trusted):
        chain = [hop.strip() for hop in connection.headers.get("X-Forwarded-For", "").split(",") if hop.strip()]
        for hop in reversed(chain):
            if not _trusted(hop, trusted):
                return _bucket(hop)
        if chain:
            return _bucket(chain[0])
    return _bucket(peer)
