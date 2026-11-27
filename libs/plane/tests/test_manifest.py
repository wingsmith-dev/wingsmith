"""The manifest schema: markers a client applies and diffs by, and the path layout."""

import pytest

from app.api.app import create_app
from app.modules.manifest.routes import ManifestRoutes
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
    assert immutable["WorkloadManifestSpec"] == ["cluster", "enabled", "gangSize", "kind"]
    assert immutable["PlacementSpec"] == ["allowedClusters", "allowedRegions"]
    assert immutable["ArtifactSpec"] == ["digest", "source"]


def test_server_defaults_are_marked_so_a_diff_skips_them_when_undeclared() -> None:
    defs = json_schema()["$defs"]
    assert _props(defs, "WorkloadManifestSpec")["priorityClass"]["x-defaulted"] is True
    assert _props(defs, "EndpointSpec")["slug"]["x-defaulted"] is True
    assert _props(defs, "ArtifactSpec")["slug"]["x-defaulted"] is True


def test_every_field_is_camel_case() -> None:
    defs = json_schema()["$defs"]
    snake = sorted(f"{name}.{k}" for name, body in defs.items() for k in body.get("properties", {}) if "_" in k)
    assert snake == []
    assert set(_props(defs, "EndpointTargetSpec")) >= {"workloadRef", "servedModelName"}
    assert "lastTransitionTime" in _props(defs, "Condition")


def test_the_http_api_stays_snake_case() -> None:
    from app.modules.workloads.schemas import ShapeSchema

    assert "device_class" in ShapeSchema.__struct_encode_fields__


def test_the_api_version_is_the_plane_group_not_the_operators() -> None:
    assert API_VERSION == "plane.wingsmith.dev/v1alpha1"
    defs = json_schema()["$defs"]
    assert _props(defs, "WorkloadManifest")["apiVersion"]["default"] == API_VERSION
    assert set(_props(defs, "ObjectMeta")) >= {"name", "namespace", "creationTimestamp", "resourceVersion"}


def test_the_manifest_routes_are_addressed_by_namespace_and_name() -> None:
    paths = {p for p in create_app().openapi_schema.to_schema()["paths"] if p.startswith("/v1/manifests")}
    assert paths == {
        "/v1/manifests/workloads/{project}/{name}",
        "/v1/manifests/workloads/{project}/{name}/scale",
        "/v1/manifests/endpoints/{project}/{name}",
        "/v1/manifests/artifacts/{project}/{name}",
        "/v1/manifests/artifacts/{name}",
    }


def test_workloads_and_endpoints_are_applied_with_put() -> None:
    paths = create_app().openapi_schema.to_schema()["paths"]
    assert {"get", "put"} <= set(paths["/v1/manifests/workloads/{project}/{name}"])
    assert "put" not in paths["/v1/manifests/artifacts/{project}/{name}"]


@pytest.mark.parametrize("created, status", [(True, 201), (False, 200)])
@pytest.mark.parametrize("handler, method", [("put_workload_manifest", "put_workload")])
async def test_put_answers_201_on_create_and_200_on_replace(handler, method, created, status) -> None:
    seen = {}

    class Svc:
        async def _put(self, project, name, body, if_match):
            seen.update(project=project, name=name, body=body, if_match=if_match)
            return "manifest", created

    setattr(Svc, method, Svc._put)

    class Req:
        async def json(self):
            return {"metadata": {"name": "n"}}

    fn = getattr(ManifestRoutes, handler).fn
    response = await fn(None, request=Req(), svc=Svc(), project="p", name="n", data=None, if_match="7")
    assert (response.status_code, response.content) == (status, "manifest")
    assert seen == {"project": "p", "name": "n", "body": {"metadata": {"name": "n"}}, "if_match": "7"}
