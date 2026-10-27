"""Every harness this runner can drive, in one table.

Adding one is two files and one line: a module beside this one that builds a
command and reads an export, an entry in `metrics.ADAPTERS` naming where its
numbers are, and a line here. Nothing else in the package is harness-specific,
which is the point of the table. It is kept the same shape as
`libs/plane/app/modules/benchmarks/harnesses.py`, so a harness that graduates to
running inside a cluster is a port instead of a rewrite.
"""

from __future__ import annotations

from hgrbench.harnesses import aiperf
from hgrbench.harnesses import guidellm
from hgrbench.harnesses import vllm_bench
from hgrbench.harnesses import vllm_sweep
from hgrbench.harnesses.base import Harness


HARNESSES: dict[str, Harness] = {
    aiperf.HARNESS.name: aiperf.HARNESS,
    vllm_bench.HARNESS.name: vllm_bench.HARNESS,
    vllm_sweep.HARNESS.name: vllm_sweep.HARNESS,
    guidellm.HARNESS.name: guidellm.HARNESS,
}

DEFAULT = aiperf.HARNESS.name


def get(name: str) -> Harness:
    """The named harness, or a complaint listing the ones there are."""
    if name not in HARNESSES:
        raise KeyError(f"unknown harness {name!r}; this runner has {', '.join(sorted(HARNESSES))}")
    return HARNESSES[name]


def names() -> list[str]:
    return sorted(HARNESSES)
