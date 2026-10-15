# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# Adapted from vLLM v0.28.0's benchmark CLI so it runs without the engine; see README.md.
"""Entrypoint: `vllm bench sweep <subcommand>` without the vLLM engine.

Replaces upstream `vllm/benchmarks/sweep/cli.py`, which cannot be vendored: it
imports `vllm.entrypoints.serve.utils.api_utils` for a parser epilog, and that
pulls in the whole engine. The subcommand table here is the upstream one minus
`startup`, which needs `vllm.benchmarks.startup` and therefore torch.
"""

import argparse
import sys

from vllm.benchmarks.sweep.plot import SweepPlotArgs
from vllm.benchmarks.sweep.plot import main as plot_main
from vllm.benchmarks.sweep.plot_pareto import SweepPlotParetoArgs
from vllm.benchmarks.sweep.plot_pareto import main as plot_pareto_main
from vllm.benchmarks.sweep.serve import SweepServeArgs
from vllm.benchmarks.sweep.serve import main as serve_main
from vllm.benchmarks.sweep.serve_workload import SweepServeWorkloadArgs
from vllm.benchmarks.sweep.serve_workload import main as serve_workload_main
from vllm.utils.argparse_utils import FlexibleArgumentParser

SUBCOMMANDS = (
    (SweepServeArgs, serve_main),
    (SweepServeWorkloadArgs, serve_workload_main),
    (SweepPlotArgs, plot_main),
    (SweepPlotParetoArgs, plot_pareto_main),
)


def add_cli_args(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(required=True, dest="sweep_type")

    for cmd, entrypoint in SUBCOMMANDS:
        subparser = subparsers.add_parser(
            cmd.parser_name,
            description=cmd.parser_help,
            usage=f"sweep.py {cmd.parser_name} [options]",
        )
        subparser.set_defaults(dispatch_function=entrypoint)
        cmd.add_cli_args(subparser)


if __name__ == "__main__":
    parser = FlexibleArgumentParser(description="vllm bench sweep (slim)")
    add_cli_args(parser)
    args = parser.parse_args()
    args.dispatch_function(args)
    sys.exit(0)
