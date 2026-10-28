"""dev/bench_plan.py: the plan `just bench` writes is one AIPerf cell wingsbench can load."""

from __future__ import annotations

import sys

from pathlib import Path

import pytest
import bench_plan


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "libs" / "benchmarking"))

from wingsbench.plan import expand
from wingsbench.plan import load_plan
from wingsbench.plan import sweep_specs


def written(tmp_path: Path, env: dict[str, str]):
    path = tmp_path / "bench.toml"
    path.write_text(bench_plan.plan(env))
    return load_plan(path)


def test_a_model_is_required() -> None:
    with pytest.raises(SystemExit, match="BENCH_MODEL"):
        bench_plan.plan({})


def test_defaults_are_one_aiperf_cell_against_a_local_port(tmp_path: Path) -> None:
    plan = written(tmp_path, {"BENCH_MODEL": "org/model"})
    cells = expand(plan)
    assert len(cells) == 1
    cell = cells[0]
    assert cell.harness == "aiperf"
    assert cell.model.name == cell.model.tokenizer == "org/model"
    assert (cell.load.concurrency, cell.load.requests) == (10, 100)
    assert (cell.input_tokens, cell.output_tokens) == (2000, 256)
    assert plan.endpoint.url == "http://host.docker.internal:8080"
    assert plan.endpoint.token_env == "BENCH_TOKEN"
    assert sweep_specs(plan) == []
    assert not plan.knee.enabled


def test_variables_override_every_default(tmp_path: Path) -> None:
    env = {
        "BENCH_MODEL": "served-name",
        "BENCH_TOKENIZER": "org/repo",
        "BENCH_URL": "http://host.docker.internal:9000",
        "BENCH_CONCURRENCY": "32",
        "BENCH_REQUESTS": "500",
        "BENCH_ISL": "4000",
        "BENCH_OSL": "64",
    }
    plan = written(tmp_path, env)
    cell = expand(plan)[0]
    assert (cell.model.name, cell.model.tokenizer) == ("served-name", "org/repo")
    assert (cell.load.concurrency, cell.load.requests) == (32, 500)
    assert (cell.input_tokens, cell.output_tokens) == (4000, 64)
    assert plan.endpoint.url == env["BENCH_URL"]


def test_values_are_quoted_not_spliced(tmp_path: Path) -> None:
    model = 'odd"name\\with\nbreaks'
    assert expand(written(tmp_path, {"BENCH_MODEL": model}))[0].model.name == model


def test_the_token_is_never_written(tmp_path: Path) -> None:
    text = bench_plan.plan({"BENCH_MODEL": "m", "BENCH_TOKEN": "secret-value"})
    assert "secret-value" not in text


@pytest.mark.parametrize("value", ["0", "-1", "ten"])
def test_counts_must_be_positive_whole_numbers(value: str) -> None:
    with pytest.raises(SystemExit, match="BENCH_CONCURRENCY"):
        bench_plan.plan({"BENCH_MODEL": "m", "BENCH_CONCURRENCY": value})
