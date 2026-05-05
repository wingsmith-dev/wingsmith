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
