"""NVIDIA AIPerf, driven the same way the plane's operator drives it.

Closed-loop and concurrency-first: it holds N requests in flight and reports the
distribution. One invocation is one operating point, which is why a load curve
here is a loop over cells rather than a flag.

The image is the one `libs/plane/app/modules/benchmarks/harnesses.py` pins, on
purpose. A local finding that cannot be reproduced by the cluster path is a local
curiosity; the same image at the same version makes it evidence.
"""

from __future__ import annotations

from hgrbench.plan import Cell
from hgrbench.harnesses.base import TOKEN_ENV
from hgrbench.harnesses.base import Context
from hgrbench.harnesses.base import Harness
from hgrbench.harnesses.base import Emission
from hgrbench.harnesses.base import Invocation
from hgrbench.harnesses.base import find_one
from hgrbench.harnesses.base import read_json
from hgrbench.harnesses.base import shell_command
from hgrbench.harnesses.base import recorded_version


IMAGE = "nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0"
DIGEST = "sha256:d22eb02bccadcd2acbbd905a1742f1e642f2bdd7bc3a0377cebda685a233902d"
PINNED_VERSION = "0.12.0"
ARTIFACTS = "/artifacts"
EXPORT = "profile_export_aiperf.json"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def command(cell: Cell, ctx: Context) -> Invocation:
    """One AIPerf run at one operating point.

    `--request-count` and `--concurrency` together are the closed-loop knob pair.
    AIPerf has no duration budget, so a cell that asked for seconds instead of
    requests is converted by the runner before it gets here.

    `ignore_eos` is set so that the generation length is the one asked for. It makes
    the comparison across harnesses and models fair and it makes the question
    unnatural, which is a trade the report states rather than hides.
    """
    args = [
        "aiperf profile",
        f"--model {_quote(cell.model.name)}",
        f"--url {_quote(ctx.endpoint_url)}",
        "--endpoint-type 'chat'",
        "--streaming",
        f'-H "Authorization: Bearer ${TOKEN_ENV}"',
        f"--tokenizer {_quote(cell.model.tokenizer)}",
        f"--concurrency {cell.load.concurrency}",
        f"--request-count {cell.load.requests}",
        f"--synthetic-input-tokens-mean {cell.input_tokens}",
        f"--output-tokens-mean {cell.output_tokens}",
        "--extra-inputs ignore_eos:true",
        "--ui simple",
        f"--artifact-dir {ARTIFACTS}",
    ]
    return shell_command(ctx, " ".join(args), mount_results_at=ARTIFACTS)


def read(cell: Cell, ctx: Context) -> list[Emission]:
    """AIPerf's own export, and the version that wrote it."""
    export = find_one(ctx.results_dir, EXPORT)
    summary = read_json(export)
    version, notes = recorded_version(summary.get("aiperf_version"), HARNESS)
    return [
        Emission(
            summary=summary,
            version=version,
            load_label=cell.load.label,
            load={"kind": "concurrency", "concurrency": cell.load.concurrency},
            notes=notes,
        )
    ]


HARNESS = Harness(
    name="aiperf",
    image=IMAGE,
    digest=DIGEST,
    pinned_version=PINNED_VERSION,
    command=command,
    read=read,
    description="NVIDIA AIPerf. Closed-loop, concurrency-first, one operating point per run.",
    notes=(
        "The same image and version the plane's benchmarks module pins, so a local "
        "number and a cluster number are the same measurement.",
        "Its amd64 variant hangs under emulation on Apple silicon — the digest pin is "
        "not an optimisation, it is the difference between running and not.",
    ),
)
