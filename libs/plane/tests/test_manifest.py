"""The manifest schema: markers a client applies and diffs by, and the path layout."""

from app.api.app import create_app
from app.modules.manifest.schemas import API_VERSION
from app.modules.manifest.schemas import json_schema


def _props(defs: dict, name: str) -> dict:
    return defs[name]["properties"]


def test_create_only_fields_are_marked_immutable() -> None:
    defs = json_schema()["$defs"]
    immutable = {
        name: sorted(k for k, v in body.get("properties", {}).items() if v.get("x-immutable"))
        for name, body in defs.items()
    }
    assert immutable["WorkloadSpec"] == ["cluster", "enabled", "gang_size", "kind"]
    assert immutable["PlacementSpec"] == ["allowed_clusters", "allowed_regions"]
    assert immutable["ArtifactSpec"] == ["digest", "source"]


def test_server_defaults_are_marked_so_a_diff_skips_them_when_undeclared() -> None:
    defs = json_schema()["$defs"]
    assert _props(defs, "WorkloadSpec")["priority_class"]["x-defaulted"] is True
    assert _props(defs, "EndpointSpec")["slug"]["x-defaulted"] is True
    assert _props(defs, "ArtifactSpec")["slug"]["x-defaulted"] is True


def test_the_api_version_is_the_plane_group_not_the_operators() -> None:
    assert API_VERSION == "plane.wingsmith.dev/v1alpha1"
    defs = json_schema()["$defs"]
    assert _props(defs, "WorkloadManifest")["apiVersion"]["default"] == API_VERSION
    assert set(_props(defs, "ObjectMeta")) >= {"name", "namespace", "creationTimestamp", "resourceVersion"}


def test_the_manifest_routes_are_addressed_by_namespace_and_name() -> None:
    paths = {p for p in create_app().openapi_schema.to_schema()["paths"] if p.startswith("/v1/manifests")}
    assert paths == {
        "/v1/manifests/workloads/{project}/{name}",
        "/v1/manifests/endpoints/{project}/{name}",
        "/v1/manifests/artifacts/{project}/{name}",
        "/v1/manifests/artifacts/{name}",
    }
