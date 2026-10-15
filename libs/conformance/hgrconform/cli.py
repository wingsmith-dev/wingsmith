"""`hgrconform` — ask an endpoint what it does, and whether that matches the one it claims to be.

Four verbs:

    hgrconform run <profile> [--reference <profile>]   ask the questions
    hgrconform diff [<run>]                            compare a run's two endpoints
    hgrconform probes                                  what this knows how to ask
    hgrconform show <run> <probe>                      one probe's evidence, verbatim

`run` takes a committed profile rather than flags, for the same reason the benchmark
runner takes a plan: what was checked, and against what, should be reviewable in a
diff rather than recoverable from somebody's shell history.
"""

from __future__ import annotations

import json
import argparse

from pathlib import Path
from datetime import UTC
from datetime import datetime
from importlib import resources
from dataclasses import field
from dataclasses import dataclass

from hgrconform.facts import Fact
from hgrconform.facts import Difference
from hgrconform.facts import compare
from hgrconform.store import Run
from hgrconform.store import Store
from hgrconform.store import read_run
from hgrconform.store import latest_run
from hgrconform.probes import registry
from hgrconform.runner import run_probes
from hgrconform.profile import load_profile


# Shipped inside the package, so a profile named by its stem resolves from an install too.
PROFILES_DIR = Path(resources.files("hgrconform") / "profiles")
DEFAULT_RESULTS = Path(".local/conformance")
TARGET = "target"
REFERENCE = "reference"


def profile_path(name: str) -> Path:
    direct = Path(name)
    if direct.exists():
        return direct
    packaged = PROFILES_DIR / f"{name}.toml"
    if packaged.exists():
        return packaged
    available = ", ".join(sorted(path.stem for path in PROFILES_DIR.glob("*.toml"))) or "none committed yet"
    raise SystemExit(f"no profile {name!r}: give a path, or one of {available}")


def cmd_run(args: argparse.Namespace) -> int:
    registry.load()
    target = load_profile(profile_path(args.profile))
    reference = load_profile(profile_path(args.reference)) if args.reference else None

    probes = registry.select(only=tuple(args.probe or ()), skip_heavy=not args.heavy)
    if not probes:
        raise SystemExit("no probes selected")

    for role, profile in (("target", target), ("reference", reference)):
        if profile and not profile.token:
            print(f"warning: {role} profile {profile.name} has no token in ${profile.token_env}")

    stamp = datetime.now(UTC)
    root = Path(args.results) / f"{stamp.strftime('%Y%m%d-%H%M%S')}-{target.name}"
    store = Store(root, Run(run_id=root.name, started_at=stamp.isoformat(), probe_ids=[p.id for p in probes]))

    # With a reference, each endpoint is asked about everything either one claims, so a
    # capability one profile leaves out is observed rather than skipped on that side.
    claimed: tuple[str, ...] = ()
    if reference:
        claimed = (*target.modalities, *target.protocols, *reference.modalities, *reference.protocols)

    print(f"{target.name} -> {target.base_url} ({len(probes)} probes)")
    run_probes(probes, target, TARGET, store, ask_anyway=claimed)
    if reference:
        print(f"{reference.name} -> {reference.base_url}")
        run_probes(probes, reference, REFERENCE, store, ask_anyway=claimed)

    store.run.finished_at = datetime.now(UTC).isoformat()
    store.flush()
    print(f"\n{root}")

    if reference:
        return _diff(store.run, args.verbose)
    _summarise(store.run)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    registry.load()
    path = Path(args.run) if args.run else latest_run(Path(args.results))
    if path is None:
        raise SystemExit(f"no runs under {args.results}")
    return _diff(read_run(path), args.verbose)


@dataclass
class Comparison:
    """What diffing a run's two endpoints found, before anything is printed."""

    differences: list[Difference] = field(default_factory=list)
    uncompared: list[tuple[registry.Probe, Fact]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def compare_run(run: Run) -> Comparison:
    """Every key on which the two endpoints disagreed, plus what could not be compared.

    A probe with a result on one side only is a `not-run` difference, never a silent
    gap: an interrupted run, or a reference that errored before reaching a probe, must
    not read as agreement. A probe this version no longer defines is listed by id
    rather than dropped, so an old run read by a newer package still accounts for it.
    """
    if REFERENCE not in run.endpoints:
        raise ValueError("this run has no reference endpoint; re-run with --reference")

    target = run.endpoints[TARGET]
    reference = run.endpoints[REFERENCE]
    result = Comparison()
    for probe_id in run.probe_ids:
        probe = registry.find(probe_id)
        here = target.facts.get(probe_id)
        there = reference.facts.get(probe_id)
        if probe is None:
            result.unknown.append(probe_id)
            continue
        if here is None or there is None:
            result.differences.append(
                Difference(
                    probe=probe_id,
                    key="",
                    target=here.status if here else "absent",
                    reference=there.status if there else "absent",
                    kind="not-run",
                )
            )
            continue
        if not probe.compare:
            if here.usable:
                result.uncompared.append((probe, here))
            continue
        if here.status == "skipped" and there.status == "skipped":
            continue
        result.differences.extend(compare(here, there, probe.compare))
    return result


# This exit code is read only by a person today. See "Running it unattended" in the
# README before wiring it to a schedule or a deploy gate.
def _diff(run: Run, verbose: bool) -> int:
    """Report every key on which the two endpoints disagreed, and return 1 if any did."""
    try:
        result = compare_run(run)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"\n{run.endpoints[TARGET].profile_name} vs {run.endpoints[REFERENCE].profile_name}")
    _print_uncompared(result.uncompared)
    if result.unknown:
        print("not compared, unknown to this version of hgrconform: " + ", ".join(result.unknown) + "\n")
    differences = result.differences
    if not differences:
        print("no differences on the compared keys")
        return 0

    print(f"{len(differences)} difference(s)\n")
    for difference in differences:
        print(f"  {difference.label}")
        print(f"    target     {json.dumps(difference.target, ensure_ascii=False, default=str)}")
        print(f"    reference  {json.dumps(difference.reference, ensure_ascii=False, default=str)}")
        if verbose:
            print(f"    kind       {difference.kind}")
    return 1


def _print_uncompared(uncompared: list[tuple[registry.Probe, Fact]]) -> None:
    """Show the probes that have no reference to diff against, so they are not invisible.

    A probe declares an empty `compare` when the reference endpoint cannot answer its
    question — `errors.leakage` asks whether our own hostnames appear in an error
    body, and the original provider will never leak our hostname. Those observations
    are absolute rather than relative, and dropping them out of the diff without
    saying so would hide the one check here that needs no reference at all.
    """
    if not uncompared:
        return
    print("not compared against the reference, observed directly:\n")
    for probe, fact in uncompared:
        print(f"  {probe.id}")
        for key, value in sorted(fact.observed.items()):
            print(f"    {key:<26} {json.dumps(value, ensure_ascii=False, default=str)}")
    print()


def _summarise(run: Run) -> None:
    """Print what a single-endpoint run saw, since there is nothing to diff it with."""
    endpoint = run.endpoints[TARGET]
    print(f"\n{endpoint.profile_name} — no reference endpoint, so these are observations, not verdicts\n")
    for probe_id in run.probe_ids:
        fact = endpoint.facts.get(probe_id)
        if fact is None:
            continue
        print(f"  {probe_id}  [{fact.status}]")
        for key, value in sorted(fact.observed.items()):
            print(f"    {key:<28} {json.dumps(value, ensure_ascii=False, default=str)}")
        if fact.error:
            print(f"    {'error':<28} {fact.error.splitlines()[0]}")
        print()


def cmd_probes(args: argparse.Namespace) -> int:
    registry.load()
    for probe in registry.all_probes():
        heavy = "  [heavy]" if probe.heavy else ""
        print(f"{probe.id}{heavy}")
        print(f"  {probe.title}")
        print(f"  answers: {probe.requirement}")
        print(f"  compares: {', '.join(probe.compare) or '(not compared against a reference)'}")
        print()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path = Path(args.run) if args.run else latest_run(Path(args.results))
    if path is None:
        raise SystemExit(f"no runs under {args.results}")
    run = read_run(path)
    for role, endpoint in run.endpoints.items():
        fact: Fact | None = endpoint.facts.get(args.probe)
        if fact is None:
            continue
        print(f"=== {role}: {endpoint.profile_name}")
        print(json.dumps(fact.observed, indent=2, ensure_ascii=False, default=str))
        for name in fact.exchanges:
            print(f"--- {role}/{name}")
            print((path / role / name).read_text())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hgrconform", description=__doc__.splitlines()[0])
    parser.add_argument("--results", default=str(DEFAULT_RESULTS), help="where runs are written")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="ask the probes of an endpoint")
    run.add_argument("profile")
    run.add_argument("--reference", help="a second profile to diff against")
    run.add_argument("--probe", action="append", help="only this probe or group; repeatable")
    run.add_argument("--heavy", action="store_true", help="include probes that send hundreds of requests")
    run.add_argument("--verbose", action="store_true")
    run.set_defaults(func=cmd_run)

    diff = sub.add_parser("diff", help="compare a stored run's two endpoints")
    diff.add_argument("run", nargs="?")
    diff.add_argument("--verbose", action="store_true")
    diff.set_defaults(func=cmd_diff)

    probes = sub.add_parser("probes", help="what this knows how to ask")
    probes.set_defaults(func=cmd_probes)

    show = sub.add_parser("show", help="one probe's evidence, verbatim")
    show.add_argument("probe")
    show.add_argument("run", nargs="?")
    show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return int(args.func(args))
