"""Render tests for the cluster-setup chart.

Run with:

    uv run --no-project --with pyyaml --with pytest pytest libs/chart/cluster-setup/tests

Each test renders the chart offline with `helm template` and asserts on the
objects it produces, or on the refusal it prints. The subcharts are not vendored,
so the chart is copied and its `dependencies:` stripped first, the same way
`just chart-lint` renders it; nothing the subcharts own is asserted here.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tarfile

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[1]

# The blanks an operator must fill. Every render supplies them unless a test is
# about one of them.
REQUIRED = {
    "cluster.id": "lint",
    "cluster.hubUrl": "wss://plane.example.com/v1/hub/sync",
    "enrolmentToken.value": "test-enrolment-token",
    "pools.labelKey": "example.com/pool",
    "pools.gpuValue": "gpu",
    "pools.cacheValue": "system",
    "modelCache.storageClass": "rwx",
}


@pytest.fixture(scope="session")
def chart(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    work = tmp_path_factory.mktemp("chart") / "cluster-setup"
    shutil.copytree(CHART, work, ignore=shutil.ignore_patterns("tests", "charts", "__pycache__"))
    meta = work / "Chart.yaml"
    meta.write_text(meta.read_text().split("dependencies:")[0])
    (work / "Chart.lock").unlink(missing_ok=True)
    return work


def _args(values: dict[str, object], required: bool) -> list[str]:
    merged: dict[str, object] = dict(REQUIRED) if required else {}
    merged.update(values)
    args: list[str] = []
    for key, value in merged.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        args += ["--set", f"{key}={value}"]
    return args


def helm(chart: pathlib.Path, values: dict[str, object], required: bool = True,
         namespace: str = "hangar-system") -> subprocess.CompletedProcess[str]:
    cmd = ["helm", "template", "rel", str(chart), "-n", namespace, *_args(values, required)]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def render(chart: pathlib.Path, **values: object) -> list[dict]:
    result = helm(chart, values)
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def refusal(chart: pathlib.Path, **values: object) -> str:
    result = helm(chart, values)
    assert result.returncode != 0, f"rendered, but should have refused: {values}"
    return result.stderr


def find(docs: list[dict], kind: str, name: str, namespace: str | None = None) -> dict | None:
    for d in docs:
        meta = d.get("metadata", {})
        if d.get("kind") == kind and meta.get("name") == name:
            if namespace is None or meta.get("namespace") == namespace:
                return d
    return None


def get(docs: list[dict], kind: str, name: str, namespace: str | None = None) -> dict:
    found = find(docs, kind, name, namespace)
    assert found is not None, f"no {kind} {name} rendered"
    return found


def container(deployment: dict, name: str) -> dict:
    for c in deployment["spec"]["template"]["spec"]["containers"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"no container {name}")


def env(c: dict) -> dict[str, object]:
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in c.get("env", [])}


# ── packaging ────────────────────────────────────────────────────────────────


def test_package_keeps_vendored_subcharts_and_drops_stray_archives(chart, tmp_path):
    """`helm dependency build` leaves subcharts as charts/*.tgz. Ignoring every
    .tgz dropped them too, so a package could not be built from a vendored chart."""
    sub = tmp_path / "sub"
    subprocess.run(["helm", "create", str(sub)], check=True, capture_output=True)
    subprocess.run(["helm", "package", str(sub), "-d", str(chart / "charts")], check=True,
                   capture_output=True)
    (chart / "stray.tgz").write_bytes(b"")
    try:
        out = tmp_path / "out"
        subprocess.run(["helm", "package", str(chart), "-d", str(out)], check=True,
                       capture_output=True)
        (package,) = out.glob("*.tgz")
        names = tarfile.open(package).getnames()
        assert "cluster-setup/charts/sub/Chart.yaml" in names
        assert not any(n.endswith("stray.tgz") for n in names)
    finally:
        (chart / "stray.tgz").unlink()
        shutil.rmtree(chart / "charts")
