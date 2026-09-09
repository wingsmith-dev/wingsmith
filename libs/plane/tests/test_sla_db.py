"""The SLA report against real rows: the quota join, and the tenant boundary it sits behind."""

import datetime as dt

from decimal import Decimal
from unittest.mock import patch

import pytest

from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.ids import TENANT
from app.core.ids import PROJECT
from app.core.ids import ENDPOINT
from app.core.ids import SERVING_QUOTA
from app.core.ids import new_uid
from app.db.models import Tenant
from app.db.models import Project
from app.db.models import Endpoint
from app.db.models import ServingQuota
from app.core.exceptions import NotFound
from app.modules.metrics import sources
from app.modules.metrics.sla import PeriodQuery
from app.modules.metrics.services import MetricsService
from app.modules.serving.services import ServingService


pytestmark = pytest.mark.database

NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
async def db(database: str):
    engine = create_async_engine(database, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE bind_tenant RESTART IDENTITY CASCADE"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            yield session
    finally:
        await engine.dispose()


class StubBackend:
    def __init__(self, samples):
        self._samples = samples
        self.requests = []

    async def samples(self, request):
        self.requests.append(request)
        return self._samples


async def served(db: AsyncSession, slug: str, model: str, objective: str | None = None) -> tuple[Tenant, Endpoint]:
    """One tenant with one endpoint, and a serving quota on it when an objective is given."""
    tenant = Tenant(uid=new_uid(TENANT), slug=slug, name=slug.title())
    db.add(tenant)
    await db.flush()
    project = Project(uid=new_uid(PROJECT), tenant_id=tenant.id, slug=f"{slug}-inference", name="Inference")
    db.add(project)
    await db.flush()
    endpoint = Endpoint(uid=new_uid(ENDPOINT), project_id=project.id, name=model, slug=model, enabled=True)
    db.add(endpoint)
    await db.flush()
    if objective is not None:
        db.add(
            ServingQuota(
                uid=new_uid(SERVING_QUOTA),
                endpoint_id=endpoint.id,
                success_rate_objective=Decimal(objective),
                enforced=True,
            )
        )
    await db.commit()
    return tenant, endpoint


def measured(failure_ratio: float):
    backend = StubBackend({"serving.failure_ratio": failure_ratio, "serving.availability": 100 - failure_ratio})
    return (
        backend,
        patch("app.modules.metrics.services.for_source", lambda source, timeout: backend),
        patch.object(sources.settings, "METRICS_SOURCES", "default=https://central:9090"),
    )


async def test_the_report_quotes_the_quota_stored_on_the_endpoint(db: AsyncSession) -> None:
    _, endpoint = await served(db, "globex", "moonshotai/kimi-k3", objective="0.99500")
    backend, store, configured = measured(0.6)
    with store, configured:
        report = await MetricsService(db).endpoint_sla(endpoint.uid, PeriodQuery(month="2026-09"), now=NOW)

    assert report.objective.success_rate == pytest.approx(0.995)
    assert report.objective.window_seconds == 600
    assert report.met is False
    assert report.error_budget_consumed == pytest.approx(1.2)
    assert backend.requests[0].scope.match == {"endpoint": (endpoint.uid,)}


async def test_a_tenant_reports_on_its_own_model_by_name(db: AsyncSession) -> None:
    tenant, endpoint = await served(db, "globex", "moonshotai/kimi-k3", objective="0.99900")
    _, store, configured = measured(0.0)
    with store, configured:
        uid = await ServingService(db, tenant.uid).metrics_ref("moonshotai/kimi-k3")
        report = await MetricsService(db).endpoint_sla(uid, PeriodQuery(), now=NOW)

    assert report.endpoint == endpoint.uid
    assert report.met is True
    assert report.partial is True


async def test_another_tenants_model_has_no_report_to_read(db: AsyncSession) -> None:
    """The report is only ever asked for with a uid the tenant was proved to own."""
    mine, _ = await served(db, "globex", "moonshotai/kimi-k3")
    _, theirs = await served(db, "acme", "openai/gpt-oss-120b", objective="0.99900")

    with pytest.raises(NotFound):
        await ServingService(db, mine.uid).metrics_ref("openai/gpt-oss-120b")
    with pytest.raises(NotFound):
        await ServingService(db, mine.uid).metrics_ref(theirs.uid)
