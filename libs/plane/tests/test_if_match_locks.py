"""A conditional update locks the row it checks If-Match against.

Without the lock, two writers holding the same revision both pass the check and
the second silently overwrites the first. Each service's update is run against a
session that records its statements and finds nothing, so the test reads the first
statement the update issued and stops at the 404.
"""

import pytest

from sqlalchemy.dialects import postgresql

from app.core.exceptions import Invalid
from app.core.exceptions import NotFound
from app.modules.tenants.schemas import TenantUpdateRequest
from app.modules.clusters.schemas import ClusterUpdateRequest
from app.modules.tenants.services import TenantService
from app.modules.artifacts.schemas import ArtifactUpdateRequest
from app.modules.clusters.services import ClusterService
from app.modules.artifacts.services import ArtifactService
from app.modules.components.schemas import ComponentUpdateRequest
from app.modules.components.services import ComponentService
from app.modules.components.repository import ComponentRepository
from app.modules.priority_classes.schemas import PriorityClassUpdateRequest
from app.modules.priority_classes.services import PriorityClassService


class Result:
    def __init__(self, rows):
        self.rows = rows

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class Session:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return Result(self.rows)


def sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


UPDATES = [
    ("tenant", lambda db: TenantService(db).update("acme", TenantUpdateRequest(name="Acme"), if_match="1")),
    ("cluster", lambda db: ClusterService(db).update("iad-1", ClusterUpdateRequest(region="us"), if_match="1")),
    ("artifact", lambda db: ArtifactService(db).update("gpt-oss", ArtifactUpdateRequest(name="x"), if_match="1")),
    (
        "component",
        lambda db: ComponentService(db).update("agent", ComponentUpdateRequest(desired_version="2"), if_match="1"),
    ),
    (
        "priority class",
        lambda db: PriorityClassService(db).update("prod", PriorityClassUpdateRequest(value=5), if_match="1"),
    ),
]


@pytest.mark.parametrize(("kind", "update"), UPDATES, ids=[kind for kind, _ in UPDATES])
async def test_an_if_match_update_reads_its_row_for_update(kind, update):
    db = Session()
    with pytest.raises(NotFound):
        await update(db)
    assert "FOR UPDATE" in sql(db.statements[0])


async def test_a_plain_read_takes_no_lock():
    db = Session()
    with pytest.raises(NotFound):
        await TenantService(db).get("acme")
    assert "FOR UPDATE" not in sql(db.statements[0])


async def test_a_component_name_two_clusters_share_is_refused_rather_than_picked():
    db = Session([("component-a",), ("component-b",)])
    with pytest.raises(Invalid, match="name a cluster"):
        await ComponentRepository(db).get("agent")
