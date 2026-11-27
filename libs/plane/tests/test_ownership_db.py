"""Ownership of applied objects (ADR 0033): who applied it, and what other writers may do."""

import importlib.util

from types import SimpleNamespace

import pytest

from sqlalchemy import text
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import Operation
from app.core.ownership import field_manager
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import InvalidParameter
from app.modules.endpoints.routes import EndpointRoutes
from app.modules.workloads.routes import WorkloadRoutes
from app.modules.endpoints.schemas import EndpointUpdateRequest
from app.modules.manifest.services import APPLIED_AT
from app.modules.manifest.services import APPLIED_BY
from app.modules.manifest.services import LAST_APPLIED
from app.modules.manifest.services import APPLIED_BY_PRINCIPAL
from app.modules.manifest.services import ManifestService
from app.modules.workloads.schemas import WorkloadUpdateRequest
from app.modules.endpoints.services import EndpointService
from app.modules.operations.schemas import OperationCreateRequest
from app.modules.workloads.services import WorkloadService
from app.modules.operations.services import OperationService

from tests.conftest import ROOT
from tests.test_modules_db import a_workload
from tests.test_modules_db import a_deployment
from tests.test_manifest_db import _wire
from tests.test_manifest_db import _endpoint_body


pytestmark = pytest.mark.database


def _context(db: AsyncSession, name: str = "alice"):
    return SimpleNamespace(db=db, principal=SimpleNamespace(name=name))


async def _applied_workload(db: AsyncSession, manager: str = "wings-cli"):
    *_, workload = await a_workload(db)
    svc = ManifestService(db, actor="ci-bot")
    body = _wire(await svc.workload("search", "gpt-oss-prod"))
    m, _ = await svc.put_workload("search", "gpt-oss-prod", body, manager=manager)
    return workload, m


async def _takeovers(db: AsyncSession, kind: str) -> list[Operation]:
    rows = await db.scalars(select(Operation).where(Operation.type == f"{kind}.takeover"))
    return list(rows)


async def test_put_records_the_manager_and_reads_it_back(db: AsyncSession) -> None:
    workload, m = await _applied_workload(db, manager="argo")
    notes = m.metadata.annotations
    assert (notes[APPLIED_BY], notes[APPLIED_BY_PRINCIPAL]) == ("argo", "ci-bot")
    assert notes[APPLIED_AT].endswith("Z")
    w = await WorkloadService(db).get(workload.id)
    assert (w.applied_by, w.applied_by_principal) == ("argo", "ci-bot")
    assert w.applied_at is not None


async def test_a_replayed_put_with_its_own_annotations_is_unchanged(db: AsyncSession) -> None:
    _, m = await _applied_workload(db)
    again, result = await ManifestService(db).put_workload("search", "gpt-oss-prod", _wire(m))
    assert result == "unchanged"
    assert APPLIED_BY not in again.metadata.annotations[LAST_APPLIED]


async def test_patch_on_a_managed_workload_is_a_conflict_naming_the_manager(db: AsyncSession) -> None:
    workload, _ = await _applied_workload(db)
    handler = WorkloadRoutes.update_workload.fn
    svc = WorkloadService(db)
    with pytest.raises(Conflict) as exc:
        await handler(None, _context(db), svc, workload.id, WorkloadUpdateRequest(preemptible=True))
    assert exc.value.detail == {"manager": "wings-cli"}
    assert "wings-cli" in exc.value.message


async def test_forced_patch_takes_the_workload_over_and_is_audited(db: AsyncSession) -> None:
    workload, _ = await _applied_workload(db)
    handler = WorkloadRoutes.update_workload.fn
    response = await handler(
        None,
        _context(db),
        WorkloadService(db),
        workload.id,
        WorkloadUpdateRequest(preemptible=True),
        if_match=None,
        force=True,
    )
    assert response.content.preemptible and response.content.applied_by is None
    cols = (Workload.applied_by, Workload.applied_by_principal, Workload.applied_at, Workload.last_applied)
    row = (await db.execute(select(*cols).where(Workload.uid == workload.id))).one()
    assert tuple(row) == (None, None, None, None)
    (op,) = await _takeovers(db, "workload")
    assert (op.phase, op.requested_by, op.payload["from"]) == ("succeeded", "alice", "wings-cli")

    m = await ManifestService(db).workload("search", "gpt-oss-prod")
    assert not {APPLIED_BY, LAST_APPLIED} & set(m.metadata.annotations)


async def test_patch_on_an_unmanaged_workload_passes(db: AsyncSession) -> None:
    *_, workload = await a_workload(db)
    handler = WorkloadRoutes.update_workload.fn
    response = await handler(
        None, _context(db), WorkloadService(db), workload.id, WorkloadUpdateRequest(preemptible=True), if_match=None
    )
    assert response.content.preemptible
    assert await _takeovers(db, "workload") == []


async def test_endpoint_patch_is_guarded_and_forced(db: AsyncSession) -> None:
    _, _, _, deployment = await a_deployment(db)
    m, _ = await ManifestService(db).put_endpoint(
        "search", "chat", _endpoint_body(slug="chat-model", targets=[{"deployment": deployment.object_name}])
    )
    handler = EndpointRoutes.update_endpoint.fn
    with pytest.raises(Conflict):
        await handler(None, _context(db), EndpointService(db), m.metadata.uid, EndpointUpdateRequest(aliases=["x"]))
    response = await handler(
        None,
        _context(db),
        EndpointService(db),
        m.metadata.uid,
        EndpointUpdateRequest(aliases=["x"]),
        if_match=None,
        project=None,
        force=True,
    )
    assert response.content.aliases == ["x"] and response.content.applied_by is None
    assert len(await _takeovers(db, "endpoint")) == 1


async def test_operations_on_a_managed_workload_are_refused_except_drain(db: AsyncSession) -> None:
    workload, _ = await _applied_workload(db)
    ops = OperationService(db, actor="alice")
    with pytest.raises(Conflict) as exc:
        await ops.create(OperationCreateRequest(type="workload.pause", target=workload.id, parameters={"paused": True}))
    assert exc.value.detail == {"manager": "wings-cli"}

    drain = await ops.create(
        OperationCreateRequest(type="workload.drain", target=workload.id, parameters={"cluster": "nowhere"})
    )
    assert drain.type == "workload.drain"
    assert await db.scalar(select(Workload.applied_by).where(Workload.uid == workload.id)) == "wings-cli"

    paused = await OperationService(db, actor="alice").create(
        OperationCreateRequest(type="workload.pause", target=workload.id, parameters={"paused": True}), force=True
    )
    assert paused.phase == "succeeded"
    assert await db.scalar(select(Workload.applied_by).where(Workload.uid == workload.id)) is None
    assert len(await _takeovers(db, "workload")) == 1


async def test_put_from_another_manager_is_a_conflict_unless_forced(db: AsyncSession) -> None:
    _, m = await _applied_workload(db, manager="wings-cli")
    body = _wire(m)
    body["spec"]["preemptible"] = True
    svc = ManifestService(db, actor="bob")
    with pytest.raises(Conflict) as exc:
        await svc.put_workload("search", "gpt-oss-prod", body, manager="argo")
    assert exc.value.detail == {"manager": "wings-cli"}

    after, result = await svc.put_workload("search", "gpt-oss-prod", body, manager="argo", force=True)
    assert result == "configured" and after.spec.preemptible
    assert (after.metadata.annotations[APPLIED_BY], after.metadata.annotations[APPLIED_BY_PRINCIPAL]) == ("argo", "bob")
    (op,) = await _takeovers(db, "workload")
    assert op.payload == {"from": "wings-cli", "to": "argo"}


async def test_put_endpoint_from_another_manager_is_a_conflict(db: AsyncSession) -> None:
    _, _, _, deployment = await a_deployment(db)
    body = _endpoint_body(slug="chat-model", targets=[{"deployment": deployment.object_name}])
    svc = ManifestService(db)
    await svc.put_endpoint("search", "chat", body)
    with pytest.raises(Conflict):
        await svc.put_endpoint("search", "chat", body, manager="argo")


async def test_put_on_an_observed_workload_is_a_422(db: AsyncSession) -> None:
    from app.db.models import Deployment

    _, _, _, deployment = await a_deployment(db)
    await db.execute(Deployment.__table__.update().where(Deployment.uid == deployment.id).values(authority="observed"))
    await db.commit()
    db.expire_all()
    svc = ManifestService(db)
    body = _wire(await svc.workload("search", "gpt-oss-prod"))
    with pytest.raises(Invalid, match="observed, adopt it first"):
        await svc.put_workload("search", "gpt-oss-prod", body, force=True)


async def test_backfill_marks_applied_rows_as_wings_cli(db: AsyncSession) -> None:
    *_, workload = await a_workload(db)
    await db.execute(
        text("UPDATE demand_workload SET last_applied = '{}'::jsonb, applied_by = NULL WHERE uid = :uid"),
        {"uid": workload.id},
    )
    path = ROOT / "migrations" / "versions" / "1791300000_c4d5e6f7a8b9.py"
    spec = importlib.util.spec_from_file_location("ownership_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for table in module.TABLES:
        await db.execute(text(module.BACKFILL.format(table=table)))
    assert await db.scalar(select(Workload.applied_by).where(Workload.uid == workload.id)) == "wings-cli"
    assert await db.scalar(select(Endpoint.applied_by).where(Endpoint.applied_by.is_not(None))) is None


def test_field_manager_defaults_and_validates() -> None:
    assert field_manager(None) == "wings-cli"
    assert field_manager("argo-cd") == "argo-cd"
    with pytest.raises(InvalidParameter):
        field_manager("bad name!")
    with pytest.raises(InvalidParameter):
        field_manager("x" * 129)


async def test_scale_on_a_managed_workload_is_a_conflict_unless_forced(db: AsyncSession) -> None:
    from tests.test_manifest_db import _scale

    await _applied_workload(db)
    svc = ManifestService(db, actor="alice")
    with pytest.raises(Conflict):
        await svc.put_scale("search", "gpt-oss-prod", _scale(3))
    s = await svc.put_scale("search", "gpt-oss-prod", _scale(3), force=True)
    assert s.spec.replicas == 3
    (op,) = await _takeovers(db, "workload")
    assert (op.requested_by, op.payload["from"]) == ("alice", "wings-cli")
