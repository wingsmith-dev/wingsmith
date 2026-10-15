"""Image pinning: a registry image runs by digest or not at all."""

from __future__ import annotations

import json
import subprocess

import pytest

from hgrbench.harnesses import base
from hgrbench.harnesses import registry
from hgrbench.harnesses.base import HarnessError


INDEX = "sha256:" + "a" * 64
ARM = "sha256:" + "b" * 64
AMD = "sha256:" + "c" * 64


def _inspect(monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str = "", stderr: str = "") -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(base.subprocess, "run", fake)
    return calls


def _index() -> str:
    return json.dumps(
        {
            "manifests": [
                {"digest": AMD, "platform": {"architecture": "amd64", "os": "linux"}},
                {"digest": ARM, "platform": {"architecture": "arm64", "os": "linux", "variant": "v8"}},
            ]
        }
    )


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("ghcr.io/vllm-project/guidellm:v0.7.3", "ghcr.io/vllm-project/guidellm"),
        ("ghcr.io/vllm-project/guidellm@" + INDEX, "ghcr.io/vllm-project/guidellm"),
        ("localhost:5000/bench:1", "localhost:5000/bench"),
        ("localhost:5000/bench", "localhost:5000/bench"),
        ("python", "python"),
    ],
)
def test_repository_strips_tag_and_digest_but_not_a_port(image: str, expected: str) -> None:
    assert base.repository(image) == expected


def test_resolves_this_architectures_variant_of_the_pinned_index(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _inspect(monkeypatch, 0, _index())

    pinned = base.resolve_digest("ghcr.io/vllm-project/guidellm@" + INDEX, "arm64")

    assert pinned == "ghcr.io/vllm-project/guidellm@" + ARM
    assert calls == [["docker", "manifest", "inspect", "ghcr.io/vllm-project/guidellm@" + INDEX]]


def test_an_unreadable_manifest_fails_instead_of_running_the_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    _inspect(monkeypatch, 1, stderr="unauthorized")

    with pytest.raises(HarnessError, match="unauthorized"):
        base.resolve_digest("ghcr.io/vllm-project/guidellm:v0.7.3", "amd64")


def test_a_missing_architecture_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _inspect(monkeypatch, 0, _index())

    with pytest.raises(HarnessError, match="no linux/s390x variant"):
        base.resolve_digest("ghcr.io/vllm-project/guidellm@" + INDEX, "s390x")


def test_a_single_architecture_tag_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _inspect(monkeypatch, 0, json.dumps({"config": {}, "layers": []}))

    with pytest.raises(HarnessError, match="pin it by digest"):
        base.resolve_digest("example.invalid/tool:1.0", "amd64")


def test_every_registry_harness_is_pinned_by_a_digest_matching_its_version() -> None:
    for name in registry.names():
        harness = registry.get(name)
        if harness.build_context:
            continue
        assert harness.digest.startswith("sha256:"), f"{name} is not pinned by digest"
        tag = harness.image.rsplit(":", 1)[-1]
        assert tag.lstrip("v") == harness.pinned_version, f"{name}: tag {tag} is not {harness.pinned_version}"
        assert harness.reference == f"{base.repository(harness.image)}@{harness.digest}"


def test_a_missing_version_is_the_pinned_one_and_says_so() -> None:
    harness = registry.get("guidellm")

    version, notes = base.recorded_version(None, harness)

    assert version == harness.pinned_version
    assert notes and harness.digest in notes[0]
    assert base.recorded_version("0.7.4", harness) == ("0.7.4", [])
