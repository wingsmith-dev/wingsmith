"""Workloads a member cluster declares over the wire (ADR 0042): scoping, manager, origin, delete."""

import asyncio

import pytest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Policy
from app.db.models import Endpoint
from app.db.models import Workload
from app.hub.messages import ManifestApply
from app.hub.messages import ManifestDelete
from app.hub.manifests import ORIGIN_UID
from app.hub.manifests import ORIGIN_CLUSTER
from app.hub.manifests import ManifestHandler
from app.modules.manifest.schemas import API_VERSION
from app.modules.clusters.services import ClusterProjectService
from app.modules.manifest.services import LAST_APPLIED
from app.modules.manifest.services import ManifestService
from app.modules.workloads.services import constraint_name

from tests.test_modules_db import a_cluster
from tests.test_modules_db import an_environment
from tests.test_modules_db import seed_free_devices


pytestmark = pytest.mark.database


def _body(artifact: str, clusters: list[str], **spec) -> dict:
    return {
        "apiVersion": API_VERSION,
        "kind": "Workload",
        "metadata": {"name": "chat", "labels": {"team": "a"}},
        "spec": {
            "runtime": {"engine": "vllm", "version": "0.6.0"},
            "serving": {"artifact": artifact, "contextLength": 8192, "shape": {"deviceClass": "h100"}},
            "placement": {"allowedClusters": clusters},
            **spec,
        },
    }


def _apply(body: dict, namespace: str = "team-a", generation: int = 1) -> ManifestApply:
    return ManifestApply(
        kind="Workload",
        project="search",
        name="chat",
        namespace=namespace,
        uid="cr-1",
        body=body,
        generation=generation,
    )


async def _setup(db: AsyncSession, link: bool = True, namespaces: tuple[str, ...] = ("team-a", "team-b")):
    cluster, project, artifact = await an_environment(db)
    await seed_free_devices(db, cluster.id)
    if link:
        await ClusterProjectService(db).link(cluster.name, project.slug, list(namespaces))
    return cluster, artifact, ManifestHandler(async_sessionmaker(db.bind, expire_on_commit=False))


async def test_an_unlinked_project_is_refused(db: AsyncSession) -> None:
    cluster, artifact, handler = await _setup(db, link=False)
    result = await handler.handle(cluster.id, _apply(_body(artifact.handle, [cluster.name])))
    assert result.result == "forbidden"
    assert await db.scalar(select(Workload.id).where(Workload.name == "chat")) is None


async def test_only_allowlisted_namespaces_may_declare(db: AsyncSession) -> None:
    cluster, artifact, handler = await _setup(db, namespaces=("team-a",))
    body = _body(artifact.handle, [cluster.name])
    assert (await handler.handle(cluster.id, _apply(body))).result == "created"
    refused = await handler.handle(cluster.id, _apply(body, namespace="team-z"))
    assert (refused.result, refused.reason) == ("forbidden", "NamespaceNotAllowed")
    listed = await ClusterProjectService(db).list(cluster.name)
    assert [(p.project, p.namespaces) for p in listed] == [("search", ["team-a"])]


async def test_an_empty_namespace_list_allows_none(db: AsyncSession) -> None:
    cluster, artifact, handler = await _setup(db, namespaces=())
    refused = await handler.handle(cluster.id, _apply(_body(artifact.handle, [cluster.name])))
    assert (refused.result, refused.reason) == ("forbidden", "NamespaceNotAllowed")
    assert await db.scalar(select(Workload.id).where(Workload.name == "chat")) is None


async def test_apply_records_its_own_manager_and_origin_and_replays_unchanged(db: AsyncSession) -> None:
    cluster, artifact, handler = await _setup(db)
    body = _body(artifact.handle, [cluster.id])
    first = await handler.handle(cluster.id, _apply(body))
    assert first.result == "created", first.error
    assert first.manager == f"k8s:{cluster.name}/team-a"
    assert first.allowed_clusters == [cluster.name] and first.generation == 1
    again = await handler.handle(cluster.id, _apply(body))
    assert again.result == "unchanged" and again.rv == first.rv

    m = await ManifestService(db).workload("search", "chat")
    assert m.metadata.annotations["plane.wingsmith.dev/applied-by"] == first.manager
    assert ORIGIN_CLUSTER in m.metadata.annotations[LAST_APPLIED]
    assert ORIGIN_UID in m.metadata.annotations[LAST_APPLIED]


async def test_another_namespace_is_another_manager_and_conflicts(db: AsyncSession) -> None:
    cluster, artifact, handler = await _setup(db)
    assert (await handler.handle(cluster.id, _apply(_body(artifact.handle, [cluster.name])))).result == "created"
    other = await handler.handle(cluster.id, _apply(_body(artifact.handle, [cluster.name]), namespace="team-b"))
    assert other.result == "conflict" and "k8s:" in other.error


async def test_allowed_clusters_is_mutable(db: AsyncSession) -> None:
    cluster, artifact, handler = await _setup(db)
    second = await a_cluster(db, name="beta")
    await handler.handle(cluster.id, _apply(_body(artifact.handle, [cluster.name])))
    widened = await handler.handle(
        cluster.id, _apply(_body(artifact.handle, [cluster.name, second.name]), generation=2)
    )
    assert widened.result == "configured", widened.error
    assert widened.allowed_clusters == [cluster.name, second.name]
    w = await db.scalar(select(Workload).where(Workload.name == "chat"))
    policy = await db.scalar(select(Policy).where(Policy.name == constraint_name(w.uid)))
    await db.refresh(policy)
    assert len(policy.allowed_cluster_ids) == 2


async def test_delete_removes_its_own_and_releases_anothers(db: AsyncSession) -> None:
    cluster, artifact, handler = await _setup(db)
    await handler.handle(cluster.id, _apply(_body(artifact.handle, [cluster.name])))
    gone = ManifestDelete(kind="Workload", project="search", name="chat", namespace="team-b", uid="cr-2")
    assert (await handler.handle(cluster.id, gone)).result == "released"
    assert await db.scalar(select(Workload.id).where(Workload.name == "chat")) is not None
    mine = ManifestDelete(kind="Workload", project="search", name="chat", namespace="team-a", uid="cr-1")
    assert (await handler.handle(cluster.id, mine)).result == "deleted"
    assert (await handler.handle(cluster.id, mine)).result == "absent"


def _endpoint(namespace: str = "team-a", name: str = "chat-ep") -> ManifestApply:
    body = {
        "apiVersion": API_VERSION,
        "kind": "Endpoint",
        "metadata": {"name": name},
        "spec": {"slug": f"{name}-slug", "targets": [{"workloadRef": {"name": "chat"}}]},
    }
    return ManifestApply(
        kind="Endpoint", project="search", name=name, namespace=namespace, uid="cr-ep", body=body, generation=1
    )


async def test_apply_answers_with_the_plane_status(db: AsyncSession) -> None:
    cluster, artifact, handler = await _setup(db)
    result = await handler.handle(cluster.id, _apply(_body(artifact.handle, [cluster.name])))
    conditions = {c["type"]: c["status"] for c in result.status["conditions"]}
    assert conditions["Ready"] == "False" and "Unschedulable" in conditions
    assert "deployments" in result.status and "scale" in result.status


async def test_an_endpoint_is_declared_scoped_and_deleted_like_a_workload(db: AsyncSession) -> None:
    cluster, _, handler = await _setup(db)
    refused = await handler.handle(cluster.id, _endpoint(namespace="team-z"))
    assert (refused.result, refused.reason) == ("forbidden", "NamespaceNotAllowed")

    created = await handler.handle(cluster.id, _endpoint())
    assert created.result == "created", created.error
    assert created.manager == f"k8s:{cluster.name}/team-a" and created.allowed_clusters is None
    ready = next(c for c in created.status["conditions"] if c["type"] == "Ready")
    assert (ready["status"], ready["reason"]) == ("False", "NoReadyTargets")
    assert (await handler.handle(cluster.id, _endpoint())).result == "unchanged"

    m = await ManifestService(db).endpoint("search", "chat-ep")
    assert m.metadata.annotations["plane.wingsmith.dev/applied-by"] == created.manager
    assert ORIGIN_CLUSTER in m.metadata.annotations[LAST_APPLIED]

    other = ManifestDelete(kind="Endpoint", project="search", name="chat-ep", namespace="team-b", uid="x")
    assert (await handler.handle(cluster.id, other)).result == "released"
    mine = ManifestDelete(kind="Endpoint", project="search", name="chat-ep", namespace="team-a", uid="cr-ep")
    assert (await handler.handle(cluster.id, mine)).result == "deleted"
    assert await db.scalar(select(Endpoint.id).where(Endpoint.name == "chat-ep")) is None


async def test_an_endpoint_into_an_unlinked_project_is_refused(db: AsyncSession) -> None:
    cluster, _, handler = await _setup(db, link=False)
    assert (await handler.handle(cluster.id, _endpoint())).result == "forbidden"


async def test_the_feed_names_only_this_clusters_declarations(db: AsyncSession) -> None:
    cluster, artifact, handler = await _setup(db)
    feed = handler.feed(cluster.id, poll=0.05)
    first = asyncio.ensure_future(anext(feed))
    await asyncio.sleep(0.1)  # the feed starts from the head it read
    await handler.handle(cluster.id, _apply(_body(artifact.handle, [cluster.name])))
    changed = await asyncio.wait_for(first, 5)
    assert (changed.kind, changed.project, changed.name, changed.namespace) == ("Workload", "search", "chat", "team-a")

    # Something another manager owns is not this cluster's to hear about.
    svc = ManifestService(db)
    await svc.put_workload("search", "solo", _body(artifact.handle, [cluster.name]) | {"metadata": {"name": "solo"}})
    await handler.handle(cluster.id, _endpoint())
    changed = await asyncio.wait_for(anext(feed), 5)
    assert (changed.kind, changed.name) == ("Endpoint", "chat-ep")
    await feed.aclose()
