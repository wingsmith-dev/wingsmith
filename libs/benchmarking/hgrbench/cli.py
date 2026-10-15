"""`hgrbench` — run a benchmark plan locally and render what it found.

Four verbs:

    hgrbench run <plan>        take the measurements
    hgrbench report [<run>]    render a suite run as one HTML page
    hgrbench harnesses         what this runner can drive, and what each is for
    hgrbench plans             the plans in this checkout

`run` is deliberately hard to point somewhere by accident: the endpoint, the
models, the load points and the ladders all come from a plan file that is committed
and reviewable, not from flags. What the flags do is narrow a plan (`--harness`,
`--model`), show it without running it (`--dry-run`), and unlock the one stage that
can hurt a live endpoint (`--hard`).
"""

from __future__ import annotations

import sys
import argparse
import tempfile

from pathlib import Path
from datetime import UTC
from datetime import datetime

from hgrbench import metrics
from hgrbench.plan import Plan
from hgrbench.plan import expand
from hgrbench.plan import load_plan
from hgrbench.plan import sweep_specs
from hgrbench.store import Store
from hgrbench.store import Suite
from hgrbench.store import latest_run
from hgrbench.store import plan_notes
from hgrbench.store import read_suite
from hgrbench.report import render
from hgrbench.runner import Runner
from hgrbench.runner import Options
from hgrbench.runner import refuse_invalid
from hgrbench.harnesses import registry


PLANS_DIR = Path(__file__).resolve().parent.parent / "plans"
DEFAULT_RESULTS = Path(".local/bench")


def plan_path(name: str) -> Path:
    """A plan by name from the committed set, or by path."""
    direct = Path(name)
    if direct.exists():
        return direct
    packaged = PLANS_DIR / f"{name}.toml"
    if packaged.exists():
        return packaged
    available = ", ".join(sorted(path.stem for path in PLANS_DIR.glob("*.toml")))
    raise SystemExit(f"no plan {name!r}: give a path, or one of {available}")


def run_id(plan: Plan, stamp: datetime) -> str:
    return f"{stamp.strftime('%Y%m%d-%H%M%S')}-{plan.name}"


def cmd_run(args: argparse.Namespace) -> int:
    plan = load_plan(plan_path(args.plan))
    refuse_invalid(plan)
    options = Options(
        hard=args.hard,
        dry_run=args.dry_run,
        only_harness=tuple(args.harness or ()),
        only_model=tuple(args.model or ()),
        timeout=args.timeout,
        hf_cache=Path(args.hf_cache).expanduser(),
    )
    for name in options.only_harness:
        registry.get(name)

    if args.dry_run:
        # Harnesses write their per-cell inputs as the commands are built, so a dry run
        # gets a scratch directory that is gone when it returns: nothing lands under
        # --results, and nothing is sent anywhere.
        with tempfile.TemporaryDirectory(prefix="hgrbench-dry-run-") as scratch:
            _run(plan, options, Path(scratch), args)
        return 0
    root = _run(plan, options, Path(args.results), args)
    report = render(read_suite(root), root / "report.html")
    print(f"\n{report}")
    if not args.no_open:
        _open(report)
    return 0


def _run(plan: Plan, options: Options, results: Path, args: argparse.Namespace) -> Path:
    stamp = datetime.now(UTC)
    root = results / run_id(plan, stamp)
    suite = Suite(
        run_id=root.name,
        plan_name=plan.name,
        plan_description=plan.description,
        plan_source=plan.source,
        started_at=stamp.isoformat(timespec="seconds"),
        endpoint=plan.endpoint.url,
        host={"arch": "", "runner": "hgrbench"},
        models=[model.name for model in plan.models],
        harnesses=list(plan.harnesses),
        planned_cells=len(expand(plan)),
        notes=plan_notes(plan),
    )
    store = Store(root, suite)
    runner = Runner(plan, store, options)
    suite.host["arch"] = runner.arch

    print(f"plan {plan.name}: {plan.description}")
    if options.dry_run:
        print("dry run: every command is printed, none is run, and nothing is kept")
    else:
        print(f"results in {root}")
    if plan.knee.enabled and not args.hard:
        print("the saturation search is in this plan but off for this run; add --hard to include it")

    runner.run()
    return root


def cmd_report(args: argparse.Namespace) -> int:
    target = Path(args.run) if args.run else latest_run(Path(args.results))
    if target is None:
        raise SystemExit(f"no suite runs under {args.results}")
    payload = read_suite(target)
    destination = Path(args.out) if args.out else (target if target.is_dir() else target.parent) / "report.html"
    report = render(payload, destination)
    print(report)
    if not args.no_open:
        _open(report)
    return 0


def cmd_harnesses(_args: argparse.Namespace) -> int:
    for name in registry.names():
        harness = registry.get(name)
        adapter = metrics.adapter_for(name, harness.pinned_version)
        fields = len(adapter.fields) if adapter else 0
        print(f"{name}")
        print(f"  {harness.description}")
        print(f"  image      {harness.image}")
        if harness.digest:
            print(f"  pinned at  {harness.digest}")
        if harness.build_context:
            print(f"  built from {harness.build_context}")
        print(f"  reads      {fields} canonical fields at {harness.pinned_version}")
        print(f"  ladders    {'yes, in one process' if harness.sweeps else 'no, one point per run'}")
        for note in harness.notes:
            print(f"  · {note}")
        print()
    return 0


def cmd_plans(args: argparse.Namespace) -> int:
    for path in sorted(PLANS_DIR.glob("*.toml")):
        plan = load_plan(path)
        cells = expand(plan)
        ladders = sweep_specs(plan)
        requests = sum(cell.load.requests for cell in cells)
        print(f"{plan.name}  ({path.name})")
        print(f"  {plan.description}")
        print(f"  {len(cells)} cells · {len(ladders)} ladder runs · ~{requests} requests in the matrix")
        print(f"  models     {', '.join(model.name for model in plan.models)}")
        print(f"  harnesses  {', '.join(plan.harnesses)}")
        if plan.sweep.enabled:
            print(f"  ladder     {', '.join(f'c{rung}' for rung in plan.sweep.ladder)}")
        if plan.knee.enabled:
            print(f"  knee       {plan.knee.field} <= {plan.knee.limit:g}, needs --hard")
        print()
    return 0


def _open(path: Path) -> None:
    import subprocess

    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hgrbench", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="take the measurements a plan describes")
    run.add_argument("plan", help="a plan name from libs/benchmarking/plans, or a path")
    run.add_argument("--results", default=str(DEFAULT_RESULTS), help="where suite runs are kept")
    run.add_argument("--harness", action="append", help="only this harness; repeatable")
    run.add_argument("--model", action="append", help="only this model; repeatable")
    run.add_argument(
        "--hard",
        action="store_true",
        help="include the saturation search, which can degrade a live endpoint",
    )
    run.add_argument("--dry-run", action="store_true", help="print every command without running any")
    run.add_argument("--timeout", type=int, default=1800, help="per-measurement ceiling, in seconds")
    run.add_argument("--hf-cache", default="~/.cache/huggingface", help="tokenizer cache to mount")
    run.add_argument("--no-open", action="store_true", help="do not open the report when done")
    run.set_defaults(func=cmd_run)

    report = subparsers.add_parser("report", help="render a suite run as one HTML page")
    report.add_argument("run", nargs="?", help="a suite directory or its suite.json; the latest by default")
    report.add_argument("--results", default=str(DEFAULT_RESULTS))
    report.add_argument("--out", help="where to write the page")
    report.add_argument("--no-open", action="store_true")
    report.set_defaults(func=cmd_report)

    harnesses = subparsers.add_parser("harnesses", help="what this runner can drive")
    harnesses.set_defaults(func=cmd_harnesses)

    plans = subparsers.add_parser("plans", help="the plans in this checkout")
    plans.set_defaults(func=cmd_plans)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
