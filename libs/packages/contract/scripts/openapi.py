"""Export the plane's OpenAPI schema and the route reference, or check them.

Writes `openapi.json` and `REFERENCE.md` beside `API.md`, both generated from
the plane's own route table, so the route-level reference cannot describe a
route the plane does not serve. `--check` writes nothing and fails when either
file is stale, or when a route group the plane serves is missing from the
resources table in `API.md` §6, or that table names a path the plane does not
serve.

Run it through `scripts/openapi.sh`, which runs it in the plane's environment.
"""

import os
import re
import sys
import json
import argparse

from pathlib import Path


CONTRACT = Path(__file__).resolve().parent.parent
PLANE = CONTRACT.parent.parent / "plane"
OPENAPI = CONTRACT / "openapi.json"
REFERENCE = CONTRACT / "REFERENCE.md"
API = CONTRACT / "API.md"

METHODS = ("get", "post", "put", "patch", "delete")


def build_schema() -> dict:
    # The plane serves its schema under `local` only; building the app is all
    # this needs, and nothing here opens a database connection.
    os.environ["HANGAR_APP_ENV"] = "local"
    sys.path.insert(0, str(PLANE))
    from app.api.app import create_app

    return create_app().openapi_schema.to_schema()


def render_schema(schema: dict) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def group_of(path: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "v1":
        return f"/v1/{parts[1]}"
    return "/" + parts[0]


def first_sentence(text: str) -> str:
    paragraph = " ".join(text.strip().split("\n\n", 1)[0].split())
    match = re.match(r"(.+?[.!?])(\s|$)", paragraph)
    sentence = match.group(1) if match else paragraph
    return sentence.replace("|", "\\|")


def render_reference(schema: dict) -> str:
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for path, operations in schema["paths"].items():
        for method in METHODS:
            operation = operations.get(method)
            if operation is None:
                continue
            what = first_sentence(operation.get("description") or "") or operation.get("summary", "")
            groups.setdefault(group_of(path), []).append((path, method.upper(), what))

    lines = [
        "# HTTP API reference",
        "",
        "Generated from the plane by `scripts/openapi.sh`; do not edit by hand. Every route the plane",
        "serves, grouped by collection. Parameters, request bodies and response schemas are in",
        "[`openapi.json`](openapi.json). What the routes mean, who may call them and the rules every",
        "response follows are in [`API.md`](API.md).",
        "",
    ]
    for group in sorted(groups):
        lines += [f"## `{group}`", "", "| Method | Path | What |", "|---|---|---|"]
        for path, method, what in sorted(groups[group], key=lambda r: (r[0], METHODS.index(r[1].lower()))):
            lines.append(f"| {method} | `{path}` | {what} |")
        lines.append("")
    return "\n".join(lines)


def normalise(path: str) -> list[str]:
    return [("{}" if part.startswith("{") else part) for part in path.strip("/").split("/")]


def check_api_md(schema: dict) -> list[str]:
    text = API.read_text()
    start = text.index("## 6. Resources")
    end = text.index("\n## ", start + 1)
    rows = [line for line in text[start:end].splitlines() if line.startswith("| `/v1/")]
    table_paths = [p for row in rows for p in re.findall(r"`(/v1/[^`]+)`", row.split("|")[1])]

    served = [normalise(p) for p in schema["paths"]]
    problems = []
    for group in sorted({group_of(p) for p in schema["paths"] if p.startswith("/v1/")}):
        if group not in {group_of(p) for p in table_paths}:
            problems.append(f"API.md §6 has no row for {group}, which the plane serves")
    for path in table_paths:
        want = normalise(path)
        if not any(candidate[: len(want)] == want for candidate in served):
            problems.append(f"API.md §6 names {path}, which the plane does not serve")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="write nothing; fail on drift")
    args = parser.parse_args()

    schema = build_schema()
    generated = {OPENAPI: render_schema(schema), REFERENCE: render_reference(schema)}

    if not args.check:
        for path, content in generated.items():
            path.write_text(content)
        problems = check_api_md(schema)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0

    problems = [
        f"{path.name} is stale; run libs/packages/contract/scripts/openapi.sh"
        for path, content in generated.items()
        if not path.exists() or path.read_text() != content
    ]
    problems += check_api_md(schema)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
