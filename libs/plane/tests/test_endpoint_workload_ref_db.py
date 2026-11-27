"""Endpoint targets that name a workload, resolved at read time (ADR 0026)."""

import pytest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import DEPLOYMENT
from app.core.ids import new_uid
from app.db.models import Cluster
from app.db.models import Deployment
from app.core.exceptions import Invalid
from app.modules.projects.schemas import ProjectCreateRequest
from app.modules.endpoints.schemas import WorkloadRefSchema
from app.modules.endpoints.schemas import EndpointCreateRequest
from app.modules.endpoints.schemas import EndpointTargetRequest
from app.modules.manifest.services import ManifestService
from app.modules.projects.services import ProjectService
from app.modules.endpoints.services import EndpointService
from app.modules.workloads.services import WorkloadService

from tests.test_modules_db import a_cluster
from tests.test_modules_db import a_project
from tests.test_modules_db import a_workload


pytestmark = pytest.mark.database


def by_ref(name: str, weight: int = 100) -> EndpointTargetRequest:
    return EndpointTargetRequest(workload_ref=WorkloadRefSchema(name=name), weight=weight)


def ref_condition(endpoint):
    (condition,) = [c for c in endpoint.conditions if c.type == "ResolvedRefs"]
    return condition


async def test_a_workload_ref_routes_to_the_workloads_deployments(db: AsyncSession) -> None:
    cluster, project, _, workload = await a_workload(db)
    svc = EndpointService(db)
    e = await svc.create(EndpointCreateRequest(name="chat", project=project.id, targets=[by_ref(workload.name)]))
    assert e.targets[0].workload_ref.name == workload.name and e.targets[0].deployment_id is None
    assert ref_condition(e).status == "True"
    (upstream,) = (await svc.resolved(e.id)).endpoints[0].upstreams
    assert upstream.deployment_id == workload.deployments[0].id
    assert upstream.cluster_id == cluster.id
    assert upstream.weight == 100

    manifest = await ManifestService(db).endpoint(project.slug, "chat")
    assert manifest.spec.targets[0].workload_ref.name == workload.name
    assert [t.workload for t in manifest.status.targets] == [workload.name]
    assert manifest.status.conditions[0].status == "True"


async def test_a_missing_workload_is_a_condition_and_routes_once_it_exists(db: AsyncSession) -> None:
    cluster, project, artifact, first = await a_workload(db)
    svc = EndpointService(db)
    e = await svc.create(
        EndpointCreateRequest(name="chat", project=project.id, slug="chat-model", targets=[by_ref("later")])
    )
    condition = ref_condition(e)
    assert (condition.status, condition.reason) == ("False", "WorkloadNotFound")
    assert "later" in condition.message
    before = await svc.resolved(e.id)
    assert before.endpoints[0].upstreams == []
    assert (await ManifestService(db).endpoint(project.slug, "chat")).status.conditions[0].status == "False"

    from app.modules.workloads.schemas import WorkloadCreateRequest

    from tests.test_modules_db import a_serving

    later = await WorkloadService(db).create(
        WorkloadCreateRequest(cluster=cluster.id, serving=a_serving(artifact.id), name="later", project=project.id)
    )
    assert ref_condition(await svc.get(e.id)).status == "True"
    after = await svc.resolved(e.id)
    assert [u.deployment_id for u in after.endpoints[0].upstreams] == [d.id for d in later.deployments]
    assert after.revision != before.revision

    await WorkloadService(db).delete(later.id)
    kept = await svc.get(e.id)
    assert kept.targets[0].workload_ref.name == "later", "deleting the workload leaves the ref"
    assert ref_condition(kept).status == "False"
    assert (await svc.resolved(e.id)).endpoints[0].upstreams == []


async def test_a_workload_ref_never_resolves_in_another_project(db: AsyncSession) -> None:
    _, _, _, workload = await a_workload(db)
    await ProjectService(db).create(ProjectCreateRequest(tenant="acme", slug="other", name="Other"))
    svc = EndpointService(db)
    e = await svc.create(EndpointCreateRequest(name="chat", project="other", slug="x", targets=[by_ref(workload.name)]))
    assert ref_condition(e).status == "False"
    assert (await svc.resolved(e.id)).endpoints[0].upstreams == []


async def test_a_workload_refs_weight_is_split_over_its_deployments(db: AsyncSession) -> None:
    cluster, project, _, workload = await a_workload(db)
    second = await a_cluster(db, name="beta")
    original = (await db.execute(select(Deployment).where(Deployment.uid == workload.deployments[0].id))).scalar_one()
    db.add(
        Deployment(
            uid=new_uid(DEPLOYMENT),
            workload_id=original.workload_id,
            cluster_id=(await db.execute(select(Cluster.id).where(Cluster.uid == second.id))).scalar_one(),
            name=original.name,
            object_name=original.object_name,
        )
    )
    await db.commit()
    svc = EndpointService(db)
    e = await svc.create(
        EndpointCreateRequest(name="chat", project=project.id, slug="m", targets=[by_ref(workload.name, weight=101)])
    )
    weights = sorted(u.weight for u in (await svc.resolved(e.id)).endpoints[0].upstreams)
    assert weights == [50, 51]


async def test_a_target_names_exactly_one_thing(db: AsyncSession) -> None:
    project = await a_project(db)
    svc = EndpointService(db)
    with pytest.raises(Invalid, match="exactly one"):
        await svc.create(EndpointCreateRequest(name="chat", project=project.id, targets=[EndpointTargetRequest()]))
    with pytest.raises(Invalid, match="twice"):
        await svc.create(EndpointCreateRequest(name="chat", project=project.id, targets=[by_ref("a"), by_ref("a")]))


async def test_a_commitment_on_a_workload_ref_endpoint_reports_and_filters_by_workload(db: AsyncSession) -> None:
    from app.modules.quotas.schemas import ServingQuotaCreateRequest
    from app.modules.quotas.services import ServingQuotaService

    _, project, _, workload = await a_workload(db)
    e = await EndpointService(db).create(
        EndpointCreateRequest(name="chat", project=project.id, slug="m", targets=[by_ref(workload.name)])
    )
    quotas = ServingQuotaService(db)
    await quotas.create(ServingQuotaCreateRequest(endpoint=e.id))
    (report,) = (await quotas.resolved()).quotas
    assert (report.endpoint, report.workloads) == (e.id, [workload.id])
    (filtered,) = (await quotas.resolved(workload=workload.name)).quotas
    assert filtered.endpoint == e.id
    assert (await quotas.resolved(workload="nobody")).quotas == []


async def test_a_benchmark_of_a_workload_ref_endpoint_covers_its_deployments(db: AsyncSession) -> None:
    from app.modules.benchmarks.repository import BenchmarkRepository

    _, project, _, workload = await a_workload(db)
    e = await EndpointService(db).create(
        EndpointCreateRequest(name="chat", project=project.id, slug="m", targets=[by_ref(workload.name)])
    )
    rows = await BenchmarkRepository(db).endpoint_targets(e.id)
    assert [(row[0].uid, row[1].uid, row[2].uid) for row in rows] == [(e.id, workload.deployments[0].id, workload.id)]
