from types import SimpleNamespace

import pytest

from litestar.testing import TestClient
from litestar.testing import create_test_client

from app.core import ids
from app.config import settings
from app.api.app import create_app
from app.core.throttle import KeyedFailureBudget
from app.core.throttle import client_address
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


COLLECTIONS = (
    "clusters",
    "nodes",
    "devices",
    "topology-domains",
    "pools",
    "artifacts",
    "workloads",
    "deployments",
    "endpoints",
    "projects",
    "quotas",
    "priority-classes",
    "policies",
    "allocations",
    "operations",
    "components",
    "events",
)

OBSERVED = ("nodes", "devices", "topology-domains", "allocations", "events")


def test_v1_routes_are_mounted():
    paths = {route.path for route in create_app().routes}
    assert "/health" in paths
    for collection in COLLECTIONS:
        assert f"/v1/{collection}" in paths


def test_observed_collections_reject_writes():
    """Observed objects are discovered, not created — API.md §6."""
    methods = {route.path: route.methods for route in create_app().routes}
    for collection in OBSERVED:
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


def test_an_invalid_request_is_422_and_a_conflict_stays_409():
    from litestar import get

    from app.core.handlers import EXCEPTION_HANDLERS
    from app.core.exceptions import Conflict

    @get("/invalid", sync_to_thread=False)
    def invalid() -> None:
        raise Invalid("devices are observed, not created")

    @get("/conflict", sync_to_thread=False)
    def conflict() -> None:
        raise Conflict("revision mismatch")

    handlers = [invalid, conflict]
    with create_test_client(route_handlers=handlers, exception_handlers=EXCEPTION_HANDLERS) as client:
        refused = client.get("/invalid")
        clashed = client.get("/conflict")

    assert (refused.status_code, refused.json()["code"]) == (422, "invalid")
    assert (clashed.status_code, clashed.json()["code"]) == (409, "conflict")


def test_framework_404_and_405_carry_contract_codes():
    with TestClient(app=create_app()) as client:
        missing = client.get("/v1/no-such-collection")
        not_allowed = client.post("/v1/devices", json={})

    assert (missing.status_code, missing.json()["code"]) == (404, "not_found")
    assert (not_allowed.status_code, not_allowed.json()["code"]) == (405, "method_not_allowed")


# POST routes that create a resource. Every other POST is an action on an existing one and answers 200.
CREATES = {
    "/v1/alert-thresholds",
    "/v1/artifacts",
    "/v1/artifacts/{ref:str}/baselines",
    "/v1/benchmarks",
    "/v1/benchmarks/requests",
    "/v1/clusters",
    "/v1/clusters/{ref:str}/enrolment-token",
    "/v1/components",
    "/v1/discovered/{ref:str}/promote",
    "/v1/endpoints",
    "/v1/notification-channels",
    "/v1/operations",
    "/v1/policies",
    "/v1/pools",
    "/v1/priority-classes",
    "/v1/projects",
    "/v1/quotas",
    "/v1/serving-quotas",
    "/v1/tenants",
    "/v1/tenants/{ref:str}/keys",
    "/v1/tenants/{ref:str}/tokens",
    "/v1/workloads",
}


def _post_routes():
    for route in create_app().routes:
        entry = getattr(route, "route_handler_map", {}).get("POST")
        if entry is not None:
            yield route.path, (entry[0] if isinstance(entry, tuple) else entry).status_code


def test_only_creates_answer_201():
    created = {path for path, status in _post_routes() if status == 201}
    assert created == CREATES


def test_unknown_query_parameter_is_rejected_with_the_contract_envelope():
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/v1/nodes", params={"status": "ready"})

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "unknown_parameter"
    assert body["detail"]["unknown"] == ["status"]
    assert "cordoned" in body["detail"]["accepted"]


def test_known_query_parameter_passes_the_gate():
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/v1/nodes", params={"cordoned": "true"})

    assert response.json().get("code") != "unknown_parameter"


def test_repeated_label_is_accepted():
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/v1/nodes?label=tier%3Dgold&label=zone%3Da")

    assert response.json().get("code") != "unknown_parameter"


def test_a_filter_valid_on_one_collection_is_rejected_on_another():
    """`severity` is an events filter; nodes does not declare it."""
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        assert client.get("/v1/events", params={"severity": "warning"}).json().get("code") != "unknown_parameter"
        rejected = client.get("/v1/nodes", params={"severity": "warning"})

    assert rejected.status_code == 400
    assert rejected.json()["detail"]["unknown"] == ["severity"]


SINGLE_PAGE_LISTS = {
    "/v1/device-classes",
    "/v1/benchmarks/harnesses",
    "/v1/alert-rules",
    "/v1/benchmarks/requests/{ref:str}/bands",
}
"""List routes that deliberately answer in one page: fixed catalogues, not collections."""


def _list_routes():
    for route in create_app().routes:
        for method, entry in getattr(route, "route_handler_map", {}).items():
            handler = entry[0] if isinstance(entry, tuple) else entry
            if method != "GET" or getattr(handler.fn, "__module__", "").startswith("litestar."):
                continue
            if "ListResponse" not in str(handler.parsed_fn_signature.return_type.annotation):
                continue
            model = handler.create_kwargs_model(path_parameters=dict.fromkeys(route.path_parameters))
            yield route.path, {param.field_alias for param in model.expected_query_params}


def test_every_paginated_list_route_accepts_limit_and_cursor():
    """The envelope promises a cursor, so the route has to take one back.

    A handler that returns `ListResponse` without declaring `limit` and `cursor`
    reads as paginated and answers `400 unknown_parameter` to the second page —
    and because `reject_unknown_params` is doing its job, the client sees the
    failure on the *first* request, not the second. `/clusters/rollups` shipped
    that way and every cluster card rendered its counts as unknown. Adding a
    route to `SINGLE_PAGE_LISTS` is how a fixed catalogue opts out, deliberately.
    """
    missing = {
        path: sorted({"limit", "cursor"} - accepted)
        for path, accepted in _list_routes()
        if path not in SINGLE_PAGE_LISTS and not {"limit", "cursor"} <= accepted
    }
    assert missing == {}


def test_the_single_page_allowlist_names_only_routes_that_exist():
    """A stale exemption silently excuses a route nobody meant to exempt."""
    assert SINGLE_PAGE_LISTS <= {path for path, _ in _list_routes()}


def test_aliased_parameter_is_accepted_under_its_wire_name():
    """`/v1/devices` declares `device_class` as `Parameter(query="class")`."""
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        accepted = client.get("/v1/devices", params={"class": "h100"})
        rejected = client.get("/v1/devices", params={"device_class": "h100"})

    assert accepted.json().get("code") != "unknown_parameter"
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["unknown"] == ["device_class"]


def test_health_is_unaffected():
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": True}


def test_slug_is_read_from_the_location():
    """`hf://openai/gpt-oss-20b` -> `openai/gpt-oss-20b`, and this is the whole repair.

    Every artifact in the fleet was named `gpt-oss-20b`, `Kimi-K3`, `GLM-5.3-Flash` — bare, no
    organisation — because a name is what a person types. The organisation was only ever written
    in the location, which nothing read, so the plane published a model id no frontend had
    registered while the operator started the engine on `--model` taken from that same location.
    """
    from app.core.serving import model_slug

    assert model_slug("hf://openai/gpt-oss-20b") == "openai/gpt-oss-20b"
    assert model_slug("hf://moonshotai/Kimi-K3") == "moonshotai/kimi-k3"


def test_a_pinned_revision_is_not_part_of_what_a_model_is_called():
    """Two revisions of one repository are one model id, and `model` must resolve to one endpoint.

    The revision says which bytes; the slug says which model. Keeping the `@sha` would give a
    caller a name that changes every time the weights are re-pinned.
    """
    from app.core.serving import model_slug

    assert model_slug("hf://deepseek-ai/DeepSeek-V4-Flash-0731@7872f01b") == ("deepseek-ai/deepseek-v4-flash-0731")


def test_preferred_location_names_the_model():
    from app.core.serving import artifact_slug

    class Loc:
        def __init__(self, uri, preferred=False):
            self.uri = uri
            self.preferred = preferred

    mirror, home = Loc("s3://weights-mirror/kimi"), Loc("hf://moonshotai/Kimi-K3", preferred=True)
    assert artifact_slug("sha256:beef", [mirror, home]) == "moonshotai/kimi-k3"
    assert artifact_slug("sha256:beef", [mirror]) == "weights-mirror/kimi"


def test_an_adopted_artifact_is_named_by_its_reference_digest():
    """Weights adopted by watching a cluster serve them have no location at all.

    The reference is the digest — `hf:org/model`, which is also why such a row is unverified — so
    that is read next rather than leaving the model with no name.
    """
    from app.core.serving import artifact_slug

    assert artifact_slug("hf:Qwen/Qwen3-Embedding-8B", []) == "qwen/qwen3-embedding-8b"
    assert artifact_slug("sha256:beef", []) == ""


# --- failure budgets and the caller's address ---------------------------------


def peer(host: str, forwarded: str | None = None) -> SimpleNamespace:
    headers = {"X-Forwarded-For": forwarded} if forwarded is not None else {}
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers)


def test_a_keyed_budget_charges_only_the_key_that_failed():
    budget = KeyedFailureBudget(limit=2, window_seconds=60.0)
    budget.fail("a")
    budget.fail("a")
    assert budget.spent("a")
    assert not budget.spent("b")


def test_a_keyed_budget_holds_at_most_max_keys():
    budget = KeyedFailureBudget(limit=1, window_seconds=60.0, max_keys=3)
    for key in "abcde":
        budget.fail(key)
    assert len(budget._budgets) == 3
    assert not budget.spent("a")
    assert budget.spent("e")


def test_forwarded_for_is_ignored_without_trusted_proxies(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "")
    assert client_address(peer("10.0.0.5", "203.0.113.7")) == "10.0.0.5"


def test_forwarded_for_is_read_from_the_right_through_trusted_hops(monkeypatch):
    """The leftmost entry is whatever the caller wrote; the first untrusted hop from the right is not."""
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "10.0.0.0/8")
    assert client_address(peer("10.0.0.5", "198.51.100.1, 203.0.113.7, 10.1.2.3")) == "203.0.113.7"


def test_forwarded_for_from_an_untrusted_peer_is_ignored(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "10.0.0.0/8")
    assert client_address(peer("192.0.2.9", "203.0.113.7")) == "192.0.2.9"


def test_an_ipv6_caller_is_charged_as_its_slash_64(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "")
    assert client_address(peer("2001:db8:1:2:aaaa::1")) == client_address(peer("2001:db8:1:2:bbbb::9"))
    assert client_address(peer("2001:db8:1:2::1")) != client_address(peer("2001:db8:1:3::1"))


def test_revision_follows_the_last_write_and_if_match_refuses_an_older_one():
    import datetime as dt

    from types import SimpleNamespace

    from app.core.revision import revision
    from app.core.revision import check_if_match
    from app.core.exceptions import Conflict

    created = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    row = SimpleNamespace(created_at=created, updated_at=None)
    first = revision(row)
    check_if_match(row, first, "thing")
    check_if_match(row, None, "thing")

    row.updated_at = created + dt.timedelta(microseconds=1)
    assert revision(row) != first
    with pytest.raises(Conflict) as caught:
        check_if_match(row, first, "thing")
    assert caught.value.detail == {"revision": revision(row)}
    assert caught.value.message == "thing changed since it was read"
