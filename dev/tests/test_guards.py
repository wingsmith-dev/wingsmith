"""dev/guards.sh, run for real against a scratch git repository and stub CLIs on PATH."""

from __future__ import annotations

import io
import os
import tarfile
import subprocess

from pathlib import Path

import pytest


GUARDS = Path(__file__).resolve().parent.parent / "guards.sh"


def run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(GUARDS), *args],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def stub(bin_dir: Path, name: str, script: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + script)
    path.chmod(0o755)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    (root / ".gitignore").write_text("*.local.yaml\n")
    (root / "chart").mkdir()
    (root / "chart" / "values.yaml").write_text("a: 1\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    return root


@pytest.fixture
def path_with(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return bin_dir, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def test_clean_tree_passes_on_a_clean_checkout(repo: Path) -> None:
    assert run("clean-tree", cwd=repo).returncode == 0


def test_clean_tree_refuses_a_modified_file(repo: Path) -> None:
    (repo / "chart" / "values.yaml").write_text("a: 2\n")
    done = run("clean-tree", cwd=repo)
    assert done.returncode == 1
    assert "chart/values.yaml" in done.stderr


def test_clean_tree_refuses_an_untracked_file(repo: Path) -> None:
    (repo / "chart" / "new.yaml").write_text("b: 1\n")
    assert run("clean-tree", cwd=repo).returncode == 1


def test_clean_tree_ignored_flag_refuses_gitignored_secrets(repo: Path) -> None:
    (repo / "chart" / "values.prod.local.yaml").write_text("token: x\n")
    assert run("clean-tree", "chart", cwd=repo).returncode == 0
    done = run("clean-tree", "--ignored", "chart", cwd=repo)
    assert done.returncode == 1
    assert "values.prod.local.yaml" in done.stderr


def test_clean_tree_is_scoped_to_the_paths_given(repo: Path) -> None:
    (repo / "elsewhere.txt").write_text("x\n")
    assert run("clean-tree", "chart", cwd=repo).returncode == 0
    assert run("clean-tree", cwd=repo).returncode == 1


def head_context(repo: Path, path: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["bash", str(GUARDS), "head-context", path], cwd=repo, capture_output=True, check=False)


def test_head_context_is_the_committed_tree_and_nothing_on_disk_beside_it(repo: Path) -> None:
    (repo / "chart" / "values.prod.local.yaml").write_text("token: x\n")
    (repo / "chart" / "untracked.yaml").write_text("b: 1\n")
    (repo / "chart" / "values.yaml").write_text("a: edited\n")
    done = head_context(repo, "chart")
    assert done.returncode == 0, done.stderr
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        assert sorted(m.name for m in tar.getmembers() if m.isfile()) == ["values.yaml"]
        member = tar.extractfile("values.yaml")
        assert member is not None
        assert member.read() == b"a: 1\n"


def test_head_context_refuses_a_path_not_in_head(repo: Path) -> None:
    (repo / "fresh").mkdir()
    (repo / "fresh" / "Dockerfile").write_text("FROM scratch\n")
    done = head_context(repo, "fresh")
    assert done.returncode == 1
    assert b"not a directory in HEAD" in done.stderr
    assert done.stdout == b""


KUBECTL = """
case "$1 $2" in
  "config current-context") [ -n "${FAKE_CURRENT:-}" ] && echo "$FAKE_CURRENT" || exit 1 ;;
  "config get-contexts") [ "$3" = "${FAKE_KNOWN:-}" ] ;;
  *) exit 3 ;;
esac
"""


@pytest.mark.parametrize("current", ["kind-wingsmith", "kind-other"])
def test_e2e_context_accepts_a_kind_context(repo: Path, path_with, current: str) -> None:
    bin_dir, env = path_with
    stub(bin_dir, "kubectl", KUBECTL)
    done = run("e2e-context", cwd=repo, env={**env, "FAKE_CURRENT": current, "WINGSMITH_E2E_CONTEXT": ""})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == current


@pytest.mark.parametrize("current", ["prod-us-east", "gke_example_zone_cluster", "kindred"])
def test_e2e_context_refuses_anything_else(repo: Path, path_with, current: str) -> None:
    bin_dir, env = path_with
    stub(bin_dir, "kubectl", KUBECTL)
    done = run("e2e-context", cwd=repo, env={**env, "FAKE_CURRENT": current, "WINGSMITH_E2E_CONTEXT": ""})
    assert done.returncode == 1
    assert done.stdout == ""
    assert "WINGSMITH_E2E_CONTEXT" in done.stderr


def test_e2e_context_refuses_when_there_is_no_context(repo: Path, path_with) -> None:
    bin_dir, env = path_with
    stub(bin_dir, "kubectl", KUBECTL)
    done = run("e2e-context", cwd=repo, env={**env, "FAKE_CURRENT": "", "WINGSMITH_E2E_CONTEXT": ""})
    assert done.returncode == 1


def test_e2e_context_takes_an_explicit_context_over_the_current_one(repo: Path, path_with) -> None:
    bin_dir, env = path_with
    stub(bin_dir, "kubectl", KUBECTL)
    extra = {"FAKE_CURRENT": "prod", "FAKE_KNOWN": "orbstack", "WINGSMITH_E2E_CONTEXT": "orbstack"}
    done = run("e2e-context", cwd=repo, env={**env, **extra})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "orbstack"


def test_e2e_context_refuses_an_explicit_context_that_does_not_exist(repo: Path, path_with) -> None:
    bin_dir, env = path_with
    stub(bin_dir, "kubectl", KUBECTL)
    done = run("e2e-context", cwd=repo, env={**env, "FAKE_KNOWN": "", "WINGSMITH_E2E_CONTEXT": "typo"})
    assert done.returncode == 1


PROBES = [
    ("docker", ("image-unpublished", "ghcr.io/x/plane:abc1234")),
    ("helm", ("chart-unpublished", "oci://r/c", "0.2.0")),
]


@pytest.mark.parametrize(("tool", "args"), PROBES)
def test_unpublished_passes_when_the_registry_says_not_found(repo: Path, path_with, tool: str, args) -> None:
    bin_dir, env = path_with
    stub(bin_dir, tool, 'echo "Error: manifest unknown" >&2; exit 1\n')
    assert run(*args, cwd=repo, env=env).returncode == 0


@pytest.mark.parametrize(("tool", "args"), PROBES)
def test_unpublished_refuses_an_existing_tag(repo: Path, path_with, tool: str, args) -> None:
    bin_dir, env = path_with
    stub(bin_dir, tool, 'echo "{}"; exit 0\n')
    done = run(*args, cwd=repo, env=env)
    assert done.returncode == 1
    assert "already exists" in done.stderr


@pytest.mark.parametrize(("tool", "args"), PROBES)
def test_unpublished_refuses_when_it_cannot_tell(repo: Path, path_with, tool: str, args) -> None:
    bin_dir, env = path_with
    stub(bin_dir, tool, 'echo "unauthorized: authentication required" >&2; exit 1\n')
    done = run(*args, cwd=repo, env=env)
    assert done.returncode == 1
    assert "could not tell" in done.stderr
