"""The runner's own behaviour, with every container replaced by a local process."""

from __future__ import annotations

import sys
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
from hgrbench.harnesses.base import Invocation


TOKEN = "tok-123"


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
