"""Print the plane's component schemas as {schema: {field: type}}.

The CLI's wire types are checked against this (internal/api/schema_test.go).
Run from libs/plane after changing a plane schema, and commit the result:

    cd libs/plane && WINGSMITH_APP_ENV=local uv run python ../cli/scripts/plane-schemas.py \
        > ../cli/internal/api/testdata/plane-schemas.json

With `--check PATH` it prints nothing to stdout and instead exits 1, naming the
schemas that differ, when PATH is not what it would write. That is the check a
CI job runs to catch a stale snapshot.

It builds the app and reads its OpenAPI document; no database is needed.
"""

import sys
import json
import argparse

from app.api.app import create_app


def kind(prop: dict) -> str:
    if "$ref" in prop:
        return "$" + prop["$ref"].rsplit("/", 1)[-1]
    if "oneOf" in prop:
        types = [kind(p) for p in prop["oneOf"] if p.get("type") != "null"]
        nullable = any(p.get("type") == "null" for p in prop["oneOf"])
        return "|".join(types) + ("?" if nullable else "")
    if prop.get("type") == "array":
        return "[]" + kind(prop.get("items", {}))
    return prop.get("type", "any")


def snapshot() -> dict:
    schema = create_app().openapi_schema.to_schema()
    return {
        name: {field: kind(prop) for field, prop in body.get("properties", {}).items()}
        for name, body in schema["components"]["schemas"].items()
        if not name.startswith("ListResponse_")
    }


def check(path: str, current: dict) -> int:
    with open(path) as f:
        committed = json.load(f)
    stale = sorted(name for name in committed.keys() | current.keys() if committed.get(name) != current.get(name))
    for name in stale:
        print(f"{path}: {name} differs from the plane", file=sys.stderr)
    if stale:
        print("regenerate it: see the docstring of scripts/plane-schemas.py", file=sys.stderr)
    return 1 if stale else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", metavar="PATH", help="compare with a committed snapshot instead of printing")
    args = parser.parse_args()
    current = snapshot()
    if args.check:
        sys.exit(check(args.check, current))
    json.dump(current, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
