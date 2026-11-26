"""Print the JSON Schema of the plane's manifest kinds (Workload, Endpoint, Artifact).

The CLI embeds it (internal/api/manifest-schema.json). Unlike the OpenAPI document it keeps the
`x-immutable` and `x-defaulted` markers that apply and diff read. Run from libs/plane after
changing app/modules/manifest/schemas.py, and commit the result:

    cd libs/plane && uv run python ../cli/scripts/manifest-schema.py \
        > ../cli/internal/api/manifest-schema.json

With `--check PATH` it prints nothing and exits 1 when PATH is not what it would write.
"""

import sys
import json
import argparse

from app.modules.manifest.schemas import json_schema


def render() -> str:
    return json.dumps(json_schema(), indent=1, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", metavar="PATH", help="compare with a committed copy instead of printing")
    args = parser.parse_args()
    current = render()
    if args.check:
        with open(args.check) as f:
            if f.read() != current:
                print(f"{args.check} differs from the plane; regenerate it (see this script's docstring)", file=sys.stderr)
                sys.exit(1)
        return
    sys.stdout.write(current)


if __name__ == "__main__":
    main()
