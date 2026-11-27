"""Endpoint names are unique per project and slugs globally, against a real Postgres (ADR 0024)."""

import pytest

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.modules.metrics.services import MetricsService
from app.modules.projects.schemas import ProjectCreateRequest
from app.modules.endpoints.schemas import EndpointCreateRequest
from app.modules.endpoints.schemas import EndpointUpdateRequest
from app.modules.manifest.services import ManifestService
from app.modules.projects.services import ProjectService
from app.modules.endpoints.services import EndpointService
from app.modules.endpoints.repository import endpoint_by_ref

from tests.test_modules_db import a_project


pytestmark = pytest.mark.database


async def two_projects(db: AsyncSession):
    project = await a_project(db)
    await ProjectService(db).create(ProjectCreateRequest(tenant="acme", slug="other", name="Other"))
    return project.slug, "other"


async def test_two_projects_may_each_have_an_endpoint_of_one_name(db: AsyncSession) -> None:
    first, second = await two_projects(db)
    svc = EndpointService(db)
    a = await svc.create(EndpointCreateRequest(name="chat", project=first, slug="model-a"))
    b = await svc.create(EndpointCreateRequest(name="chat", project=second, slug="model-b"))
    assert a.id != b.id
    assert (await svc.get("chat", first)).id == a.id
    assert (await svc.get("chat", second)).id == b.id
    assert (await ManifestService(db).endpoint(second, "chat")).metadata.uid == b.id


async def test_one_name_twice_in_one_project_is_a_conflict(db: AsyncSession) -> None:
    first, _ = await two_projects(db)
    svc = EndpointService(db)
    await svc.create(EndpointCreateRequest(name="chat", project=first, slug="model-a"))
    with pytest.raises(Conflict):
        await svc.create(EndpointCreateRequest(name="chat", project=first, slug="model-b"))


async def test_an_ambiguous_bare_name_asks_for_the_project(db: AsyncSession) -> None:
    first, second = await two_projects(db)
    svc = EndpointService(db)
    a = await svc.create(EndpointCreateRequest(name="chat", project=first, slug="model-a"))
    await svc.create(EndpointCreateRequest(name="chat", project=second, slug="model-b"))
    for read in (
        lambda: svc.get("chat"),
        lambda: svc.update("chat", EndpointUpdateRequest(enabled=False)),
        lambda: svc.delete("chat"),
        lambda: svc.resolved("chat"),
        lambda: endpoint_by_ref(db, "chat"),
        lambda: MetricsService(db).endpoint("chat"),
    ):
        with pytest.raises(Invalid, match="project"):
            await read()
    assert (await svc.get(a.id)).name == "chat", "an id is never ambiguous"


async def test_a_unique_bare_name_still_resolves_without_a_project(db: AsyncSession) -> None:
    first, _ = await two_projects(db)
    svc = EndpointService(db)
    a = await svc.create(EndpointCreateRequest(name="chat", project=first, slug="model-a"))
    assert (await svc.get("chat")).id == a.id


async def test_a_slug_another_endpoint_has_is_refused(db: AsyncSession) -> None:
    first, second = await two_projects(db)
    svc = EndpointService(db)
    await svc.create(EndpointCreateRequest(name="chat", project=first, slug="gpt-oss-20b"))
    with pytest.raises(Conflict, match="model id"):
        await svc.create(EndpointCreateRequest(name="chat", project=second, slug="gpt-oss-20b"))
    b = await svc.create(EndpointCreateRequest(name="chat", project=second, slug="model-b"))
    with pytest.raises(Conflict, match="model id"):
        await svc.update(b.id, EndpointUpdateRequest(slug="gpt-oss-20b"))


async def test_a_slug_another_endpoint_holds_as_an_alias_is_refused(db: AsyncSession) -> None:
    first, second = await two_projects(db)
    svc = EndpointService(db)
    await svc.create(EndpointCreateRequest(name="chat", project=first, slug="model-a", aliases=["openai/gpt-oss"]))
    with pytest.raises(Conflict, match="alias"):
        await svc.create(EndpointCreateRequest(name="chat", project=second, slug="openai/gpt-oss"))
