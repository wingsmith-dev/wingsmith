"""The frozen table of availability objectives: which outcomes count against an endpoint.

An availability number is a ratio, and which gateway outcomes sit in its numerator
and its denominator is a commercial definition rather than a query detail. A
`ServingQuota` names one of these entries by version; this table is the only place
the definition lives, and `promql.DIALECT` renders its failure and availability
expressions from it rather than carrying a second copy of the outcome sets.

Entries are superseded, never edited. A number recomputed under a redefined
objective is a number nobody can audit, so a change to either set is a new
version, and every quota naming the old one keeps meaning what it was sold as.

The outcome vocabulary is the gateway's closed set
(`libs/gateway/internal/metrics/outcome.go`). An entry names outcomes from it and
nothing else, which is checked at import.
"""

from dataclasses import dataclass


OUTCOMES = frozenset(
    {
        "ok",
        "client_error",
        "auth_rejected",
        "rate_limited",
        "shed",
        "no_upstream",
        "upstream_error",
        "upstream_timeout",
        "canceled",
        "internal_error",
    }
)
"""Every outcome the gateway labels a request with. Closed: no operator can add one."""


@dataclass(frozen=True)
class Objective:
    """One version of the availability definition.

    `failed` are the outcomes that are ours to answer for. `excluded` are the
    outcomes that sit in neither half of the ratio; everything else is counted, so
    the denominator always contains the numerator. Both are tuples so the rendered
    matcher is the same string on every import. `failure_metric` and
    `availability_metric` are the registered metric names the gateway dialect
    answers under this definition, so a caller resolves the version to a name and
    never reads a metric by literal name.
    """

    version: str
    failed: tuple[str, ...]
    excluded: tuple[str, ...]
    failure_metric: str
    availability_metric: str
    description: str

    @property
    def counted(self) -> frozenset[str]:
        return OUTCOMES - frozenset(self.excluded)

    @property
    def failed_matcher(self) -> str:
        """The `outcome` matcher selecting the numerator."""
        return f'outcome=~"{"|".join(self.failed)}"'

    @property
    def counted_matcher(self) -> str:
        """The `outcome` matcher selecting the denominator, written as what it leaves out."""
        if len(self.excluded) == 1:
            return f'outcome!="{self.excluded[0]}"'
        return f'outcome!~"{"|".join(self.excluded)}"'


AVAILABILITY_2026_1 = Objective(
    version="availability-2026.1",
    failed=("shed", "no_upstream", "upstream_error", "upstream_timeout", "internal_error"),
    excluded=("canceled",),
    failure_metric="serving.failure_ratio",
    availability_metric="serving.availability",
    description=(
        "Live traffic only. Failed: shed, no_upstream, upstream_error, upstream_timeout, "
        "internal_error. Counted: every outcome except canceled, so a caller's own 4xx and a "
        "429 over its declared limit count as served, and a caller hanging up counts as neither."
    ),
)
"""The definition every quota has carried since quotas existed.

`rate_limited` is not a failure because a 429 issued to an identified caller over
its own declared limit is the standard's carve-out, while `shed` — the same status,
issued by the gateway to protect itself — is. `canceled` is out of both sets: a
caller that hung up took its own request away.
"""

AVAILABILITY_OBJECTIVES: dict[str, Objective] = {AVAILABILITY_2026_1.version: AVAILABILITY_2026_1}

DEFAULT_AVAILABILITY_OBJECTIVE = AVAILABILITY_2026_1.version


def objective(version: str) -> Objective | None:
    """The entry a quota names, or `None` for a version that does not exist."""
    return AVAILABILITY_OBJECTIVES.get(version)


def _check(entries: dict[str, Objective]) -> None:
    for version, entry in entries.items():
        if version != entry.version:
            raise ValueError(f"objective {version!r} is filed under the wrong key")
        named = frozenset(entry.failed) | frozenset(entry.excluded)
        if not named <= OUTCOMES:
            raise ValueError(f"objective {version!r} names an outcome outside the gateway's set")
        if not entry.excluded:
            raise ValueError(f"objective {version!r} must exclude at least one outcome, as canceled always is")
        if frozenset(entry.failed) & frozenset(entry.excluded):
            raise ValueError(f"objective {version!r} both fails and excludes one outcome")


_check(AVAILABILITY_OBJECTIVES)
