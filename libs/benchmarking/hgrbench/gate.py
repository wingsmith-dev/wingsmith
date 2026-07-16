"""Refuse a run that cannot answer anything, and judge a finished one by machine.

Three gates, and the reason there are three is that the failures they catch happen at
three different moments.

`pre` runs before load is sent. It is about comparability: a run whose fleet is halfway
through a rollout, whose shape cannot reach full session depth, or which differs from its
comparator in more than the one declared variable, cannot produce a number worth the wall
clock it costs.

`post` runs after, from the artifacts alone, and `void` is a verdict it can reach on its
own. It reads the run's own start and end times, pins the telemetry window to them, and
asks whether what happened inside that window was the workload or something else.

`compare` runs when a claim is made, and it is the one that has teeth. A gate that only
guards spending gets skipped at three in the morning by anybody in a hurry; the moment
that actually needs guarding is the one where two numbers become a sentence in a
document. So `compare` refuses to print a difference unless both runs are valid, their
shapes match, and the difference exceeds a noise floor that was *measured on this
deployment* rather than assumed.

Every threshold comes from a profile. There are no numbers in this module, deliberately
and permanently -- see `hgrbench.profile`. A check whose threshold is unavailable returns
UNKNOWN, which is a third outcome and not a pass: the most common way a gate lies is by
skipping a check it could not run and reporting the silence as agreement.
"""

from __future__ import annotations

import json
import glob
import os

from typing import Any
from pathlib import Path
from dataclasses import field
from dataclasses import dataclass

from hgrbench.fleet import adapters
from hgrbench.fleet.scrape import Dump
from hgrbench.profile import DeploymentProfile
from hgrbench.profile import WorkloadProfile
from hgrbench.profile import CHECK_PREFIX_CAPACITY
from hgrbench.profile import CHECK_SESSION_DEPTH
from hgrbench.profile import CHECK_SLO


PASS = "pass"
FAIL = "fail"
WARN = "warn"
UNKNOWN = "unknown"

VALID = "valid"
CAVEATS = "valid-with-caveats"
UNVERIFIED = "unverified"
VOID = "void"

_MARK = {PASS: "ok  ", FAIL: "FAIL", WARN: "warn", UNKNOWN: "????"}


VALIDITY = "validity"
TARGET = "target"


@dataclass
class Check:
    """`kind` separates two questions that must not be summed.

    A validity check asks whether the run measured what it claims to have measured. A
    target check asks whether the result meets the service level the traffic is sold
    against. A configuration can be perfectly valid and miss its target -- that is a
    finding, not a defect in the measurement -- and folding the two together would void
    every honest run of an under-provisioned fleet.
    """

    status: str
    name: str
    detail: str = ""
    kind: str = VALIDITY


@dataclass
class Report:
    title: str
    checks: list[Check] = field(default_factory=list)

    def add(self, status: str, name: str, detail: str = "", kind: str = VALIDITY) -> str:
        self.checks.append(Check(status, name, detail, kind))
        return status

    def judge(self, ok: bool | None, name: str, detail: str = "", soft: bool = False,
              kind: str = VALIDITY) -> str:
        """`None` means the check could not be run, which is never a pass."""
        if ok is None:
            return self.add(UNKNOWN, name, detail, kind)
        return self.add(PASS if ok else (WARN if soft else FAIL), name, detail, kind)

    @property
    def verdict(self) -> str:
        s = {c.status for c in self.checks if c.kind == VALIDITY}
        if FAIL in s:
            return VOID
        if UNKNOWN in s:
            return UNVERIFIED
        if WARN in s:
            return CAVEATS
        return VALID

    @property
    def meets_target(self) -> str | None:
        s = {c.status for c in self.checks if c.kind == TARGET}
        if not s:
            return None
        if FAIL in s:
            return "misses SLO"
        if UNKNOWN in s:
            return "SLO not checked"
        return "meets SLO"

    def render(self) -> str:
        lines = [self.title]
        for c in self.checks:
            lines.append(f"  {_MARK[c.status]} {c.name:<38} {c.detail}")
        tail = self.verdict
        if self.meets_target:
            tail += f", {self.meets_target}"
        lines.append(f"  => {tail}")
        return "\n".join(lines)


@dataclass
class Run:
    """One benchmark's artifacts, from whichever harness produced them.

    `client` is the load generator's own summary, verbatim. `invocation` is what the
    harness was actually told -- the only statement of the shape that a later edit to a
    script cannot contradict, which is why the checks read it rather than the plan.
    """

    label: str
    client: dict[str, Any]
    invocation: str = ""
    dump: Dump | None = None

    def num(self, *path: str) -> float | None:
        cur: Any = self.client
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return None
            cur = cur[p]
        return float(cur) if isinstance(cur, (int, float)) else None

    @property
    def start(self) -> str | None:
        return self.client.get("start_time")

    @property
    def end(self) -> str | None:
        return self.client.get("end_time")


def load_run(data_dir: str | Path, label: str, dump_dir: str | Path | None = None) -> Run:
    """Every artifact of one run, matched exactly rather than by prefix.

    Prefix matching is how `a4.14.1` silently absorbs a future `a4.14.10` and reports two
    runs under one verdict, so the label must be followed by a separator.
    """
    data = Path(data_dir)
    steps = [p for p in sorted(glob.glob(str(data / f"{label}*step*.json")))
             if os.path.basename(p)[len(label)] in "-_."]
    client = json.loads(Path(steps[0]).read_text()) if steps else {}
    dump = None
    if dump_dir and os.path.isdir(dump_dir):
        dump = Dump(dump_dir)
    return Run(label=label, client=client,
               invocation=(client.get("run_info") or {}).get("cli_command", ""), dump=dump)


def _shape_from(invocation: str) -> dict[str, int]:
    import re
    out: dict[str, int] = {}
    for k, v in re.findall(r"--([a-z-]+)[= ](\d+)", invocation):
        out[k] = int(v)
    return out


def post(run: Run, workload: WorkloadProfile,
         deployment: DeploymentProfile | None = None) -> Report:
    """Judge a finished run from its artifacts."""
    r = Report(f"post {run.label}  [{workload.name}]")

    if not run.client:
        r.add(UNKNOWN, "client summary", "no harness summary on disk; nothing to check "
                                         "it against and nothing to compare later")
    shape = _shape_from(run.invocation)
    asked = shape.get("request-count")
    got = run.num("request_count", "avg")
    r.judge(None if (asked is None or got is None) else got >= 0.99 * asked,
            "fired what it was told",
            f"{got} of {asked}" if asked else "no request count in the invocation")

    if workload.applies(CHECK_SESSION_DEPTH):
        conv = shape.get("num-conversations")
        turns = shape.get("conversation-turn-mean")
        if conv is None or turns is None:
            r.add(UNKNOWN, "sessions ran to full depth",
                  "the invocation names no session shape, so this run cannot be shown to "
                  "have run the workload this profile describes")
        else:
            r.judge(asked == conv * turns, "sessions ran to full depth",
                    f"{asked} requests for {conv}x{turns}={conv * turns}")
            r.judge(turns == workload.shape.turns, "depth matches the profile",
                    f"{turns} turns"
                    + ("" if turns == workload.shape.turns else
                       f" against the profile's {workload.shape.turns}; depth moves "
                       f"throughput on its own, so this is a different workload"))
            want = shape.get("concurrency")
            r.judge(None if want is None else want <= conv,
                    "concurrency under the session ceiling",
                    f"asked {want} with {conv} sessions"
                    + ("" if (want or 0) <= (conv or 0) else
                       "; turns are sequential, so this measured the ceiling and not "
                       "the configuration"))

    eff = run.num("effective_concurrency", "avg")
    want = shape.get("concurrency")
    conv = shape.get("num-conversations")
    reach = min(x for x in (want, conv) if x) if (want or conv) else None
    r.judge(None if (eff is None or reach is None) else eff >= 0.9 * reach,
            "concurrency was reached", f"{eff} of {reach}" if eff else "not reported")

    # Three sources, in descending directness, because harnesses disagree about which
    # they publish and a check that only knows one of them is UNKNOWN on most of the
    # archive. Falling back is not skipping: each fallback is named in the detail, so a
    # weaker piece of evidence is visible as weaker.
    budget = workload.slo.max_error_fraction * (got or 1)
    errs = run.num("error_request_count", "avg")
    summary = run.client.get("error_summary")
    if errs is not None:
        r.judge(errs <= budget, "no requests lost", f"{errs:.0f} errored, budget {budget:.0f}")
    elif summary is not None:
        n = len(summary) if isinstance(summary, (list, dict)) else 0
        r.judge(n <= budget, "no requests lost",
                f"{n} in the harness error summary (no error count published)")
    elif asked is not None and got is not None:
        r.judge(asked - got <= budget, "no requests lost",
                f"{asked - got:.0f} unaccounted for, inferred from completions against "
                f"the request count; this harness publishes no error field")
    else:
        r.add(UNKNOWN, "no requests lost",
              "the harness published no error count, no error summary and no request "
              "count -- nothing here can show whether requests were lost")

    if run.dump is None:
        r.add(UNKNOWN, "fleet telemetry",
              "no scrape dump; the client's word is the only record and nothing about "
              "the fleet can be checked or re-derived later")
        return r

    _telemetry(r, run, workload, deployment)
    return r


def _telemetry(r: Report, run: Run, workload: WorkloadProfile,
               deployment: DeploymentProfile | None) -> None:
    d = run.dump
    assert d is not None
    roles_counting = 2 if any("prefill" in k for k in d.roles_in(*_rough(d))) else 1
    w = d.window(start=run.start, end=run.end,
                 request_count=run.num("request_count", "avg"),
                 roles_counting=roles_counting)
    r.judge(w.trustworthy, "telemetry window is this run",
            f"{w.lo}-{w.hi}s by {w.basis}"
            + (f" ({w.segments} serving periods in the dump)" if w.segments > 1 else "")
            + (f"; {w.note}" if w.note else ""),
            soft=False)
    if not w.trustworthy:
        return

    gens = d.generations_in(w.lo, w.hi)
    r.judge(len(gens) <= 1, "one worker generation throughout",
            f"{sorted(gens)}; the fleet rolled mid-run and the window holds two "
            f"configurations" if len(gens) > 1 else f"{sorted(gens) or ['unlabelled']}")

    roles = d.roles_in(w.lo, w.hi)
    serving = [k for k in ("prefill", "decode", "aggregated") if k in roles]
    for role in serving:
        s = d.summarise(w.lo, w.hi, role)
        if s is None or not s.per_pod:
            r.add(UNKNOWN, f"{role}: readable", "no pod produced two scrapes in the window")
            continue
        if s.dropped:
            r.add(WARN, f"{role}: pods dropped",
                  "; ".join(f"{k}: {v}" for k, v in s.dropped.items()))
        if s.missing:
            r.add(UNKNOWN, f"{role}: series missing",
                  f"{s.missing} -- the engine map does not cover this build")

        bal = s.balance(adapters.REQUESTS_DONE)
        r.judge(None if bal is None else bal >= 0.8, f"{role}: load shared across workers",
                f"least-loaded pod at {100 * bal:.0f}% of the busiest"
                if bal is not None else "single worker in this role")

        if workload.applies(CHECK_PREFIX_CAPACITY):
            hit = s.ratio(adapters.CACHE_HITS, adapters.CACHE_QUERIES)
            band = (deployment.hit_rate_band or {}).get(role) if deployment else None
            if hit is None:
                r.add(UNKNOWN, f"{role}: prefix hit rate", "not published by this engine")
            elif band is None:
                r.add(UNKNOWN, f"{role}: prefix hit rate in band",
                      f"{100 * hit:.1f}% measured, but no calibrated band for this "
                      f"deployment -- run the calibration pass")
            else:
                r.judge(band[0] <= hit <= band[1], f"{role}: prefix hit rate in band",
                        f"{100 * hit:.1f}% against {100 * band[0]:.0f}-{100 * band[1]:.0f}%")

        pre = s.mean("queue")
        if pre is not None:
            r.judge(True, f"{role}: queue", f"{pre:.2f}s mean")

    if workload.applies(CHECK_PREFIX_CAPACITY):
        role = "prefill" if "prefill" in roles else "aggregated"
        cap, why = d.capacity_tokens(role, w.lo, w.hi)
        if cap is None:
            r.add(UNKNOWN, "cache capacity", why)
        else:
            ceiling = int(cap // workload.shape.final_context_tokens)
            eff = run.num("effective_concurrency", "avg") or 0
            r.judge(eff <= ceiling, "working set inside cache capacity",
                    f"{eff:.0f} sessions x {workload.shape.final_context_tokens} tok vs "
                    f"{cap / 1e6:.2f}M on {len(roles[role])} caching worker(s); "
                    f"ceiling ~{ceiling} sessions")

    if workload.applies(CHECK_SLO):
        _slo(r, run, workload)


def _rough(d: Dump) -> tuple[int, int]:
    ts = d.times
    return ts[0], ts[-1]


def _slo(r: Report, run: Run, workload: WorkloadProfile) -> None:
    target = workload.slo.ttft_p90_seconds
    ttft = run.num("time_to_first_token", "p90")
    if target is None or ttft is None:
        r.add(UNKNOWN, "TTFT p90 within SLO", "no target in the profile, or not reported",
              kind=TARGET)
    else:
        r.judge(ttft / 1000.0 <= target, "TTFT p90 within SLO",
                f"{ttft / 1000.0:.2f}s against {target}s", kind=TARGET)
    tgt = workload.slo.decode_tokens_per_second_per_user_p90
    per_user = run.num("output_token_throughput_per_user", "p90")
    if tgt is None or per_user is None:
        r.add(UNKNOWN, "decode rate per user within SLO", "no target, or not reported",
              kind=TARGET)
    else:
        r.judge(per_user >= tgt, "decode rate per user within SLO",
                f"{per_user:.1f} tok/s against {tgt}", kind=TARGET)


MATCH_FIELDS = (
    ("input_sequence_length", "ISL", 0.02),
    ("output_sequence_length", "OSL", 0.02),
    ("effective_concurrency", "concurrency", 0.02),
    ("request_count", "request count", 0.01),
)


def compare(a: Run, b: Run, workload: WorkloadProfile,
            deployment: DeploymentProfile | None,
            metric: str = "request_throughput",
            verdicts: dict[str, str] | None = None) -> tuple[Report, str]:
    """Two arms, one number, against a measured noise floor.

    Returns the report and the sentence that may be published. A claim is withheld
    rather than qualified: a difference printed beside a failed match check is a
    difference somebody will quote without the caveat.
    """
    r = Report(f"compare {a.label} vs {b.label} on {metric}  [{workload.name}]")

    for label, run in ((a.label, a), (b.label, b)):
        v = (verdicts or {}).get(label)
        if v is None:
            r.add(UNKNOWN, f"{label}: judged", "no verdict on file; run post first")
        else:
            r.judge(v in (VALID, CAVEATS), f"{label}: judged", v)

    for fieldname, human, tol in MATCH_FIELDS:
        va, vb = a.num(fieldname, "avg"), b.num(fieldname, "avg")
        if va is None or vb is None or vb == 0:
            r.add(UNKNOWN, f"{human} matched", "not reported by one of the arms")
            continue
        r.judge(abs(va - vb) / vb < tol, f"{human} matched",
                f"{va:.1f} vs {vb:.1f} ({100 * (va - vb) / vb:+.1f}%)")

    ma, mb = a.num(metric, "avg"), b.num(metric, "avg")
    if ma is None or mb is None or mb == 0:
        return r, f"{metric} is not in both summaries; nothing to compare."

    delta = (ma - mb) / mb
    floor = deployment.noise_floor.value if (deployment and deployment.noise_floor) else None
    if floor is None:
        r.add(UNKNOWN, "noise floor", "not calibrated for this deployment; a difference "
                                      "cannot be called real or null")

    body = f"{a.label} {ma:.4f}  {b.label} {mb:.4f}  delta {100 * delta:+.1f}%"
    if r.verdict == VOID:
        return r, f"{body}\n  WITHHELD: the arms are not comparable. Fix the failures above."
    if floor is None:
        return r, f"{body}\n  WITHHELD: no measured noise floor, so this could be nothing."
    if abs(delta) < floor:
        return r, (f"{body}\n  NULL: inside the measured {100 * floor:.1f}% floor "
                   f"({deployment.noise_floor.note or 'calibrated'}). Not a small effect "
                   f"-- no effect this instrument can see.")
    return r, (f"{body}\n  EFFECT: {100 * delta:+.1f}%, outside the measured "
               f"{100 * floor:.1f}% floor. One repeat of the winner confirms it.")
