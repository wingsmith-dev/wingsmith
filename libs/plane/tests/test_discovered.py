"""Adoption must never become a rollout.

The guarantee this module exists to hold is negative, so most of these tests assert
that something does *not* happen: tracking a discovered workload writes a decision
and produces no declared state, which is what keeps a model's weights in VRAM when
an operator adds it to the inventory.
"""

import ast
import inspect

from uuid import UUID

import pytest

from sqlalchemy.dialects import postgresql

from app.db.models import WorkloadAdoption
from app.db.models import DiscoveredWorkload
from app.ingestion import desired
from app.ingestion import statements
from app.hub.messages import MESSAGE_TYPES
from app.hub.messages import WorkloadDiscovered
from app.core.exceptions import Invalid
from app.modules.discovered import services
from app.modules.discovered.routes import DiscoveredWorkloadRoutes
from app.modules.discovered.schemas import DecisionRequest
from app.modules.discovered.schemas import DiscoveredWorkloadSchema
from app.modules.discovered.services import DiscoveredWorkloadService
from app.modules.discovered.repository import DiscoveredWorkloadRepository


CLUSTER = UUID("11111111-1111-1111-1111-111111111111")


def code_of(module) -> str:
    """A module's source with its docstrings removed.

    These tests assert that a name does not appear, and a docstring explaining why
    it does not appear would otherwise fail them.
    """
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and (
            ast.get_docstring(node) is not None
        ):
            node.body = node.body[1:]
    return ast.unparse(tree)


def compiled(query) -> str:
    return str(query.compile(dialect=postgresql.dialect()))


def sighting(**overrides) -> WorkloadDiscovered:
    payload = {
        "workload_id": "dsw_01j9x2ffffffffffffffffffff",
        "cluster_id": "clu_01j9x2aaaaaaaaaaaaaaaaaaaa",
        "namespace": "serving",
        "name": "llama-70b-vllm",
        "kind": "deployment",
        "images": ["ghcr.io/acme/vllm:0.9.1"],
        "replicas_desired": 2,
        "replicas_ready": 2,
        "devices_per_replica": 4,
        "devices_total": 8,
        "node_ids": ["nod_01j9x2bbbbbbbbbbbbbbbbbbbb"],
        "labels": {"app": "llama-70b-vllm"},
        "model": "meta-llama/Llama-3.1-70B-Instruct",
        "observed_at": "2026-08-19T14:02:11.400Z",
    }
    payload.update(overrides)
    return WorkloadDiscovered(**payload)


def test_the_wire_type_is_registered() -> None:
    assert MESSAGE_TYPES["workload.discovered"] is WorkloadDiscovered


def test_tracking_a_workload_declares_nothing() -> None:
    """The load-bearing test. Nothing a decision writes can reach a cluster.

    `stream_desired` is the only thing that puts declared state on the wire, and it
    reads neither of the two tables this module touches. A discovered workload
    therefore cannot produce a `deployment.desired`, and a reconciler cannot be
    handed a difference to converge.
    """
    source = code_of(desired)
    assert "DiscoveredWorkload" not in source
    assert "WorkloadAdoption" not in source


def test_the_service_creates_no_deployment() -> None:
    source = code_of(services)
    assert "Deployment" not in source
    assert "generation" not in source
    assert "Recipe" not in source


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
    assert services.STATUSES == {"new", "tracked", "ignored"}


async def test_listing_rejects_a_status_that_is_not_a_decision() -> None:
    svc = DiscoveredWorkloadService(db=None)
    with pytest.raises(Invalid):
        await svc.list(status="adopted")


def test_the_schema_names_no_recipe_and_no_generation() -> None:
    fields = set(DiscoveredWorkloadSchema.__struct_fields__)
    assert not fields & {"recipe_id", "generation", "desired_replicas", "enabled", "overrides"}
    assert "status" in fields


def test_the_decision_request_carries_nothing_a_cluster_could_obey() -> None:
    fields = set(DecisionRequest.__struct_fields__)
    assert fields == {"project", "note"}


def test_the_controller_reads_and_decides_and_does_nothing_else() -> None:
    handlers = [h for h in vars(DiscoveredWorkloadRoutes).values() if hasattr(h, "http_methods")]
    assert handlers
    methods = {m for h in handlers for m in h.http_methods}
    assert methods == {"GET", "POST"}


def test_the_repository_writes_only_the_decision_table() -> None:
    source = inspect.getsource(DiscoveredWorkloadRepository)
    assert "insert(WorkloadAdoption)" in source
    assert "insert(DiscoveredWorkload)" not in source
    assert "delete(" not in source
