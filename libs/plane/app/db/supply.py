"""Shared query fragments over observed supply.

A device is free when nothing holds it, and a hold is an `Allocation`, not a
column on the device — `allocatable` says the accelerator is claimable at all,
which is a different claim from unclaimed. Every rollup that reports free or
allocated devices has to say the same thing as `app.modules.placement`, so the
predicate lives here once instead of once per repository.
"""

from sqlalchemy import ColumnElement
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select

from app.db.models import Device
from app.db.models import AllocationDevice


def device_held() -> ColumnElement[bool]:
    """True for a device some unreleased allocation still holds."""
    return (
        select(AllocationDevice.id)
        .where(
            AllocationDevice.device_id == Device.id,
            AllocationDevice.released_at.is_(None),
        )
        .exists()
    )


def device_free() -> ColumnElement[bool]:
    """True for a device that is claimable and unclaimed."""
    return Device.allocatable.is_(True) & ~device_held()


def free_device_sum() -> ColumnElement[int]:
    """Free devices in the group, as a sum that yields zero rather than null."""
    return func.sum(case((device_free(), 1), else_=0))
