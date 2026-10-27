"""The batched artifact usage read's parameters, which are refused before any database read."""

import pytest

from litestar.testing import TestClient

from app.core import ids
from app.api.app import create_app
from app.core.exceptions import Invalid
from app.modules.artifacts.usage import requested_uids


def test_the_batched_usage_read_takes_ids_only_and_a_page_at_most():
    uid = ids.new_uid(ids.ARTIFACT)
    assert requested_uids([uid, f" {uid} ", ""]) == [uid]
    with pytest.raises(Invalid):
        requested_uids([])
    with pytest.raises(Invalid):
        requested_uids([uid, "zz-weights"])
    with pytest.raises(Invalid):
        requested_uids([ids.new_uid(ids.ARTIFACT) for _ in range(201)])


def test_a_name_is_refused_on_the_wire_and_the_route_is_not_read_as_a_ref():
    with TestClient(app=create_app(), base_url="http://localhost") as client:
        response = client.get("/v1/artifacts/usage", params={"id": ["zz-weights"]})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid"
    assert response.json()["detail"] == {"not_ids": ["zz-weights"]}
