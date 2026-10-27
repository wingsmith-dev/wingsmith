"""Fetching the numbers a metric rule is judged on, so that the judging stays pure.

`rules.py` is intentionally free of I/O and this file is the reason it can stay that
way: everything that has to talk to a measurement store lives here, and what crosses
back into `rules` is a plain float or nothing at all.

**Nothing is a first-class answer.** A `Reading` with no value carries the reason it
has none: no source configured for the cluster, the store refused, the deployment
has no dialect, the series is empty. `metric_breach` never alerts on one, so a rule
cannot mistake silence for a number, and the reason is kept, not discarded
because "why is this not being evaluated" is the question that gets asked about an
alert that never fired.

Reads go through `app.modules.metrics`, which is the only thing that knows a query
language. This module names Hangar metrics and Hangar subjects and nothing else,
which is what makes a second backend invisible here. Rule 4 is not in play: the
reactor reads an exporter about the data plane, it never carries data-plane traffic.
"""

import logging

from typing import Protocol
from dataclasses import field
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.modules.metrics.client import MetricsUnavailable
from app.modules.metrics.services import MetricsService


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricQuery:
    """One number to go and get: a registry metric, about one subject, over one window."""

    metric: str
    subject_type: str
    subject_uid: str
    lookback: str
    dimensions: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Reading:
    """What came back. `value` is None exactly when `withheld` says why."""

    metric: str
    value: float | None = None
    withheld: str | None = None


class MetricReader(Protocol):
    """Answers one `MetricQuery`. The seam a test replaces with a dict."""

    async def read(self, query: MetricQuery) -> Reading: ...


class ServiceMetricReader:
    """Reads through `MetricsService`, once per subject and window per tick.

    The cache is the reason this is a class instead of a function. A tick asks for
    several metrics about the same deployment (TTFT, queue time, output speed)
    and `MetricsService` answers a whole engine's worth of metrics in one round
    trip, so keying on the subject, not on the metric turns eight rules into
    one query.

    Nothing here raises. A cluster with no configured metrics source is the
    ordinary case and not an error, and a reactor tick that dies because a
    Prometheus is down is a tick that also failed to notify anyone about the
    cluster that went away.

    TODO: a wall-clock budget for the whole pass, so a slow store cannot starve the
    event and notification passes that follow it.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.metrics = MetricsService(db=db)
        self._cache: dict[tuple[str, str, str], tuple[dict[str, float | None], str]] = {}

    async def read(self, query: MetricQuery) -> Reading:
        if query.dimensions:
            return Reading(query.metric, withheld=f"no dimensioned read for {sorted(query.dimensions)}")
        samples, withheld = await self._samples(query)
        if withheld:
            return Reading(query.metric, withheld=withheld)
        if query.metric not in samples:
            return Reading(query.metric, withheld=f"{query.metric} is not answered for a {query.subject_type}")
        value = samples[query.metric]
        return Reading(query.metric, value=value, withheld=None if value is not None else "no series")

    async def _samples(self, query: MetricQuery) -> tuple[dict[str, float | None], str]:
        key = (query.subject_type, query.subject_uid, query.lookback)
        if key not in self._cache:
            self._cache[key] = await self._fetch(query)
        return self._cache[key]

    async def _fetch(self, query: MetricQuery) -> tuple[dict[str, float | None], str]:
        read = {"deployment": self.metrics.deployment, "node": self.metrics.node}.get(query.subject_type)
        if read is None:
            return {}, f"no metric read for a {query.subject_type} subject"
        try:
            answer = await read(query.subject_uid, window=query.lookback)
        except (NotFound, MetricsUnavailable) as error:
            logger.debug("reactor.measures.unavailable subject=%s error=%s", query.subject_uid, error.message)
            return {}, error.message
        if answer.unavailable:
            return {}, answer.unavailable
        return {sample.metric: sample.value for sample in answer.samples}, ""
