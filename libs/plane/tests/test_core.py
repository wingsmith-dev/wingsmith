import pytest

from litestar.testing import create_test_client

from app.core import ids
from app.api.app import create_app
from app.core.exceptions import Invalid
from app.core.pagination import MAX_LIMIT
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.core.pagination import encode_cursor
from app.modules.clusters.routes import parse_labels


def test_new_uid_is_prefixed_and_recognised():
    uid = ids.new_uid(ids.CLUSTER)
    assert uid.startswith("clu_")
    assert ids.is_uid(uid)


def test_new_uid_rejects_unknown_prefix():
    with pytest.raises(ValueError):
        ids.new_uid("nope_")


def test_uids_sort_in_creation_order():
    first = ids.new_uid(ids.NODE)
    second = ids.new_uid(ids.NODE)
    assert first < second


@pytest.mark.parametrize("value", ["iad-1", "clu_short", "", "clu_" + "u" * 26])
def test_names_are_not_mistaken_for_ids(value):
    assert not ids.is_uid(value)


def test_cursor_round_trip():
    assert decode_cursor(encode_cursor(4412)) == 4412


def test_cursor_is_opaque():
    assert encode_cursor(4412) != "4412"


def test_bad_cursor_is_invalid_not_a_crash():
    with pytest.raises(Invalid):
        decode_cursor("!!!!")


def test_limit_is_clamped_not_rejected():
    assert clamp_limit(None) > 0
    assert clamp_limit(10_000) == MAX_LIMIT
    with pytest.raises(Invalid):
        clamp_limit(0)


def test_page_emits_a_cursor_only_when_a_row_was_over_fetched():
    kept, cursor = page([1, 2, 3], 3, lambda r: r)
    assert (kept, cursor) == ([1, 2, 3], None)

    kept, cursor = page([1, 2, 3, 4], 3, lambda r: r)
    assert kept == [1, 2, 3]
    assert decode_cursor(cursor) == 3


def test_labels_parse_into_a_selector():
    assert parse_labels(["tier=gold", "zone=a"]) == {"tier": "gold", "zone": "a"}


def test_malformed_label_is_rejected():
    with pytest.raises(Invalid):
        parse_labels(["tier"])


def test_v1_routes_are_mounted():
    paths = {route.path for route in create_app().routes}
    assert "/health" in paths
    for collection in ("clusters", "nodes", "devices", "topology-domains", "pools", "events"):
        assert f"/v1/{collection}" in paths


def test_observed_collections_reject_writes():
    """Observed objects are discovered, not created — API.md §6."""
    methods = {route.path: route.methods for route in create_app().routes}
    for collection in ("nodes", "devices", "topology-domains", "events"):
        assert "POST" not in methods[f"/v1/{collection}"]


def test_plane_errors_render_as_the_contract_envelope():
    from litestar import get

    from app.core.handlers import EXCEPTION_HANDLERS
    from app.core.exceptions import NotFound

    @get("/boom", sync_to_thread=False)
    def boom() -> None:
        raise NotFound("no cluster 'iad-9'", {"ref": "iad-9"})

    with create_test_client(route_handlers=[boom], exception_handlers=EXCEPTION_HANDLERS) as client:
        response = client.get("/boom")

    assert response.status_code == 404
    assert response.json() == {
        "code": "not_found",
        "message": "no cluster 'iad-9'",
        "detail": {"ref": "iad-9"},
    }
