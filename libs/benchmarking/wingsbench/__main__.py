"""`python3 -m wingsbench`: the same entry point as the installed script."""

from wingsbench.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
