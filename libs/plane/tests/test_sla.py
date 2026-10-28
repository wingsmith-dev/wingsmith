"""The SLA report: the period, the verdict, and the service against a fake metrics store.

The period and the verdict are pure, and the service is driven through a stub
backend and a scripted session, so none of this needs a Prometheus or a
database. `test_sla_db.py` covers the tenant boundary against real rows.
"""

import datetime as dt

from types import SimpleNamespace
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.auth import AuthManager
from app.db.models import Endpoint
from app.db.models import ServingQuota
from app.core.context import authorize
from app.core.exceptions import Invalid
from app.core.exceptions import NotFound
from app.core.exceptions import Forbidden
from app.modules.metrics import sources
from app.modules.metrics.sla import Period
from app.modules.metrics.sla import PeriodQuery
from app.modules.metrics.sla import verdict
from app.modules.metrics.sla import request_count
from app.modules.metrics.sla import resolve_period
from app.modules.metrics.client import MetricsUnavailable
from app.modules.metrics.services import TENANT_UNAVAILABLE
from app.modules.metrics.services import MetricsService


NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)
ENDPOINT = "end_01j9x2aaaaaaaaaaaaaaaaaaaa"
SHARED = "default=https://central:9090|token=secret"


def utc(*parts: int) -> dt.datetime:
    return dt.datetime(*parts, tzinfo=dt.UTC)


# --- the period ---------------------------------------------------------------


def test_no_period_is_the_current_calendar_month_measured_up_to_now():
    period = resolve_period(PeriodQuery(), NOW)
    assert period.start == utc(2026, 10, 1)
    assert period.end == NOW
    assert period.partial is True


def test_a_month_that_has_ended_is_measured_whole():
    period = resolve_period(PeriodQuery(month="2026-09"), NOW)
    assert (period.start, period.end, period.partial) == (utc(2026, 9, 1), utc(2026, 10, 1), False)
    assert period.lookback == f"{30 * 86400}s"


def test_february_ends_where_the_calendar_says():
    period = resolve_period(PeriodQuery(month="2024-02"), NOW)
    assert period.end == utc(2024, 3, 1)


def test_a_range_is_taken_as_given_and_a_missing_end_is_now():
    period = resolve_period(PeriodQuery(start=utc(2026, 9, 10), end=utc(2026, 9, 20)), NOW)
    assert (period.start, period.end, period.partial) == (utc(2026, 9, 10), utc(2026, 9, 20), False)
    assert resolve_period(PeriodQuery(start=utc(2026, 10, 5)), NOW).end == NOW


def test_a_naive_timestamp_is_read_as_utc():
    naive = utc(2026, 9, 10).replace(tzinfo=None)
    period = resolve_period(PeriodQuery(start=naive, end=naive + dt.timedelta(days=1)), NOW)
    assert period.start == utc(2026, 9, 10)


def test_a_range_running_past_now_is_clamped_and_marked_partial():
    period = resolve_period(PeriodQuery(start=utc(2026, 10, 1), end=utc(2026, 11, 1)), NOW)
    assert period.end == NOW
    assert period.partial is True


@pytest.mark.parametrize(
    "query",
    [
        PeriodQuery(month="2026-09", start=utc(2026, 9, 1)),
        PeriodQuery(end=utc(2026, 9, 1)),
        PeriodQuery(month="September"),
        PeriodQuery(month="2026-13"),
        PeriodQuery(month="2026-11"),
        PeriodQuery(start=utc(2026, 9, 20), end=utc(2026, 9, 10)),
        PeriodQuery(start=utc(2026, 1, 1), end=utc(2026, 6, 1)),
        PeriodQuery(start=utc(2026, 9, 1), end=utc(2026, 9, 1, 0, 0, 30)),
    ],
    ids=[
        "month-and-range",
        "to-without-from",
        "not-a-month",
        "no-such-month",
        "future",
        "backwards",
        "too-long",
        "too-short",
    ],
)
def test_a_period_that_cannot_be_reported_on_is_refused(query):
    with pytest.raises(Invalid):
        resolve_period(query, NOW)


# --- the verdict --------------------------------------------------------------


def test_the_objective_is_a_fraction_and_the_ratio_a_percentage():
    """The one ×100 in the system. 0.05% failed against three nines is half the budget, and met."""
    met, consumed = verdict(0.05, 0.999)
    assert met is True
    assert consumed == pytest.approx(0.5)


def test_spending_more_than_the_budget_is_a_breach():
    met, consumed = verdict(0.25, 0.999)
    assert met is False
    assert consumed == pytest.approx(2.5)


def test_exactly_the_budget_is_met():
    met, consumed = verdict(1.0, 0.99)
    assert met is True
    assert consumed == pytest.approx(1.0)


def test_a_reading_nothing_measured_reaches_no_verdict():
    """A false that means "we could not measure" is worse than no answer."""
    assert verdict(None, 0.999) == (None, None)
    assert verdict(0.1, None) == (None, None)


def test_a_perfect_objective_has_no_budget_to_divide_but_is_still_judged():
    assert verdict(0.0, 1.0) == (True, None)
    assert verdict(0.01, 1.0) == (False, None)


def test_requests_are_the_average_rate_across_the_period():
    period = Period(start=utc(2026, 9, 1), end=utc(2026, 9, 2), partial=False)
    assert request_count(2.5, period) == 216_000
    assert request_count(None, period) is None


# --- the service against a fake store -----------------------------------------


class StubBackend:
    def __init__(self, samples=None, raises=None):
        self._samples = samples or {}
        self._raises = raises
        self.requests = []

    async def samples(self, request):
        self.requests.append(request)
        if self._raises:
            raise self._raises
        return self._samples


class ScriptedSession:
    def __init__(self, *results):
        self._results = list(results)

    async def execute(self, query):
        rows = self._results.pop(0)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: rows[0] if rows else None))


def with_backend(backend):
    return patch("app.modules.metrics.services.for_source", lambda source, timeout: backend)


def with_sources(value=SHARED):
    return patch.object(sources.settings, "METRICS_SOURCES", value)


def endpoint() -> Endpoint:
    return Endpoint(id=1, uid=ENDPOINT, name="kimi-k3")


def quota(**kw) -> ServingQuota:
    fields = {
        "success_rate_objective": Decimal("0.99900"),
        "availability_objective": "availability-2026.1",
        "objective_window_seconds": 600,
        "enforced": True,
    }
    return ServingQuota(**(fields | kw))


SEPTEMBER = Period(start=utc(2026, 9, 1), end=utc(2026, 10, 1), partial=False)

MEASURED = {
    "serving.requests_per_second": 4.0,
    "serving.failure_ratio": 0.02,
    "serving.availability": 99.98,
    "serving.latency_p50": 0.8,
    "serving.latency_p95": 2.1,
    "serving.latency_p99": 4.4,
    "serving.ttft_p50": 0.21,
    "serving.ttft_p95": 0.55,
    "serving.ttft_p99": 1.3,
}


def service() -> MetricsService:
    return MetricsService(db=None)  # type: ignore[arg-type]


async def test_a_period_is_measured_as_one_lookback_taken_at_its_end():
    """Not a lookback from now: September asked in October must not read October's traffic."""
    backend = StubBackend(MEASURED)
    with with_sources(), with_backend(backend):
        await service().sla(endpoint(), quota(), SEPTEMBER)

    window = backend.requests[0].window
    assert window.lookback == f"{30 * 86400}s"
    assert window.at == SEPTEMBER.end.timestamp()
    assert backend.requests[0].scope.match == {"endpoint": (ENDPOINT,)}
    assert backend.requests[0].scope.cluster == "", "the gateway is never narrowed by a cluster"


async def test_the_report_quotes_every_figure_and_the_objective_beside_it():
    with with_sources(), with_backend(StubBackend(MEASURED)):
        report = await service().sla(endpoint(), quota(), SEPTEMBER)

    assert report.endpoint == ENDPOINT
    assert report.endpoint_name == "kimi-k3"
    assert report.requests == 4 * 30 * 86400
    assert report.availability == 99.98
    assert report.failure_ratio == 0.02
    assert (report.latency.p50, report.latency.p95, report.latency.p99) == (0.8, 2.1, 4.4)
    assert (report.ttft.p50, report.ttft.p95, report.ttft.p99) == (0.21, 0.55, 1.3)
    assert report.definition == "availability-2026.1"
    assert "canceled" in report.definition_text
    assert report.objective.success_rate == pytest.approx(0.999)
    assert report.objective.window_seconds == 600
    assert report.met is True
    assert report.error_budget_consumed == pytest.approx(0.2)
    assert report.partial is False
    assert report.unavailable is None


async def test_the_failure_figures_are_read_through_the_objective_the_quota_names():
    backend = StubBackend(MEASURED)
    with with_sources(), with_backend(backend):
        await service().sla(endpoint(), quota(), SEPTEMBER)

    asked = {metric.name for metric in backend.requests[0].metrics}
    assert {"serving.failure_ratio", "serving.availability", "serving.ttft_p99"} <= asked


async def test_an_endpoint_with_no_quota_is_measured_and_judged_against_nothing():
    with with_sources(), with_backend(StubBackend(MEASURED)):
        report = await service().sla(endpoint(), None, SEPTEMBER)

    assert report.availability == 99.98
    assert report.objective is None
    assert report.met is None
    assert report.error_budget_consumed is None
    assert report.definition == "availability-2026.1", "every figure is under some definition, and it says which"


async def test_a_quota_with_no_success_objective_reaches_no_verdict():
    with with_sources(), with_backend(StubBackend(MEASURED)):
        report = await service().sla(endpoint(), quota(success_rate_objective=None), SEPTEMBER)

    assert report.objective.success_rate is None
    assert report.met is None


async def test_a_period_nothing_called_reaches_no_verdict_and_invents_no_availability():
    with with_sources(), with_backend(StubBackend({})):
        report = await service().sla(endpoint(), quota(), SEPTEMBER)

    assert report.availability is None
    assert report.requests is None
    assert report.met is None


async def test_no_gateway_store_says_so_rather_than_reporting_full_availability():
    backend = StubBackend(MEASURED)
    with with_sources(""), with_backend(backend):
        report = await service().sla(endpoint(), quota(), SEPTEMBER)

    assert backend.requests == []
    assert report.availability is None
    assert report.met is None
    assert "WINGSMITH_METRICS_SOURCES" in report.unavailable


async def test_a_store_that_is_down_is_a_reason_and_not_an_error():
    with with_sources(), with_backend(StubBackend(raises=MetricsUnavailable("cannot reach the metrics store"))):
        report = await service().sla(endpoint(), quota(), SEPTEMBER)

    assert report.unavailable == "cannot reach the metrics store"
    assert report.met is None


async def test_a_quota_naming_a_definition_this_plane_does_not_hold_is_not_measured_under_another():
    backend = StubBackend(MEASURED)
    with with_sources(), with_backend(backend):
        report = await service().sla(endpoint(), quota(availability_objective="availability-2031.1"), SEPTEMBER)

    assert backend.requests == []
    assert report.definition == "availability-2031.1"
    assert report.availability is None
    assert "availability-2031.1" in report.unavailable


@pytest.mark.parametrize(
    ("sources_value", "raises"),
    [
        (SHARED, MetricsUnavailable("cannot reach the metrics store for partner-inference-usw2: connection refused")),
        ("", None),
    ],
    ids=["store-down", "no-gateway-store"],
)
async def test_a_tenant_reads_one_generic_line_where_the_operator_reads_the_reason(sources_value, raises):
    """The operator's reason names the cluster, the store's error and the setting; a tenant gets none of it."""
    backend = StubBackend(MEASURED, raises=raises)
    with with_sources(sources_value), with_backend(backend):
        tenant = await MetricsService(db=ScriptedSession([endpoint()], [quota()])).tenant_sla(  # type: ignore[arg-type]
            "kimi-k3", PeriodQuery(month="2026-09"), now=NOW
        )
        operator = await MetricsService(db=ScriptedSession([endpoint()], [quota()])).endpoint_sla(  # type: ignore[arg-type]
            "kimi-k3", PeriodQuery(month="2026-09"), now=NOW
        )

    assert tenant.unavailable == TENANT_UNAVAILABLE
    assert tenant.met is None
    assert operator.unavailable != TENANT_UNAVAILABLE
    for detail in ("partner-inference-usw2", "connection refused", "WINGSMITH_METRICS_SOURCES"):
        assert detail not in tenant.unavailable


async def test_a_tenant_reading_a_measured_report_gets_it_whole():
    with with_sources(), with_backend(StubBackend(MEASURED)):
        report = await MetricsService(db=ScriptedSession([endpoint()], [quota()])).tenant_sla(  # type: ignore[arg-type]
            "kimi-k3", PeriodQuery(month="2026-09"), now=NOW
        )

    assert report.unavailable is None
    assert report.met is True


async def test_the_endpoint_and_its_quota_are_read_before_the_store():
    svc = MetricsService(db=ScriptedSession([endpoint()], [quota()]))  # type: ignore[arg-type]
    with with_sources(), with_backend(StubBackend(MEASURED)):
        report = await svc.endpoint_sla("kimi-k3", PeriodQuery(month="2026-09"), now=NOW)

    assert report.period_start == SEPTEMBER.start
    assert report.met is True


async def test_an_unknown_endpoint_is_a_404():
    svc = MetricsService(db=ScriptedSession([]))  # type: ignore[arg-type]
    with with_sources(), with_backend(StubBackend()), pytest.raises(NotFound):
        await svc.endpoint_sla("nope", PeriodQuery(), now=NOW)


async def test_a_malformed_period_is_refused_before_anything_is_read():
    svc = MetricsService(db=ScriptedSession())  # type: ignore[arg-type]
    with pytest.raises(Invalid):
        await svc.endpoint_sla("kimi-k3", PeriodQuery(month="2026-13"), now=NOW)


# --- who may read it ----------------------------------------------------------


def test_a_tenant_reads_its_report_on_its_own_surface_and_nowhere_else():
    tenant = AuthManager.tenant_principal(tenant_uid="ten_01m1yd", slug="globex")
    authorize(tenant, "GET", "/v1/serving/models/{model}/sla")
    for route in ("/v1/metrics/endpoints/{ref}/sla", "/v1/tenants/{tenant}/serving/models/{model}/sla"):
        with pytest.raises(Forbidden):
            authorize(tenant, "GET", route)
