"""P8.3: engine presets expand into a plain container worker on the wire and in reads."""

import pytest
import msgspec

from app.config import settings
from app.core.presets import expand
from app.core.presets import preset_image
from app.core.exceptions import Invalid
from app.ingestion.desired import _serving
from app.modules.manifest.schemas import WorkloadManifest
from app.modules.workloads.schemas import ShapeSchema
from app.modules.workloads.schemas import RuntimeSchema
from app.modules.workloads.schemas import ContainerSchema
from app.modules.workloads.serving import serving_view
from app.modules.workloads.serving import apply_serving
from app.modules.workloads.serving import serving_request
from app.modules.manifest.translate import runtime_spec
from app.modules.manifest.translate import workload_create
from app.modules.workloads.validation import check_runtime

from .test_serving import a_request
from .test_serving import a_workload
from .test_serving import an_artifact


VLLM = "docker.io/vllm/vllm-openai@sha256:" + "1" * 64
SGLANG = "docker.io/lmsysorg/sglang@sha256:" + "2" * 64


@pytest.fixture(autouse=True)
def table(monkeypatch):
    monkeypatch.setattr(
        settings,
        "ENGINE_PRESET_IMAGES",
        f"vllm@0.34.0={VLLM},vllm@0.33.0=docker.io/vllm/vllm-openai:v0.33.0,sglang@0.5.23={SGLANG}",
    )


def a_preset_request(device_count=1, **container):
    return a_request(
        engine="",
        engine_runtime_version="",
        context_length=0,
        shape=ShapeSchema(device_class="h100", device_count=device_count),
        runtime=RuntimeSchema(kind="container", container=ContainerSchema(**({"preset": "vllm"} | container))),
    )


def test_the_shipped_table_is_digest_pinned():
    for entry in type(settings)().ENGINE_PRESET_IMAGES.split(","):
        assert "@sha256:" in entry.split("=", 1)[1]


@pytest.mark.parametrize(
    ("preset", "tp_flag", "image"), [("vllm", "--tensor-parallel-size", VLLM), ("sglang", "--tp-size", SGLANG)]
)
def test_tensor_parallel_comes_from_the_shape(preset, tp_flag, image):
    out = expand(ContainerSchema(preset=preset, args=["--max-model-len", "8192"]), 4)
    assert out.image == image
    assert out.args[out.args.index(tp_flag) + 1] == "4"
    assert out.args[-2:] == ["--max-model-len", "8192"]
    assert "$(WS_MODEL_PATH)" in out.args and "$(WS_SERVED_MODEL_NAME)" in out.args
    assert out.ports[0].name == "http" and out.ports[0].container_port == 8000
    assert out.metrics.path == "/metrics"


@pytest.mark.parametrize(("protocol", "path"), [("openai-chat", "/v1/models"), ("openai-images", "/health")])
def test_readiness_follows_the_protocol(protocol, path):
    out = expand(ContainerSchema(preset="sglang", protocol=protocol), 1)
    assert out.readiness_probe["httpGet"]["path"] == path
    assert out.startup_probe["httpGet"]["path"] == "/health"


def test_a_declared_probe_wins():
    probe = {"httpGet": {"path": "/ready", "port": "http"}}
    assert expand(ContainerSchema(preset="vllm", readiness_probe=probe), 1).readiness_probe == probe


@pytest.mark.parametrize(
    ("version", "message"),
    [("0.33.0", "no pinned digest"), ("9.9.9", "not in ENGINE_PRESET_IMAGES")],
)
def test_a_preset_version_needs_a_pinned_digest(version, message):
    with pytest.raises(Invalid, match=message):
        preset_image("vllm", version)
    with pytest.raises(Invalid):
        check_runtime(a_preset_request(version=version))


@pytest.mark.parametrize("container", [{"preset": "tgi"}, {"image": VLLM}, {"command": ["sh"]}])
def test_a_preset_refuses_what_it_owns(container):
    with pytest.raises(Invalid):
        check_runtime(a_preset_request(**container))


def test_spec_stays_as_declared_and_the_read_shows_the_expansion():
    artifact = an_artifact()
    workload = a_workload()
    request = a_preset_request(device_count=2)
    check_runtime(request)
    apply_serving(workload, request, artifact, [])

    assert workload.runtime_spec["preset"] == "vllm"
    assert workload.runtime_spec["version"] == "0.34.0"
    assert workload.runtime_spec["image"] == ""
    assert serving_request(workload, artifact).runtime.resolved is None
    view = serving_view(workload, artifact)
    assert view.runtime.container.image == ""
    assert view.runtime.resolved.image == VLLM

    wire = _serving(workload, artifact).container
    assert wire.image == VLLM
    assert wire.args[wire.args.index("--tensor-parallel-size") + 1] == "2"
    assert wire.readiness_probe["httpGet"]["path"] == "/v1/models"
    assert wire.metrics.path == "/metrics"


def test_a_plain_container_stores_no_preset_keys():
    workload = a_workload()
    request = a_preset_request(preset=None, image=VLLM, ports=[{"name": "http", "container_port": 8000}])
    request = msgspec.convert(msgspec.to_builtins(request), type(request))
    apply_serving(workload, request, an_artifact(), [])
    assert "preset" not in workload.runtime_spec and "version" not in workload.runtime_spec
    assert serving_view(workload, an_artifact()).runtime.resolved is None


def test_a_preset_manifest_round_trips_with_the_expansion_in_status():
    body = {
        "apiVersion": "plane.wingsmith.dev/v1alpha1",
        "kind": "Workload",
        "metadata": {"name": "qwen", "namespace": "core"},
        "spec": {
            "runtime": {
                "kind": "container",
                "preset": "sglang",
                "version": "0.5.23",
                "args": ["--mem-fraction-static", "0.8"],
            },
            "serving": {"artifact": "qwen3-8b", "shape": {"deviceClass": "h100", "deviceCount": 1}},
        },
    }
    create = workload_create(msgspec.convert(body, WorkloadManifest, strict=False), "prj_x")
    container = create.serving.runtime.container
    assert (container.preset, container.version, container.image) == ("sglang", "0.5.23", "")

    artifact = an_artifact()
    workload = a_workload()
    apply_serving(workload, a_preset_request(preset="sglang", version="0.5.23"), artifact, [])

    class W:
        serving = serving_view(workload, artifact)

    declared = msgspec.to_builtins(runtime_spec(W))
    assert declared["preset"] == "sglang" and "image" not in declared
    resolved = msgspec.to_builtins(runtime_spec(W, resolved=True))
    assert resolved["image"] == SGLANG and "preset" not in resolved


@pytest.mark.parametrize(
    "flag",
    [
        "--port",
        "--host=0.0.0.0",
        "--model",
        "--model-path",
        "--served-model-name=x",
        "--tensor-parallel-size",
        "--tp-size=2",
        "--tp",
        "-tp",
    ],
)
@pytest.mark.parametrize("preset", ["vllm", "sglang"])
def test_args_may_not_override_preset_flags(preset, flag):
    with pytest.raises(Invalid, match=flag.partition("=")[0]):
        expand(ContainerSchema(preset=preset, args=["--max-model-len", "8192", flag, "9"]), 1)


def test_shipped_preset_images_are_append_only():
    from pathlib import Path

    from app.config.settings import Settings

    def entries(items):
        pairs = (e.strip().partition("=") for e in items)
        return {k: v for k, _, v in pairs if k and not k.startswith("#")}

    shipped = entries((Path(__file__).parent / "data" / "preset_images.txt").read_text().splitlines())
    current = entries(Settings.ENGINE_PRESET_IMAGES.split(","))
    changed = {k: v for k, v in shipped.items() if current.get(k) != v}
    assert not changed, f"shipped preset entries removed or re-pointed: {changed}"
