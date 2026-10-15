"""Serialization for writes that read capacity, quota or a revision and then act on it.

Placement reads a snapshot and the caller writes deployments from it. Two callers
doing that at once both see the same free devices and the same quota headroom, and
both write — the over-commit a snapshot cannot see. The lock is per device class,
because capacity is shared across projects within a class and a quota is scoped to
one class: holding it covers both the cluster's devices and the project's ceiling.

A transaction-scoped advisory lock rather than a row lock because the thing being
protected is an aggregate with no row of its own. It is released at commit or
rollback, so there is nothing to forget to unlock.
"""

from collections.abc import Iterable

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


PLACEMENT_LOCK_PREFIX = "hangar.placement:"


async def lock_placement(db: AsyncSession, device_classes: Iterable[str]) -> None:
    """Holds the placement lock for each device class until the transaction ends.

    Taken in sorted order so two writers that need the same pair of classes cannot
    each hold one and wait for the other.
    """
    for device_class in sorted(set(device_classes)):
        key = func.hashtextextended(PLACEMENT_LOCK_PREFIX + device_class, 0)
        await db.execute(select(func.pg_advisory_xact_lock(key)))
