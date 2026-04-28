"""The device catalog: the contract dictionary joined to what the fleet holds.

The merge is the whole of the module's logic and it is pure, so it is tested
without a database. What needs a database is the aggregate behind it, and that
is asserted as a compiled statement here for the same reason every other module
suite does — the shape of the query, not the numbers it returns.
"""

import pytest

from sqlalchemy import text

from app.core.devices import DeviceClass
from app.core.devices import table
from app.core.exceptions import NotFound
from app.modules.device_classes.routes import DeviceClassRoutes
from app.modules.device_classes.schemas import DeviceClassSchema
from app.modules.device_classes.services import DeviceClassService
from app.modules.device_classes.services import schema
from app.modules.device_classes.services import catalog
from app.modules.device_classes.repository import DeviceClassRepository

from tests.conftest import fetch


H100 = DeviceClass(
    name="h100-80gb-sxm",
    label="NVIDIA H100 80GB SXM",
    architecture="hopper",
    vendor="nvidia",
    memory_mib=81559,
    form_factor="sxm",
    aliases={"dcgm": ("NVIDIA H100 80GB HBM3",)},
)
L4 = DeviceClass(
    name="l4-24gb-pcie",
    label="NVIDIA L4 24GB",
    architecture="ada",
    vendor="nvidia",
    memory_mib=23034,
    form_factor="pcie",
    aliases={},
)


@pytest.fixture
def repo():
    return DeviceClassRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_the_catalog_is_read_only_because_the_operator_shares_the_table():
    handlers = {h.handler_name for h in DeviceClassRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {"list_device_classes", "get_device_class"}


def test_route_path_is_hyphenated_even_though_the_package_is_underscored():
    assert DeviceClassRoutes.path == "/device-classes"


def test_a_class_we_own_none_of_still_appears_so_it_can_be_planned_for():
    items = catalog([H100, L4], {})
    assert [i.name for i in items] == ["h100-80gb-sxm", "l4-24gb-pcie"]
    assert all(i.in_catalog for i in items)
    assert all(i.inventory.devices == 0 for i in items)


def test_hardware_the_table_does_not_name_is_reported_rather_than_dropped():
    """The urgent half of the merge: a class in the fleet with no entry."""
    items = catalog([H100], {"quantum-9999": (4, 4, 4, 1, ["iad-1"])})
    unnamed = next(i for i in items if i.name == "quantum-9999")
    assert unnamed.in_catalog is False
    assert unnamed.label == "quantum-9999"
    assert unnamed.vendor == ""
    assert unnamed.inventory.devices == 4


def test_the_classes_we_own_most_of_come_first():
    items = catalog([H100, L4], {"l4-24gb-pcie": (8, 8, 8, 2, ["iad-1"])})
    assert [i.name for i in items] == ["l4-24gb-pcie", "h100-80gb-sxm"]


def test_present_narrows_to_what_the_fleet_actually_holds():
    inventory = {"l4-24gb-pcie": (8, 7, 6, 2, ["iad-1", "sfo-2"])}
    assert [i.name for i in catalog([H100, L4], inventory, present=True)] == ["l4-24gb-pcie"]
    assert [i.name for i in catalog([H100, L4], inventory, present=False)] == ["h100-80gb-sxm"]


def test_architecture_filters_a_vendor_generation():
    assert [i.name for i in catalog([H100, L4], {}, architecture="ada")] == ["l4-24gb-pcie"]
    assert catalog([H100, L4], {}, architecture="turing") == []


def test_inventory_counts_are_carried_apart_rather_than_rolled_into_a_health_ratio():
    item = schema("l4-24gb-pcie", L4, (8, 7, 6, 2, ["sfo-2", "iad-1"]))
    assert (item.inventory.devices, item.inventory.healthy, item.inventory.allocatable) == (8, 7, 6)
    assert item.inventory.nodes == 2
    assert item.inventory.clusters == ["iad-1", "sfo-2"]


def test_a_null_cluster_aggregate_does_not_become_a_null_in_the_schema():
    assert schema("x", None, (0, 0, 0, 0, None)).inventory.clusters == []


def test_label_is_the_only_string_meant_for_a_person():
    item = schema("h100-80gb-sxm", H100, None)
    assert item.name == "h100-80gb-sxm"
    assert item.label == "NVIDIA H100 80GB SXM"


def test_every_shipped_class_carries_a_label_and_an_architecture():
    for item in catalog(table().classes, {}):
        assert item.label and item.label != item.name
        assert item.architecture


def test_inventory_query_groups_by_class_rather_than_listing_devices(repo):
    statement = sql(repo.query())
    assert "GROUP BY supply_device.device_class" in statement
    assert "count(" in statement


def test_inventory_query_scopes_to_one_cluster(repo):
    assert "supply_cluster" in sql(repo.query(cluster="iad-1"))


def test_schema_is_flat_enough_to_render_a_select_from():
    fields = set(DeviceClassSchema.__struct_fields__)
    assert {"name", "label", "architecture", "memory_mib", "in_catalog", "inventory"} <= fields


@pytest.mark.database
class TestInventoryAgainstPostgres:
    """The aggregate, executed.

    `array_agg(distinct ...)` beside `count(case ...)` compiles on any dialect
    and is not portable in the way that suggests — and a grouped query that
    returns the wrong shape is invisible until something reads row[4]. So the
    numbers here come out of a real database.
    """

    async def test_counts_group_by_class_and_name_their_clusters(self, sessions, cluster):
        async with sessions.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO supply_node (uid, cluster_id, name, ready, condition)"
                    " VALUES (:uid, :cluster, 'node-a', true, 'ready') RETURNING id"
                ),
                {"uid": "nod_01j9x2eeeeeeeeeeeeeeeeeeea", "cluster": cluster},
            )

        node = (await fetch(sessions, "SELECT id FROM supply_node WHERE name = 'node-a'"))[0][0]

        rows = [
            ("dev_01j9x2ffffffffffffffffff01", "h100-80gb-sxm", "healthy", True),
            ("dev_01j9x2ffffffffffffffffff02", "h100-80gb-sxm", "healthy", True),
            ("dev_01j9x2ffffffffffffffffff03", "h100-80gb-sxm", "degraded", False),
            ("dev_01j9x2ffffffffffffffffff04", "l4-24gb-pcie", "healthy", False),
        ]
        async with sessions.begin() as db:
            for index, (uid, klass, health, allocatable) in enumerate(rows):
                await db.execute(
                    text(
                        "INSERT INTO supply_device"
                        " (uid, cluster_id, node_id, device_uuid, device_index, device_class,"
                        "  memory_mib, health, allocatable)"
                        " VALUES (:uid, :cluster, :node, :uuid, :index, :class, 81559, :health, :alloc)"
                    ),
                    {
                        "uid": uid,
                        "cluster": cluster,
                        "node": node,
                        "uuid": f"GPU-{index}",
                        "index": index,
                        "class": klass,
                        "health": health,
                        "alloc": allocatable,
                    },
                )

        async with sessions() as db:
            service = DeviceClassService(db=db)
            result = await service.list(present=True)

        by_name = {item.name: item for item in result.items}
        assert [item.name for item in result.items] == ["h100-80gb-sxm", "l4-24gb-pcie"]

        h100 = by_name["h100-80gb-sxm"]
        assert (h100.inventory.devices, h100.inventory.healthy, h100.inventory.allocatable) == (3, 2, 2)
        assert h100.inventory.nodes == 1
        assert h100.inventory.clusters == ["alpha"]
        assert h100.label == "NVIDIA H100 80GB SXM"
        assert h100.in_catalog is True

    async def test_a_class_nobody_owns_reports_zero_rather_than_disappearing(self, sessions, cluster):
        async with sessions() as db:
            found = await DeviceClassService(db=db).get("b200-180gb-sxm")
        assert found.inventory.devices == 0
        assert found.inventory.clusters == []
        assert found.in_catalog is True

    async def test_a_name_that_is_neither_owned_nor_catalogued_is_not_found(self, sessions):
        async with sessions() as db:
            with pytest.raises(NotFound):
                await DeviceClassService(db=db).get("no-such-card")


def test_a_rack_scale_part_carries_its_node_size_and_an_hgx_board_does_not():
    """An NVL72 is 72 devices and one node; the interconnect is what makes a node a node."""
    catalogue = table()
    for name in ("gb200-186gb-nvl", "gb300-288gb-nvl"):
        entry = catalogue.get(name)
        assert entry is not None, f"{name} is missing from the contract table"
        assert entry.devices_per_node == 72

    hgx = catalogue.get("b300-288gb-sxm")
    assert hgx is not None
    assert hgx.devices_per_node is None, "an HGX board takes the caller's default, not a table value"
