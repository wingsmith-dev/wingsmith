"""The runner's own behaviour, with every container replaced by a local process."""

from __future__ import annotations

import sys
import json
import subprocess

from pathlib import Path

import pytest

from hgrbench import runner as runner_module
from hgrbench.cli import PLANS_DIR
from hgrbench.plan import load_plan
from hgrbench.store import Store
from hgrbench.store import Suite
from hgrbench.runner import Runner
from hgrbench.runner import Options
from hgrbench.harnesses.base import Emission
from hgrbench.harnesses.base import Invocation
from hgrbench.harnesses.base import redacted


TOKEN = "hgr-bench-runner-token"


def _runner(tmp_path: Path, **options: object) -> Runner:
    plan = load_plan(PLANS_DIR / "smoke.toml")
    suite = Suite(
        run_id="test",
        plan_name=plan.name,
        plan_description="",
        plan_source="",
        started_at="",
        endpoint=plan.endpoint.url,
        host={},
        models=[],
        harnesses=[],
        planned_cells=0,
    )
    runner = Runner(plan, Store(tmp_path / "run", suite), Options(quiet=True, **options))
    runner.token = TOKEN
    return runner


def _python(code: str, container: str = "") -> Invocation:
    argv = [sys.executable, "-c", code]
    return Invocation(argv=argv, display="python -c ...", container=container)


def test_the_log_is_written_redacted(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    log = tmp_path / "harness.log"

    returncode, _ = runner.execute(_python(f"print('Bearer {TOKEN}'); print('done')"), log, timeout=30)

    assert returncode == 0
    text = log.read_text()
    assert TOKEN not in text
    assert "Bearer $HGR_BENCH_TOKEN" in text
    assert "done" in text


def test_a_timed_out_run_kills_its_container_and_keeps_its_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner(tmp_path)
    log = tmp_path / "harness.log"
    killed: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["docker", "kill"]:
            killed.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    code = f"import time; print('started {TOKEN}', flush=True); time.sleep(30)"

    with pytest.raises(subprocess.TimeoutExpired):
        runner.execute(_python(code, container="hgrbench-test"), log, timeout=1)

    assert killed == [["docker", "kill", "hgrbench-test"]]
    text = log.read_text()
    assert "started $HGR_BENCH_TOKEN" in text
    assert "timed out after 1s" in text


def _plan_text(**overrides: str) -> str:
    sections = {
        "head": 'name = "t"\nharnesses = ["aiperf"]\nrepeats = 1\n',
        "endpoint": '[endpoint]\nurl = "http://host.docker.internal:8080"\n',
        "models": '[[models]]\nname = "m"\ntokenizer = "org/m"\n',
        "load": "[[load]]\nconcurrency = 2\nrequests = 4\n",
        "knee": "[knee]\nenabled = false\n",
    }
    sections.update(overrides)
    return "\n".join(sections.values())


def _problems(tmp_path: Path, **overrides: str) -> list[str]:
    path = tmp_path / "plan.toml"
    path.write_text(_plan_text(**overrides))
    return runner_module.plan_problems(load_plan(path))


def test_a_valid_plan_has_no_problems(tmp_path: Path) -> None:
    assert _problems(tmp_path) == []


@pytest.mark.parametrize("path", sorted(PLANS_DIR.glob("*.toml")), ids=lambda p: p.stem)
def test_committed_plans_are_valid(path: Path) -> None:
    assert runner_module.plan_problems(load_plan(path)) == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"head": 'name = "t"\nharnesses = ["nope"]\n'}, "unknown harness 'nope'"),
        ({"head": 'name = "t"\nharnesses = ["aiperf"]\nrepeats = 0\n'}, "repeats is 0"),
        ({"models": '[[models]]\nname = "m"\n'}, "m has no tokenizer"),
        ({"load": "[[load]]\nconcurrency = 2\nseconds = 30\n"}, "aiperf counts requests, not seconds"),
        ({"load": "[[load]]\nconcurrency = 0\nrequests = 4\n"}, "concurrency must be at least 1"),
        (
            {"knee": '[knee]\nenabled = true\nfield = "ttft.p95"\nrequests_per_rung = 4\n'},
            "knee field 'ttft.p95' is not a metric id",
        ),
        (
            {"knee": '[sweep]\nenabled = true\nharnesses = ["aiperf"]\n'},
            "aiperf is in [sweep] and cannot walk a ladder",
        ),
    ],
)
def test_a_plan_is_refused_before_the_first_cell(tmp_path: Path, overrides: dict[str, str], expected: str) -> None:
    assert any(expected in problem for problem in _problems(tmp_path, **overrides))


def test_the_cli_refuses_an_invalid_plan_without_creating_a_run(tmp_path: Path) -> None:
    from hgrbench import cli

    path = tmp_path / "plan.toml"
    path.write_text(_plan_text(models='[[models]]\nname = "m"\n'))
    results = tmp_path / "results"

    assert cli.main(["run", str(path), "--results", str(results), "--no-open"]) == 1
    assert not results.exists()


def test_points_of_one_run_reporting_the_same_load_get_distinct_ids(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    cell = runner_module.expand(runner.plan)[0]
    first = Emission(summary={}, version="0.7.3", load={"kind": "concurrency", "concurrency": 4})
    second = Emission(summary={}, version="0.7.3", load={"kind": "concurrency", "concurrency": 4})

    taken: set[str] = set()
    one = runner.relabel(cell, first, taken)
    taken.add(one.id)
    two = runner.relabel(cell, second, taken)

    assert one.load.concurrency == two.load.concurrency == 4
    assert one.id != two.id


def test_the_suite_file_is_replaced_whole(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    store = runner.store

    store.flush()

    assert json.loads(store.suite_path.read_text())["run_id"] == "test"
    assert [path.name for path in store.root.iterdir()] == ["suite.json"]


def test_a_dry_run_runs_nothing_sends_nothing_and_keeps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hgrbench import cli
    from hgrbench.harnesses import base

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a dry run started a process or a request: {args}")

    monkeypatch.setattr(runner_module, "host_arch", lambda: "amd64")
    monkeypatch.setattr(runner_module.subprocess, "run", refuse)
    monkeypatch.setattr(runner_module.subprocess, "Popen", refuse)
    monkeypatch.setattr(base.subprocess, "run", refuse)
    monkeypatch.setattr(runner_module.urlrequest, "urlopen", refuse)
    monkeypatch.setenv("HANGAR_API_KEY", TOKEN)
    results = tmp_path / "results"

    assert cli.main(["run", "deep", "--hard", "--dry-run", "--results", str(results)]) == 0

    out = capsys.readouterr().out
    assert "would run" in out
    assert TOKEN not in out
    assert not results.exists()


def test_a_token_too_short_to_be_a_credential_is_not_scrubbed(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner.token = "x"
    log = tmp_path / "harness.log"

    returncode, _ = runner.execute(_python("print('/results/x/max')"), log, timeout=30)

    assert returncode == 0
    assert "/results/x/max" in log.read_text()
    assert redacted(["--dir", "/results/x"], "x") == "--dir /results/x"
