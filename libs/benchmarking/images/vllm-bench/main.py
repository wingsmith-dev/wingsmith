"""Entrypoint: `vllm bench serve` without the vLLM engine."""

import sys

from vllm.benchmarks.serve import add_cli_args, main
from vllm.utils.argparse_utils import FlexibleArgumentParser

if __name__ == "__main__":
    parser = FlexibleArgumentParser(description="vllm bench serve (slim)")
    add_cli_args(parser)
    sys.exit(0 if main(parser.parse_args()) is not None else 1)
