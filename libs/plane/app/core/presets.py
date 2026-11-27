"""Engine presets (P8.3): `runtime.container.preset` expanded into a full container worker.

A preset is a template for `kind: container`, not a runtime of its own: the plane stores what was
declared and expands it wherever the worker is used (validation, the wire, reads), so a cluster
and a host see a plain container spec and never learn presets exist.
"""

import msgspec

from app.config import settings
from app.core.serving import MODELS_PROBE_PROTOCOLS
from app.core.exceptions import Invalid
from app.modules.workloads.schemas import ContainerSchema
from app.modules.workloads.schemas import ContainerPortSchema
from app.modules.workloads.schemas import ContainerMetricsSchema


VLLM = "vllm"
SGLANG = "sglang"
PRESETS = frozenset({VLLM, SGLANG})
PRESET_PORT = 8000
STARTUP_FAILURES = 180
"""Startup probe budget at a 10 s period: 30 minutes for weights to load and graphs to compile."""
RESERVED_FLAGS = frozenset(
    {
        "--port",
        "--host",
        "--model",
        "--model-path",
        "--served-model-name",
        "--tensor-parallel-size",
        "--tp-size",
        "--tp",
        "-tp",
    }
)
"""Flags the preset sets itself (vLLM and SGLang spellings). Engines take the last occurrence, so a
user arg repeating one would silently move the port, model or parallelism the plane relies on."""


def preset_images() -> dict[str, dict[str, str]]:
    """`ENGINE_PRESET_IMAGES` as `preset -> {version: image}`, in declared order."""
    table: dict[str, dict[str, str]] = {}
    for entry in settings.ENGINE_PRESET_IMAGES.split(","):
        key, _, image = entry.strip().partition("=")
        preset, _, version = key.strip().partition("@")
        if preset and version and image:
            table.setdefault(preset.strip().lower(), {})[version.strip().lstrip("v")] = image.strip()
    return table


def default_version(preset: str) -> str:
    versions = preset_images().get(preset, {})
    return next(iter(versions), "")


def preset_image(preset: str, version: str) -> str:
    """The digest-pinned image a preset version runs, or `Invalid` saying how to fix the table."""
    if preset not in PRESETS:
        raise Invalid(f"runtime.container.preset must be one of {sorted(PRESETS)}, not {preset!r}", {"preset": preset})
    versions = preset_images().get(preset, {})
    version = (version or default_version(preset)).lstrip("v")
    image = versions.get(version)
    if image is None:
        raise Invalid(
            f"{preset} {version!r} is not in ENGINE_PRESET_IMAGES; known: {sorted(versions)}",
            {"preset": preset, "version": version},
        )
    if "@sha256:" not in image:
        raise Invalid(
            f"{preset} {version} has no pinned digest in ENGINE_PRESET_IMAGES ({image}); "
            "add repo@sha256:... per docs/operations/custom-workers.md",
            {"preset": preset, "version": version},
        )
    return image


def _serve_args(preset: str, tensor_parallel: int) -> tuple[list[str], list[str]]:
    common = ["--served-model-name", "$(WS_SERVED_MODEL_NAME)", "--host", "0.0.0.0", "--port", "$(WS_PORT)"]
    if preset == VLLM:
        return ["vllm", "serve"], ["$(WS_MODEL_PATH)", *common, "--tensor-parallel-size", str(tensor_parallel)]
    return ["python3", "-m", "sglang.launch_server"], [
        "--model-path",
        "$(WS_MODEL_PATH)",
        *common,
        "--tp-size",
        str(tensor_parallel),
        "--enable-metrics",
    ]


def _http_get(path: str, **extra) -> dict:
    return {"httpGet": {"path": path, "port": "http"}, "periodSeconds": 10, **extra}


def expand(container: ContainerSchema, tensor_parallel: int) -> ContainerSchema:
    """A declared container with a preset, as the full worker it stands for. No preset: unchanged.

    The preset owns image, command and the flags in `RESERVED_FLAGS`; declared `args` are appended
    after its own and may not repeat a reserved flag. Declared ports, env, probes and metrics win
    over the defaults.
    Tensor parallelism is the shape's device count: one replica is one node.
    """
    if not container.preset:
        return container
    if container.image or container.command:
        raise Invalid("a preset picks the image and command; declare args instead", {"preset": container.preset})
    reserved = sorted({a.partition("=")[0] for a in container.args} & RESERVED_FLAGS)
    if reserved:
        raise Invalid(
            f"the {container.preset} preset sets {', '.join(reserved)} itself; remove it from args",
            {"preset": container.preset, "args": reserved},
        )
    image = preset_image(container.preset, container.version)
    command, args = _serve_args(container.preset, max(tensor_parallel, 1))
    ready = "/v1/models" if container.protocol in MODELS_PROBE_PROTOCOLS else "/health"
    return msgspec.structs.replace(
        container,
        image=image,
        command=command,
        args=[*args, *container.args],
        ports=container.ports or [ContainerPortSchema(name="http", container_port=PRESET_PORT)],
        readiness_probe=container.readiness_probe or _http_get(ready),
        startup_probe=container.startup_probe or _http_get("/health", failureThreshold=STARTUP_FAILURES),
        metrics=container.metrics or ContainerMetricsSchema(port="http", path="/metrics"),
    )
