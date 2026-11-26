"""What counts as a workload's spec, and the generation that counts changes to it.

The spec is every declared column plus the disaggregated roles. Status is the short list below:
observed or derived values that ingestion and rollups write. A new column is spec by default, so
forgetting to classify it costs a spurious generation, never a missed one.

`serving_digest` is left out on purpose. It is derived from the declared columns, which are
already compared, and how it is built may change (custom workers redefine it); the generation
must not move or stay put because of that.
"""

import copy

from decimal import Decimal

import sqlalchemy as sa

from app.db.models import Workload
from app.db.models import WorkloadRole


STATUS = frozenset(
    {
        "id",
        "uid",
        "seq",
        "created_at",
        "updated_at",
        "generation",
        "serving_digest",
        "imported_args",
        "started_at",
        "finished_at",
    }
)
SPEC = tuple(c.key for c in sa.inspect(Workload).column_attrs if c.key not in STATUS)


def spec_of(workload: Workload, roles: list[WorkloadRole]) -> tuple:
    """A comparable snapshot of the declared spec."""
    declared = tuple(_plain(getattr(workload, key)) for key in SPEC)
    shaped = tuple(
        sorted(
            ((r.role, r.weight, r.max_batch_tokens, copy.deepcopy(r.engine_args)) for r in roles), key=lambda r: r[0]
        )
    )
    return declared, shaped


def _plain(value: object) -> object:
    # A Numeric column reads back as Decimal and is written as float; equal values must compare equal.
    # JSONB values are copied so an in-place mutation after the snapshot still reads as a change.
    if isinstance(value, Decimal):
        return float(value)
    return copy.deepcopy(value) if isinstance(value, (dict, list)) else value


def bump_spec(workload: Workload) -> None:
    """Records that the declared spec changed. Only spec writers call it."""
    workload.generation = (workload.generation or 1) + 1
