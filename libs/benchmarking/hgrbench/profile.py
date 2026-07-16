"""What a workload is, and what a deployment was measured to do with it.

Two documents, and the split between them is the point of this package.

A **workload profile** is committed TOML, one per *use case* rather than per model:
agentic multi-turn, retrieval, chat, code completion, batch summarisation. It says what
traffic looks like, and -- more consequentially -- which questions are even meaningful
about it. Prefix-cache capacity is the binding constraint of a multi-turn agentic
workload and is not a fact about any model; the same model under single-turn traffic has
no shared working set at all, and every capacity check would be noise. A profile that
declares its own applicable checks is what stops a finding from one shape being enforced
on another.

A **deployment profile** is measured, not written. One per model x topology x build,
produced by a calibration pass and holding every threshold the gate later enforces:
the KV pool each role actually reports, the session ceiling that follows, the prefix hit
rate a healthy run of this shape holds, the noise floor across a reload, and the smallest
cell whose answer matches the big one. Nothing here is a literal, because a literal goes
stale in silence: the capacity constant this project carried for a month belonged to a
worker generation three builds old, and every judgement made against it was drawn against
a fleet that no longer existed.

A deployment profile therefore records the fingerprint it was measured on, and knows when
it no longer applies. A gate with no profile refuses; a gate with a stale one refuses and
says which field of the fingerprint moved. That is the mechanism by which a method
transfers to the next model instead of being re-derived from scratch, and it is also the
mechanism by which it declines to transfer where it should not.
"""

from __future__ import annotations

import json
import tomllib

from typing import Any
from pathlib import Path
from dataclasses import field
from dataclasses import asdict
from dataclasses import dataclass


CHECK_PREFIX_CAPACITY = "prefix_cache_capacity"
CHECK_SESSION_DEPTH = "session_depth"
CHECK_DISAGGREGATION = "disaggregation"
CHECK_SLO = "slo"

ALL_CHECKS = (CHECK_PREFIX_CAPACITY, CHECK_SESSION_DEPTH, CHECK_DISAGGREGATION, CHECK_SLO)


@dataclass(frozen=True)
class Shape:
    """The traffic, in tokens and turns.

    `turns` is not a knob for run length and the profile is where that is stated rather
    than remembered. Depth decides how far a session's unique context is amortised
    across its turns, so it moves throughput on its own -- one pair of runs here differ
    only in 25 turns against 8 and differ by 34% in requests per second. Shortening a
    run by shortening its sessions changes the workload, and the two runs are then not
    comparable to each other or to anything measured before.
    """

    shared_prefix_tokens: int = 0
    session_context_tokens: int = 0
    new_tokens_per_turn: int = 0
    output_tokens: int = 0
    turns: int = 1

    @property
    def first_context_tokens(self) -> int:
        return self.shared_prefix_tokens + self.session_context_tokens + self.new_tokens_per_turn

    @property
    def final_context_tokens(self) -> int:
        """Context at the last turn -- the number a working set is computed from.

        Not the mean input length. A session's context grows turn by turn, so the fleet
        is sized by where it ends, and reasoning from the mean under-states the working
        set by roughly a third on a long agentic shape.
        """
        grows = (self.turns - 1) * (self.new_tokens_per_turn + self.output_tokens)
        return self.first_context_tokens + grows

    @property
    def requests_per_session(self) -> int:
        return self.turns


@dataclass(frozen=True)
class SLO:
    """What the deployment is required to do, as distinct from what it can do.

    Kept separate from throughput on purpose. A configuration can win on requests per
    second and fail the target that the traffic is actually sold against, and a method
    that only attributes throughput cannot say when to stop tuning it.
    """

    ttft_p90_seconds: float | None = None
    decode_tokens_per_second_per_user_p90: float | None = None
    max_error_fraction: float = 0.0


@dataclass(frozen=True)
class WorkloadProfile:
    """One use case: its shape, its target, and which checks mean anything for it."""

    name: str
    description: str = ""
    shape: Shape = field(default_factory=Shape)
    slo: SLO = field(default_factory=SLO)
    checks: tuple[str, ...] = ALL_CHECKS
    harness_extra: str = ""

    @classmethod
    def load(cls, path: str | Path) -> WorkloadProfile:
        raw = tomllib.loads(Path(path).read_text())
        shape = Shape(**raw.get("shape", {}))
        slo = SLO(**raw.get("slo", {}))
        checks = tuple(raw.get("checks", list(ALL_CHECKS)))
        unknown = [c for c in checks if c not in ALL_CHECKS]
        if unknown:
            raise ValueError(f"{path}: unknown checks {unknown}; known are {list(ALL_CHECKS)}")
        return cls(
            name=raw["name"],
            description=raw.get("description", ""),
            shape=shape,
            slo=slo,
            checks=checks,
            harness_extra=raw.get("harness_extra", ""),
        )

    def applies(self, check: str) -> bool:
        return check in self.checks

    def request_count(self, sessions: int) -> int:
        """How many requests a run of `sessions` sessions must fire to reach full depth.

        Firing fewer truncates every session before its context has grown, which makes
        depth a function of concurrency rather than of the workload -- and it does not
        penalise a struggling configuration, it spares it. One comparison here was
        published as +53% and was really +132% for exactly this reason.
        """
        return sessions * self.shape.requests_per_session


@dataclass
class Calibration:
    """One measured threshold, and the runs that measured it.

    A threshold with no provenance is a literal with extra steps. `runs` names the
    benchmark ids behind the number so that a later correction re-reads what is on disk
    rather than re-running the fleet.
    """

    value: float
    unit: str = ""
    runs: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class DeploymentProfile:
    """Everything about one fleet that a later run is judged against.

    `fingerprint` is what makes it expire. It holds the image, the worker generation and
    the rendered engine arguments per role; when the live fleet's fingerprint differs in
    any of them, every calibrated number here was measured somewhere else.
    """

    model: str
    topology: str
    engine: str
    workload: str
    fingerprint: dict[str, Any] = field(default_factory=dict)
    capacity_tokens: dict[str, Calibration] = field(default_factory=dict)
    hit_rate_band: dict[str, list[float]] = field(default_factory=dict)
    noise_floor: Calibration | None = None
    standard_cell: dict[str, int] = field(default_factory=dict)
    baseline: dict[str, float] = field(default_factory=dict)

    def session_ceiling(self, shape: Shape, serving_role: str = "prefill") -> int | None:
        """How many concurrent sessions this fleet's cache holds at this shape.

        Only the roles that serve prefill count. In a disaggregated deployment the
        decode workers hold a pool of their own and it is not reuse capacity for the
        next turn of a session, so counting it inflates the ceiling by the decode side
        and hides the cliff entirely.
        """
        cap = self.capacity_tokens.get(serving_role)
        if cap is None or shape.final_context_tokens <= 0:
            return None
        return int(cap.value // shape.final_context_tokens)

    def stale_against(self, live: dict[str, Any]) -> list[str]:
        """Which fingerprint fields moved since this profile was measured."""
        out = []
        for k, was in sorted(self.fingerprint.items()):
            now = live.get(k)
            if now != was:
                out.append(f"{k}: {was!r} -> {now!r}")
        for k in sorted(set(live) - set(self.fingerprint)):
            out.append(f"{k}: absent -> {live[k]!r}")
        return out

    def to_json(self) -> str:
        def enc(o: Any) -> Any:
            if isinstance(o, Calibration):
                return asdict(o)
            raise TypeError(type(o))
        return json.dumps(asdict(self), indent=2, sort_keys=True, default=enc) + "\n"

    @classmethod
    def load(cls, path: str | Path) -> DeploymentProfile:
        raw = json.loads(Path(path).read_text())
        raw["capacity_tokens"] = {k: Calibration(**v)
                                  for k, v in raw.get("capacity_tokens", {}).items()}
        if raw.get("noise_floor"):
            raw["noise_floor"] = Calibration(**raw["noise_floor"])
        return cls(**raw)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())
        return p
