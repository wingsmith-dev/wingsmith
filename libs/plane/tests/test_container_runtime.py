"""P8.2: the `container` runtime on the plane: validation, digest, wire and manifest."""

import json
import hashlib

import pytest
import msgspec

from app.config import settings
from app.core.serving import DIGEST_FIELDS
from app.core.serving import _canonical
from app.core.serving import serving_digest
from app.core.exceptions import Invalid
from app.ingestion.desired import _serving
from app.modules.manifest.schemas import WorkloadManifest
from app.modules.workloads.schemas import EnvVarSchema
from app.modules.workloads.schemas import RouterSchema
from app.modules.workloads.schemas import OverlaySchema
from app.modules.workloads.schemas import RuntimeSchema
from app.modules.workloads.schemas import TopologySchema
from app.modules.workloads.schemas import ContainerSchema
from app.modules.workloads.schemas import ContainerPortSchema
from app.modules.workloads.serving import serving_view
from app.modules.workloads.serving import apply_serving
from app.modules.workloads.serving import serving_request
from app.modules.manifest.translate import runtime_spec
from app.modules.manifest.translate import workload_create
from app.modules.workloads.validation import check_runtime

from .test_serving import a_request
from .test_serving import a_workload
from .test_serving import an_artifact


IMAGE = "ghcr.io/acme/worker@sha256:" + "a" * 64


def a_container(**kwargs) -> ContainerSchema:
    return ContainerSchema(
        **({"image": IMAGE, "ports": [ContainerPortSchema(name="http", container_port=8000)]} | kwargs)
    )


def a_container_request(**kwargs):
    container = kwargs.pop("container", a_container())
    return a_request(
        engine="", engine_runtime_version="", runtime=RuntimeSchema(kind="container", container=container), **kwargs
    )


def legacy_digest(workload, artifact) -> str:
    """The digest as it was computed before runtimes existed, for an aggregated workload with no
    router and no recipe defaults."""
    payload = {"artifact": str(artifact.uid)}
    for key, attribute in DIGEST_FIELDS:
        payload[key] = _canonical(getattr(workload, attribute))
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "svg_" + hashlib.sha256(body.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("kind", [None, "dynamo"])
def test_a_dynamo_digest_is_byte_identical_to_before_runtimes_existed(kind):
    artifact = an_artifact()
    workload = a_workload(runtime_kind=kind, runtime_spec={})
    assert serving_digest(workload, artifact, []) == legacy_digest(workload, artifact)


def test_a_container_digest_covers_the_worker():
    artifact = an_artifact()
    one = a_workload(engine="container", runtime_kind="container", runtime_spec={"image": IMAGE})
    other = a_workload(
        engine="container", runtime_kind="container", runtime_spec={"image": "ghcr.io/acme/worker@sha256:" + "b" * 64}
    )
    assert serving_digest(one, artifact, []) != legacy_digest(one, artifact)
    assert serving_digest(one, artifact, []) != serving_digest(other, artifact, [])


def test_a_container_request_is_stored_and_read_back():
    artifact = an_artifact()
    workload = a_workload()
    request = a_container_request(context_length=0)
    check_runtime(request)
    apply_serving(workload, request, artifact, [])

    assert workload.engine == "container"
    assert workload.runtime_kind == "container"
    assert workload.engine_runtime_image is None
    assert workload.runtime_spec["image"] == IMAGE
    assert serving_request(workload, artifact).runtime == request.runtime
    view = serving_view(workload, artifact)
    assert view.runtime.container.image == IMAGE
    assert view.resolved_engine_args == {}


def test_the_wire_carries_the_container_only_off_dynamo():
    artifact = an_artifact()
    dynamo = a_workload(runtime_kind="dynamo", runtime_spec={})
    assert _serving(dynamo, artifact).runtime is None
    assert _serving(dynamo, artifact).container is None

    workload = a_workload()
    apply_serving(workload, a_container_request(), artifact, [])
    spec = _serving(workload, artifact)
    assert spec.runtime == "container"
    assert spec.container.image == IMAGE
    assert spec.container.ports[0].container_port == 8000


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"container": a_container(image="ghcr.io/acme/worker:latest")},
        {"container": a_container(protocol="grpc")},
        {"container": a_container(ports=[ContainerPortSchema(name="api", container_port=8000)])},
        {"container": a_container(env=[EnvVarSchema(name="WS_MODEL_PATH", value="/elsewhere")])},
        {"container": a_container(env=[EnvVarSchema(name="WS_FUTURE", value="1")])},
        {"container": a_container(env=[EnvVarSchema(name="A=B", value="1")])},
        {"container": a_container(liveness_probe={"exec": {"command": ["true"]}})},
        {"topology": TopologySchema(serving_topology="disaggregated")},
        {"router": RouterSchema(mode="kv")},
        {"engine_args": {"max-num-seqs": "8"}},
        {"overlay": OverlaySchema(pod_spec={"hostNetwork": True})},
    ],
    ids=[
        "tag",
        "protocol",
        "no-http-port",
        "injected-env",
        "reserved-env-prefix",
        "invalid-env-name",
        "exec-probe",
        "disaggregated",
        "router",
        "engine-args",
        "overlay",
    ],
)
def test_a_container_request_is_refused(request_kwargs):
    with pytest.raises(Invalid):
        check_runtime(a_container_request(**request_kwargs))


def test_a_tag_is_accepted_behind_the_admin_flag(monkeypatch):
    monkeypatch.setattr(settings, "CONTAINER_IMAGE_TAGS_ALLOWED", True)
    check_runtime(a_container_request(container=a_container(image="ghcr.io/acme/worker:1.0")))


def test_dynamo_still_needs_an_engine_and_no_container():
    with pytest.raises(Invalid):
        check_runtime(a_request(engine=""))
    with pytest.raises(Invalid):
        check_runtime(a_request(runtime=RuntimeSchema(kind="dynamo", container=a_container())))
    with pytest.raises(Invalid):
        check_runtime(a_request(runtime=RuntimeSchema(kind="wasm")))
    check_runtime(a_request())


MANIFEST = {
    "apiVersion": "plane.wingsmith.dev/v1alpha1",
    "kind": "Workload",
    "metadata": {"name": "my-worker", "namespace": "core"},
    "spec": {
        "runtime": {
            "kind": "container",
            "image": IMAGE,
            "args": ["--model", "$(WS_MODEL_PATH)"],
            "ports": [{"name": "http", "containerPort": 8000}],
            "protocol": "openai-chat",
            "readinessProbe": {"httpGet": {"path": "/v1/models", "port": "http"}},
        },
        "serving": {"artifact": "qwen3-8b", "shape": {"deviceClass": "l40s"}},
    },
}


def test_a_container_manifest_becomes_a_container_create():
    m = msgspec.convert(MANIFEST, WorkloadManifest, strict=False)
    create = workload_create(m, "prj_x")
    runtime = create.serving.runtime
    assert runtime.kind == "container"
    assert runtime.container.image == IMAGE
    assert runtime.container.args == ["--model", "$(WS_MODEL_PATH)"]
    assert runtime.container.readiness_probe == {"httpGet": {"path": "/v1/models", "port": "http"}}
    assert create.serving.engine == ""
    assert create.serving.engine_args == {}


def test_a_dynamo_manifest_without_kind_is_unchanged():
    body = {
        **MANIFEST,
        "spec": {**MANIFEST["spec"], "runtime": {"engine": "vllm", "version": "1.4.1", "args": {"a": "1"}}},
    }
    m = msgspec.convert(body, WorkloadManifest, strict=False)
    create = workload_create(m, "prj_x")
    assert create.serving.runtime.kind == "dynamo"
    assert create.serving.engine == "vllm"
    assert create.serving.engine_args == {"a": "1"}


def test_a_read_shows_each_runtime_only_its_own_fields():
    artifact = an_artifact()
    workload = a_workload()
    apply_serving(workload, a_container_request(), artifact, [])
    spec = msgspec.to_builtins(runtime_spec(_schema(workload, artifact)))
    assert spec["kind"] == "container"
    assert "engine" not in spec and "version" not in spec

    dynamo = msgspec.to_builtins(runtime_spec(_schema(a_workload(), artifact)))
    assert dynamo == {"kind": "dynamo", "engine": "vllm", "version": "0.6.0"}


def _schema(workload, artifact):
    class W:
        serving = serving_view(workload, artifact)

    return W
