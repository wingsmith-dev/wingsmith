"""Offline checks: the committed plans load, and no harness puts the token on a command line."""

from __future__ import annotations

from pathlib import Path

import pytest

from hgrbench import cli
from hgrbench.plan import expand
from hgrbench.plan import load_plan
from hgrbench.plan import sweep_specs
from hgrbench.harnesses import registry
from hgrbench.harnesses.base import TOKEN_ENV
from hgrbench.harnesses.base import Context


TOKEN = "hgr-bench-test-token-value"
PLANS = sorted(cli.PLANS_DIR.glob("*.toml"))


def context(tmp_path: Path, token: str = TOKEN) -> Context:
    (tmp_path / "out").mkdir(exist_ok=True)
    return Context(
        endpoint_url="http://host.docker.internal:8080",
        token=token,
        hf_cache=tmp_path / "hf",
        results_dir=tmp_path / "out",
        image="example.invalid/harness@sha256:" + "0" * 64,
    )


@pytest.mark.parametrize("path", PLANS, ids=[path.stem for path in PLANS])
def test_committed_plan_loads_and_expands(path: Path) -> None:
    plan = load_plan(path)
    assert expand(plan), f"{path.name} expands to no cells"
    for name in plan.harnesses:
        registry.get(name)
    sweep_specs(plan)


@pytest.mark.parametrize("path", PLANS, ids=[path.stem for path in PLANS])
def test_no_harness_puts_the_token_on_argv(path: Path, tmp_path: Path) -> None:
    plan = load_plan(path)
    ctx = context(tmp_path)
    invocations = [registry.get(cell.harness).command(cell, ctx) for cell in expand(plan)]
    for spec in sweep_specs(plan):
        harness = registry.get(spec.harness)
        assert harness.sweep_command is not None
        invocations.append(harness.sweep_command(ctx, spec))

    assert invocations
    for invocation in invocations:
        assert not any(TOKEN in part for part in invocation.argv), invocation.argv
        assert TOKEN not in invocation.display
        assert invocation.env == {TOKEN_ENV: TOKEN}
        pairs = list(zip(invocation.argv, invocation.argv[1:], strict=False))
        assert ("-e", TOKEN_ENV) in pairs, "the token variable is not passed through by name"


def test_no_token_means_no_token_variable(tmp_path: Path) -> None:
    plan = load_plan(PLANS[0])
    ctx = context(tmp_path, token="")
    invocation = registry.get(plan.harnesses[0]).command(expand(plan)[0], ctx)
    assert invocation.env == {}
    assert TOKEN_ENV not in invocation.argv


def test_plans_command_lists_every_committed_plan(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["plans"]) == 0
    out = capsys.readouterr().out
    for path in PLANS:
        assert load_plan(path).name in out
