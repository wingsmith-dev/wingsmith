"""The tenant serving view against a real database, because the isolation is a join.

`test_serving_tenant.py` proves what a tenant principal cannot reach. This proves
the other half — that inside the one surface it can reach, it sees its own rows
and no others — and that is a property of a query, which no fake session can show.
"""

import os
import subprocess

from pathlib import Path

import pytest
import psycopg

from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth import AuthManager
from app.core.ids import TENANT
from app.core.ids import PROJECT
from app.core.ids import ENDPOINT
from app.core.ids import TENANT_TOKEN
from app.core.ids import new_uid
from app.db.models import Tenant
from app.db.models import Project
from app.db.models import Endpoint
from app.db.models import TenantToken
from app.auth.service import AuthService
from app.core.exceptions import NotFound
from app.core.exceptions import NotAuthorized
from app.modules.serving.services import ServingService


pytestmark = pytest.mark.database

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get("HANGAR_TEST_DATABASE_URL", "postgresql+psycopg://hangar:hangar@localhost:5442/hangar")
TEST_DATABASE = "hangar_serving_test"


@pytest.fixture(scope="session")
def database() -> str:
    admin = make_url(ADMIN_URL)
    dsn = admin.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        connection = psycopg.connect(dsn, connect_timeout=3, autocommit=True)
    except psycopg.Error as exc:
        pytest.skip(f"no database at {admin.host}:{admin.port} — run `just dev-up` ({exc})")

    with connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)')
        connection.execute(f'CREATE DATABASE "{TEST_DATABASE}"')

    url = admin.set(database=TEST_DATABASE).render_as_string(hide_password=False)
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env={**os.environ, "HANGAR_DATABASE_URL": url},
    )
    return url


@pytest.fixture
async def db(database: str):
    engine = create_async_engine(database, poolclass=NullPool)
    async with engine.begin() as connection:
        tables = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'")
        )
        names = ", ".join(f'"{row[0]}"' for row in tables)
        if names:
            await connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            yield session
    finally:
        await engine.dispose()


async def served(db: AsyncSession, slug: str, model: str, enabled: bool = True) -> Tenant:
    """One tenant with one project and one endpoint in it."""
    tenant = Tenant(uid=new_uid(TENANT), slug=slug, name=slug.title())
    db.add(tenant)
    await db.flush()
    project = Project(uid=new_uid(PROJECT), tenant_id=tenant.id, slug=f"{slug}-inference", name="Inference")
    db.add(project)
    await db.flush()
    db.add(
        Endpoint(
            uid=new_uid(ENDPOINT),
            project_id=project.id,
            name=model,
            slug=model,
            enabled=enabled,
        )
    )
    await db.commit()
    return tenant


async def test_a_tenant_sees_only_its_own_models(db: AsyncSession) -> None:
    mine = await served(db, "globex", "moonshotai/kimi-k3")
    await served(db, "acme", "openai/gpt-oss-120b")

    overview = await ServingService(db, mine.uid).overview()
    assert [model.model for model in overview.models] == ["moonshotai/kimi-k3"]
    assert overview.tenant == "globex"


async def test_naming_another_tenants_model_is_a_404_and_not_a_403(db: AsyncSession) -> None:
    """A 403 would confirm the name exists."""
    mine = await served(db, "globex", "moonshotai/kimi-k3")
    await served(db, "acme", "openai/gpt-oss-120b")

    with pytest.raises(NotFound):
        await ServingService(db, mine.uid).model("openai/gpt-oss-120b")


async def test_another_tenants_model_cannot_be_measured(db: AsyncSession) -> None:
    """The metrics query is only ever given a uid this tenant was proved to own."""
    mine = await served(db, "globex", "moonshotai/kimi-k3")
    await served(db, "acme", "openai/gpt-oss-120b")

    with pytest.raises(NotFound):
        await ServingService(db, mine.uid).metrics_ref("openai/gpt-oss-120b")


async def test_an_admin_may_name_a_tenant_by_slug_on_every_read(db: AsyncSession) -> None:
    """The only way to look at what a customer sees, and a slug is what a link carries."""
    await served(db, "globex", "moonshotai/kimi-k3")

    svc = ServingService(db, "globex")
    assert [model.model for model in (await svc.overview()).models] == ["moonshotai/kimi-k3"]
    assert (await svc.model("moonshotai/kimi-k3")).model.model == "moonshotai/kimi-k3"
    assert (await svc.metrics_ref("moonshotai/kimi-k3")).startswith("end_")


async def test_a_model_with_no_deployment_behind_it_is_degraded_not_missing(db: AsyncSession) -> None:
    """ "Nowhere to go right now" and "no such model" must not collapse into one answer."""
    tenant = await served(db, "globex", "moonshotai/kimi-k3")
    overview = await ServingService(db, tenant.uid).overview()
    assert overview.models[0].state == "degraded"
    assert overview.models[0].replicas_ready == 0


async def test_a_disabled_tenant_cannot_be_read(db: AsyncSession) -> None:
    tenant = await served(db, "globex", "moonshotai/kimi-k3")
    tenant.enabled = False
    await db.commit()

    with pytest.raises(NotAuthorized):
        await ServingService(db, tenant.uid).overview()


# --- the credential ---------------------------------------------------------


async def mint(db: AsyncSession, tenant: Tenant) -> str:
    plaintext, digest = AuthManager.mint_tenant_token()
    db.add(TenantToken(uid=new_uid(TENANT_TOKEN), tenant_id=tenant.id, token_hash=digest, name="console"))
    await db.commit()
    return plaintext


async def test_a_tenant_token_resolves_to_its_tenant(db: AsyncSession) -> None:
    tenant = await served(db, "globex", "moonshotai/kimi-k3")
    plaintext = await mint(db, tenant)

    resolved = await AuthService(db).resolve_tenant_token(plaintext)
    assert resolved is not None
    assert resolved[0].slug == "globex"


async def test_an_unknown_token_resolves_to_nothing(db: AsyncSession) -> None:
    await served(db, "globex", "moonshotai/kimi-k3")
    assert await AuthService(db).resolve_tenant_token("hgrt1.not-a-real-token") is None


async def test_a_revoked_token_stops_resolving(db: AsyncSession) -> None:
    tenant = await served(db, "globex", "moonshotai/kimi-k3")
    plaintext = await mint(db, tenant)

    service = AuthService(db)
    resolved = await service.resolve_tenant_token(plaintext)
    assert resolved is not None
    resolved[1].revoked_at = resolved[1].created_at
    await db.commit()

    assert await service.resolve_tenant_token(plaintext) is None


async def test_a_disabled_tenants_token_stops_resolving(db: AsyncSession) -> None:
    """Disabling a tenant is the one switch that must end their access without a second act."""
    tenant = await served(db, "globex", "moonshotai/kimi-k3")
    plaintext = await mint(db, tenant)
    tenant.enabled = False
    await db.commit()

    assert await AuthService(db).resolve_tenant_token(plaintext) is None


async def test_a_revoked_token_ends_the_sessions_it_opened(db: AsyncSession) -> None:
    """Revocation that only refuses the next sign-in leaves a live session running for its TTL."""
    tenant = await served(db, "globex", "moonshotai/kimi-k3")
    plaintext = await mint(db, tenant)

    service = AuthService(db)
    resolved = await service.resolve_tenant_token(plaintext)
    assert resolved is not None
    refresh = await service.create_refresh_token(name=tenant.slug, tenant_token_id=resolved[1].id)
    assert await service.rotate_refresh_token(refresh) is not None

    resolved[1].revoked_at = resolved[1].created_at
    await db.commit()
    rotated = await service.create_refresh_token(name=tenant.slug, tenant_token_id=resolved[1].id)
    assert await service.rotate_refresh_token(rotated) is None
