"""What a run's phase does when its operation ends without one being reported.

A benchmark's phase is written by ingestion from `benchmark.observed`, and that
frame is sent by the executor that ran the harness. Everything that goes wrong
*before* the executor reaches the harness therefore reports nothing: a cluster
that refused the kind, an operator too old to know it, a cancellation while the
operation was still pending, a cluster that never came back. Each of those left
a run `pending` for ever with nothing on the row saying why, which is the exact
failure operations exist to make impossible.

So a terminal operation carries its phase across. The guard is that the run must
still be open: a run that has already reported is the more authoritative of the
two, because it was written by the thing that actually did the work, and its
answer must survive an operation frame arriving after it.

Its own module, and importing nothing from `app.modules.operations`, because the
operations service is one of the two callers and the other is ingestion. A
helper either of them owned would have to be imported by the other.
"""

import datetime as dt

from sqlalchemy import Update
from sqlalchemy import select
from sqlalchemy import update

from app.db.models import Operation
from app.db.models import BenchmarkRun


OPEN_PHASES = frozenset({"pending", "running"})
"""Restated here, not imported, to keep this module free of its callers.

Three words in two places is the cheaper of the two costs; the other is a cycle
between `operations` and `benchmarks`.
"""

DEFAULT_MESSAGES = {
    "cancelled": "the operation carrying this run was cancelled before it reported a result",
    "timed_out": "the operation carrying this run passed its deadline without reporting a result",
    "failed": "the operation carrying this run failed before it reported a result",
    "succeeded": "the operation carrying this run finished without reporting a result",
}
"""Why a run ended, when the only thing that knows is the operation.

`succeeded` is listed because an operation that succeeded while its run never reported
means the executor finished and sent nothing; the run fails instead of claiming a
measurement of nothing.
"""


def follows_operation(operation_uid: str, phase: str, error: str | None = None) -> Update:
    """Carries an operation's terminal phase onto the run it was carrying out.

    Matched through `operation_id`, not by passing the run down, because
    the caller that cancels an operation has no reason to know a run exists
    (most operations have none), and the caller in ingestion is holding a wire
    payload that names only the operation.
    """
    message = error or DEFAULT_MESSAGES.get(phase)
    return (
        update(BenchmarkRun)
        .where(
            BenchmarkRun.operation_id.in_(select(Operation.id).where(Operation.uid == operation_uid)),
            BenchmarkRun.phase.in_(OPEN_PHASES),
        )
        .values(phase=phase, finished_at=dt.datetime.now(dt.UTC), error=message)
    )
