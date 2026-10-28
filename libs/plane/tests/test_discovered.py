"""Adoption must never become a rollout.

The guarantee this module exists to hold is negative, so most of these tests assert
that something does *not* happen: tracking a discovered workload writes a decision
and produces no declared state, which is what keeps a model's weights in VRAM when
an operator adds it to the inventory.
"""

import datetime as dt

from uuid import UUID
from types import SimpleNamespace
from collections import namedtuple

import pytest

from sqlalchemy.dialects import postgresql

from app.db.models import WorkloadAdoption
from app.db.models import DiscoveredWorkload
from app.ingestion import desired
from app.ingestion import statements
from app.hub.messages import MESSAGE_TYPES
from app.hub.messages import WorkloadDiscovered
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.modules.discovered import services
from app.modules.discovered.routes import DiscoveredWorkloadRoutes
from app.modules.discovered.schemas import PromoteRequest
from app.modules.discovered.schemas import DecisionRequest
from app.modules.discovered.schemas import DiscoveredWorkloadSchema
from app.modules.discovered.services import DiscoveredWorkloadService
from app.modules.discovered.repository import DiscoveredWorkloadRepository


CLUSTER = UUID("11111111-1111-1111-1111-111111111111")


def compiled(query) -> str:
    return str(query.compile(dialect=postgresql.dialect()))


def sighting(**overrides) -> WorkloadDiscovered:
    payload = {
        "workload_id": "dsw_01j9x2ffffffffffffffffffff",
        "cluster_id": "clu_01j9x2aaaaaaaaaaaaaaaaaaaa",
        "namespace": "serving",
        "name": "gpt-oss-120b-vllm",
        "kind": "deployment",
        "images": ["ghcr.io/acme/vllm:0.9.1"],
        "replicas_desired": 2,
        "replicas_ready": 2,
        "devices_per_replica": 4,
        "devices_total": 8,
        "node_ids": ["nod_01j9x2bbbbbbbbbbbbbbbbbbbb"],
        "labels": {"app": "gpt-oss-120b-vllm"},
        "spec_fingerprint": "sha256:9f2c1b7d4e6a08",
        "model": "openai/gpt-oss-120b",
        "observed_at": "2026-08-19T14:02:11.400Z",
    }
    payload.update(overrides)
    return WorkloadDiscovered(**payload)


def test_the_wire_type_is_registered() -> None:
    assert MESSAGE_TYPES["workload.discovered"] is WorkloadDiscovered


Row = namedtuple("Row", "DiscoveredWorkload cluster_uid cluster_name WorkloadAdoption project_uid deployment_uid")


def sighted(replicas: int = 3, fingerprint: str = "sha256:abc", deployment_uid: str | None = None) -> Row:
    """A sighting as the repository returns it, read by column name."""
    workload = SimpleNamespace(
        uid="dsw_01j9x2ffffffffffffffffffff",
        cluster_id=CLUSTER,
        name="gpt-oss",
        namespace="serving",
        kind="Deployment",
        model="acme/gpt-oss-120b",
        images=["vllm/vllm-openai:v0.8.0"],
        replicas_desired=replicas,
        replicas_ready=replicas,
        devices_per_replica=1,
        devices_total=replicas,
        node_uids=[],
        labels={"app": "gpt-oss"},
        spec={},
        spec_fingerprint=fingerprint,
        first_seen_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        observed_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        seq=1,
    )
    return Row(workload, "clu_1", "iad-1", None, None, deployment_uid)


class SightingRepo:
    """Answers the reads a decision or a promotion makes, and records every write."""

    def __init__(self, row: Row) -> None:
        self.row = row
        self.decisions: list[dict] = []

    async def get(self, ref, cluster=None):
        return self.row

    async def project_by_ref(self, ref):
        return SimpleNamespace(id=UUID(int=7), uid="prj_1")

    async def decide(self, values: dict) -> None:
        self.decisions.append(values)
        adoption = SimpleNamespace(
            decision=values["decision"],
            note=values.get("note"),
            decided_by=values.get("decided_by"),
            decided_at=values.get("decided_at"),
        )
        self.row = self.row._replace(WorkloadAdoption=adoption)

    async def artifact_by_ref(self, ref):
        return SimpleNamespace(uid="art_1", name="gpt-oss")

    async def project_row(self, ref):
        return SimpleNamespace(id=UUID(int=7), slug="acme", default_priority_class_id=UUID(int=9))


class WriteLog:
    """A session that refuses to be handed an object, so declaring anything fails the test."""

    def __init__(self) -> None:
        self.commits = 0

    def add(self, obj) -> None:
        raise AssertionError(f"declared {type(obj).__name__} while only deciding")

    async def commit(self) -> None:
        self.commits += 1


def service_over(row: Row) -> tuple[DiscoveredWorkloadService, SightingRepo, WriteLog]:
    db = WriteLog()
    svc = DiscoveredWorkloadService(db=db, principal="ops@example.com")
    svc.repo = SightingRepo(row)
    return svc, svc.repo, db


@pytest.mark.parametrize("decision", ["tracked", "ignored"])
async def test_tracking_a_workload_declares_nothing(decision) -> None:
    """The test the rest of this file depends on. Tracking writes a decision and no declared state.

    Promotion is the only path that creates a deployment, and it is a separate,
    explicitly gated call. Track and ignore construct nothing a reconciler could
    ever be handed.
    """
    svc, repo, db = service_over(sighted())
    result = await svc.decide("gpt-oss", decision, DecisionRequest(project="acme", note="seen"))
    assert [d["decision"] for d in repo.decisions] == [decision]
    assert repo.decisions[0]["project_id"] == UUID(int=7)
    assert db.commits == 1
    assert result.status == decision


def test_the_desired_stream_never_reads_the_decision() -> None:
    """`stream_desired` is the only thing that puts declared state on the wire.

    It joins the sighting to name the object an adoption points at, and it must
    never read the adoption table: a decision to track is not a declaration, so a
    tracked workload that was never promoted has nothing to stream.
    """
    assert not any(value is WorkloadAdoption for value in vars(desired).values())


class AdoptionCapture:
    """Stands in for `WorkloadService`, keeping what a promotion asked it to adopt."""

    adopted: list[dict] = []

    def __init__(self, db) -> None:
        self.db = db

    async def adopt(self, workload, deployment, artifact, **kwargs):
        AdoptionCapture.adopted.append({"workload": workload, "deployment": deployment, **kwargs})
        await kwargs["alongside"]()
        return "adopted"


@pytest.fixture
def promotion(monkeypatch):
    """A promotion with the serving gate stubbed out; that gate is tested with the workload service."""
    AdoptionCapture.adopted = []
    monkeypatch.setattr(services, "WorkloadService", AdoptionCapture)
    monkeypatch.setattr(services, "serving_from_draft", lambda draft, artifact: SimpleNamespace())
    monkeypatch.setattr(services, "check_serving_request", lambda serving, artifact: None)
    monkeypatch.setattr(services, "apply_serving", lambda workload, serving, artifact, roles: None)

    async def no_draft(self, row):
        return None

    monkeypatch.setattr(DiscoveredWorkloadService, "_draft", no_draft)
    return AdoptionCapture.adopted


async def test_a_promotion_adopts_the_scale_it_found(promotion) -> None:
    """Promotion must not decide the replica count.

    Taking it from the request would scale a live workload as a side effect of
    cataloguing it, which is the same class of accident as re-rendering its spec.
    """
    svc, repo, _ = service_over(sighted(replicas=3))
    request = PromoteRequest(artifact="art_1", project="acme", fingerprint="sha256:abc")
    assert await svc.promote("gpt-oss", request) == "adopted"
    (adopted,) = promotion
    workload, deployment = adopted["workload"], adopted["deployment"]
    assert (workload.min_scale, workload.max_scale, workload.desired_scale) == (3, 3, 3)
    assert (deployment.min_replicas, deployment.max_replicas, deployment.desired_replicas) == (3, 3, 3)
    assert workload.pinned_cluster_id == CLUSTER and workload.max_clusters == 1
    assert (adopted["cluster_uid"], adopted["cluster_name"], adopted["project"]) == ("clu_1", "iad-1", "acme")
    assert [d["decision"] for d in repo.decisions] == ["tracked"]


async def test_a_promotion_pins_the_digest_it_was_shown(promotion) -> None:
    svc, _, _ = service_over(sighted(fingerprint="sha256:abc"))
    await svc.promote("gpt-oss", PromoteRequest(artifact="art_1", project="acme", fingerprint="sha256:abc"))
    assert promotion[0]["deployment"].adopted_fingerprint == "sha256:abc"

    svc, repo, _ = service_over(sighted(fingerprint="sha256:new"))
    with pytest.raises(Conflict):
        await svc.promote("gpt-oss", PromoteRequest(artifact="art_1", project="acme", fingerprint="sha256:abc"))
    assert len(promotion) == 1
    assert repo.decisions == []


async def test_a_promoted_sighting_is_not_promoted_twice(promotion) -> None:
    svc, _, _ = service_over(sighted(deployment_uid="dep_01j9x2gggggggggggggggggggg"))
    with pytest.raises(Conflict):
        await svc.promote("gpt-oss", PromoteRequest(artifact="art_1", project="acme", fingerprint="sha256:abc"))
    assert promotion == []


def test_the_promote_request_cannot_ask_for_a_different_spec() -> None:
    """Nothing in the request can change what the workload is running.

    A field here for the image, the overrides or the replica count would make
    promotion capable of causing the restart the whole path exists to avoid.

    `artifact` is the one part of the serving configuration a caller states, because
    the weights are the only part the cluster names by something this catalogue does
    not hold. A `serving` block here would let a caller declare a configuration for a
    process that is already up, which is the restart the path exists to avoid.
    """
    fields = set(PromoteRequest.__struct_fields__)
    assert fields == {"artifact", "project", "fingerprint", "name", "priority_class"}
    assert not fields & {"serving", "engine", "engine_args", "shape", "serving_digest"}


def test_the_sighting_and_the_decision_are_different_tables() -> None:
    """Two writers, two tables.

    Ingestion owns the sighting and the API owns the decision. A shared row would
    let a resync overwrite what an operator chose.
    """
    assert DiscoveredWorkload.__tablename__ != WorkloadAdoption.__tablename__
    sighting_columns = set(DiscoveredWorkload.__table__.columns.keys())
    assert not sighting_columns & {"decision", "decided_by", "decided_at", "note", "project_id"}


def test_a_decision_outlives_the_sighting_it_was_made_about() -> None:
    """Keyed by uid, not by foreign key.

    Pruning is how a snapshot says a workload is gone, and a cluster that
    reconnects mid-snapshot must not cost an operator the five decisions they
    already made.
    """
    foreign_keys = {fk.column.table.name for fk in WorkloadAdoption.__table__.foreign_keys}
    assert "demand_discovered_workload" not in foreign_keys


def test_ingestion_writes_the_sighting_and_freezes_when_we_first_saw_it() -> None:
    statement = statements.upsert_discovered_workload(CLUSTER, sighting())
    rendered = compiled(statement)
    assert "demand_discovered_workload" in rendered
    assert "first_seen_at" in rendered
    update = rendered.split("DO UPDATE SET")[1]
    assert "first_seen_at" not in update
    assert "observed_at" in update


def test_ingestion_refuses_a_sighting_carrying_the_wrong_kind_of_id() -> None:
    with pytest.raises(statements.MalformedObservation):
        statements.upsert_discovered_workload(CLUSTER, sighting(workload_id="dep_01j9x2ffffffffffffffffffff"))


def test_a_prune_is_scoped_to_one_cluster() -> None:
    rendered = compiled(statements.prune_discovered_workloads(CLUSTER, {"dsw_01j9x2ffffffffffffffffffff"}))
    assert "cluster_id" in rendered
    assert rendered.upper().startswith("DELETE")


def test_an_undecided_sighting_reads_as_new() -> None:
    assert services.NEW == "new"
    assert services.STATUSES == {"new", "tracked", "ignored", "dismissed", "promoted"}


def test_promotion_outranks_the_decision_that_led_to_it() -> None:
    decided = type("Adoption", (), {"decision": "tracked"})()
    assert DiscoveredWorkloadService._status(decided, "dep_01j9x2gggggggggggggggggggg") == "promoted"
    assert DiscoveredWorkloadService._status(decided, None) == "tracked"
    assert DiscoveredWorkloadService._status(None, None) == "new"


async def test_listing_rejects_a_status_that_is_not_a_decision() -> None:
    svc = DiscoveredWorkloadService(db=None)
    with pytest.raises(Invalid):
        await svc.list(status="adopted")


def test_the_schema_names_no_serving_configuration_and_no_generation() -> None:
    fields = set(DiscoveredWorkloadSchema.__struct_fields__)
    assert not fields & {"serving", "serving_digest", "generation", "desired_replicas", "enabled", "overrides"}
    assert "status" in fields


def test_the_decision_request_carries_nothing_a_cluster_could_obey() -> None:
    fields = set(DecisionRequest.__struct_fields__)
    assert fields == {"project", "note"}


def test_the_controller_reads_and_decides_and_does_nothing_else() -> None:
    """DELETE is here, and it still writes nothing but a decision.

    It removes an entry from the list instead of the sighting under it, which is
    why it is allowed to exist beside a rule that gives observed state one writer.
    """
    handlers = [h for h in vars(DiscoveredWorkloadRoutes).values() if hasattr(h, "http_methods")]
    assert handlers
    methods = {m for h in handlers for m in h.http_methods}
    assert methods == {"GET", "POST", "DELETE"}


def test_the_draft_is_read_as_a_serving_block_and_writes_nothing() -> None:
    """The reading a promotion declares, offered under the name of what it drafts.

    There is no recipe to draft any more: the fields land on the workload the
    promotion mints, so the path is `/serving-draft` and it is a GET: the same
    sighting derives the same draft every time, and asking does not change it.
    """
    handler = DiscoveredWorkloadRoutes.draft_serving
    assert next(iter(handler.paths)).endswith("/serving-draft")
    assert handler.http_methods == {"GET"}


async def test_the_repository_writes_only_the_decision_table() -> None:
    class Session:
        def __init__(self) -> None:
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)

        async def flush(self) -> None:
            pass

    session = Session()
    repo = DiscoveredWorkloadRepository(session)
    await repo.decide({"cluster_id": CLUSTER, "workload_uid": "dsw_01j9x2ffffffffffffffffffff", "decision": "tracked"})
    await repo.undecide("dsw_01j9x2ffffffffffffffffffff")
    written = {statement.table.name for statement in session.statements}
    assert written == {WorkloadAdoption.__tablename__}
    assert not hasattr(repo, "add")


@pytest.mark.parametrize("name", ["track_discovered", "restore_discovered", "ignore_discovered"])
def test_a_decision_answers_200_because_it_creates_nothing(name) -> None:
    assert getattr(DiscoveredWorkloadRoutes, name).status_code == 200


def test_a_promotion_answers_201_because_it_creates_a_workload() -> None:
    assert DiscoveredWorkloadRoutes.promote_discovered.status_code == 201
