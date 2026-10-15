import inspect

import pytest

from app.core.ids import POOL
from app.core.ids import new_uid
from app.core.exceptions import Invalid
from app.modules.pools.routes import PoolRoutes
from app.modules.pools.schemas import PoolSchema
from app.modules.pools.schemas import PoolCreateRequest
from app.modules.pools.schemas import PoolUpdateRequest
from app.modules.pools.schemas import PoolMembershipSchema
from app.modules.pools.services import EMPTY
from app.modules.pools.services import within_bounds
from app.modules.pools.repository import PoolRepository


@pytest.fixture
def repo():
    return PoolRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_create_request_defaults_are_a_usable_pool():
    data = PoolCreateRequest(cluster="iad-1", name="h100-a", device_class="h100")
    assert data.min_nodes == 0
    assert data.max_nodes == 0
    assert data.enabled is True
    assert data.taints == []


def test_update_request_leaves_absent_fields_unset():
    data = PoolUpdateRequest(max_nodes=8)
    fields = data.to_dict(exclude_unset=True)
    assert fields == {"max_nodes": 8}


def test_pool_cannot_be_moved_between_clusters_by_patch():
    assert "cluster" not in PoolUpdateRequest.__struct_fields__


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(
            limit=10,
            after_seq=41,
            cluster="iad-1",
            device_class="h100",
            enabled=True,
            search="spot",
            labels={"tier": "gold"},
        )
    )
    assert "FROM supply_pool JOIN supply_cluster" in text
    assert "supply_pool.seq > " in text
    assert "supply_cluster.name = " in text
    assert "supply_pool.device_class = " in text
    assert "supply_pool.enabled IS true" in text
    assert "lower(supply_pool.name) LIKE lower(" in text
    assert "supply_pool.labels ->> " in text
    assert "ORDER BY supply_pool.seq ASC" in text


def test_list_query_resolves_a_cluster_id_against_uid_not_name(repo):
    text = sql(repo.list_query(limit=10, cluster=new_uid(POOL).replace("pol_", "clu_")))
    assert "supply_cluster.uid = " in text


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    compiled = repo.list_query(limit=10).compile()
    assert compiled.params["param_1"] == 11


def test_get_query_resolves_a_name_or_an_id(repo):
    assert "supply_pool.name = " in sql(repo.get_query("h100-a"))
    assert "supply_pool.uid = " in sql(repo.get_query(new_uid(POOL)))


def test_membership_is_counted_in_the_database_not_by_loading_nodes(repo):
    nodes = sql(repo.node_rollup_query([1, 2]))
    devices = sql(repo.device_rollup_query([1, 2]))
    assert nodes.startswith("SELECT supply_node.pool_id, count(supply_node.id)")
    assert "GROUP BY supply_node.pool_id" in nodes
    assert "count(supply_device.id)" in devices
    assert "JOIN supply_node ON supply_device.node_id = supply_node.id" in devices
    assert "GROUP BY supply_node.pool_id" in devices


def test_device_rollup_counts_health_and_allocatable(repo):
    text = sql(repo.device_rollup_query([1]))
    assert "supply_device.health = " in text
    assert "supply_device.allocatable IS true" in text


@pytest.mark.parametrize(
    ("nodes", "low", "high", "expected"),
    [
        (4, 2, 8, True),
        (2, 2, 8, True),
        (8, 2, 8, True),
        (1, 2, 8, False),
        (9, 2, 8, False),
        (0, 0, 0, True),
        (300, 0, 0, True),
        (0, 1, 0, False),
    ],
)
def test_bounds_comparison(nodes, low, high, expected):
    assert within_bounds(nodes, low, high) is expected


def test_zero_max_nodes_is_unbounded_not_a_ceiling_of_zero():
    assert within_bounds(50, 0, 0) is True


def test_empty_membership_is_all_zero():
    assert EMPTY == PoolMembershipSchema(
        nodes_total=0,
        nodes_ready=0,
        nodes_cordoned=0,
        devices_total=0,
        devices_healthy=0,
        devices_allocatable=0,
    )


def test_declared_and_observed_are_returned_side_by_side_not_merged():
    fields = set(PoolSchema.__struct_fields__)
    assert {"min_nodes", "max_nodes", "enabled", "device_class"} <= fields
    assert "membership" in fields
    assert "within_declared_bounds" in fields
    assert "nodes_total" not in fields


def test_controller_exposes_full_crud():
    handlers = {h.handler_name for h in PoolRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {"list_pools", "create_pool", "get_pool", "update_pool", "delete_pool"}


def test_class_filter_is_exposed_as_class_not_device_class():
    parameter = inspect.signature(PoolRoutes.list_pools.fn).parameters["device_class"]
    assert parameter.default.query == "class"


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.deleted = []

    async def execute(self, query):
        return _Result(self.rows)

    async def delete(self, row):
        self.deleted.append(row)

    async def flush(self):
        return None

    async def commit(self):
        return None


class _Row(tuple):
    cluster_uid = "clu_a"


async def test_delete_refuses_while_membership_is_non_empty():
    from types import SimpleNamespace

    from app.core.exceptions import Conflict
    from app.modules.pools.services import PoolService

    pool = SimpleNamespace(id="p1")
    db = _Db([_Row((pool,))])
    service = PoolService(db)

    async def membership(ids):
        return {"p1": {"nodes_total": 3}}

    service.repo.membership = membership
    with pytest.raises(Conflict):
        await service.delete("h100-a")
    assert db.deleted == []


async def test_a_pool_name_two_clusters_share_is_refused_rather_than_picked():
    from types import SimpleNamespace

    repo = PoolRepository(_Db([_Row((SimpleNamespace(id="p1"),)), _Row((SimpleNamespace(id="p2"),))]))
    with pytest.raises(Invalid, match="name a cluster"):
        await repo.get("h100-a")


def test_bad_label_selector_is_rejected_before_it_reaches_sql():
    from app.modules.clusters.routes import parse_labels

    with pytest.raises(Invalid):
        parse_labels(["tier"])
