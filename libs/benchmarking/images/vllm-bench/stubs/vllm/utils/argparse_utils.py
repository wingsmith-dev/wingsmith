# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# A reduced stand-in for vLLM v0.28.0's vllm/utils/argparse_utils.py, without its torch-dependent internals; see README.md.
import argparse


class FlexibleArgumentParser(argparse.ArgumentParser):
    """Slim stand-in: accepts dashes or underscores in long option names."""

    def parse_args(self, args=None, namespace=None):
        if args is None:
            import sys

            args = sys.argv[1:]
        fixed = []
        for arg in args:
            if arg.startswith("--") and "_" in arg.split("=")[0]:
                head, _, tail = arg.partition("=")
                head = head.replace("_", "-")
                arg = f"{head}={tail}" if _ else head
            fixed.append(arg)
        return super().parse_args(fixed, namespace)
