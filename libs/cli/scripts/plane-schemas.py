"""Print the plane's component schemas as {schema: {field: type}}.

The CLI's wire types are checked against this (internal/api/schema_test.go).
Run from libs/plane after changing a plane schema, and commit the result:

    cd libs/plane && APP_ENV=local uv run python ../cli/scripts/plane-schemas.py \
        > ../cli/internal/api/testdata/plane-schemas.json

It builds the app and reads its OpenAPI document; no database is needed.
"""

import json
import sys

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


def main() -> None:
    schema = create_app().openapi_schema.to_schema()
    out = {
        name: {field: kind(prop) for field, prop in body.get("properties", {}).items()}
        for name, body in schema["components"]["schemas"].items()
        if not name.startswith("ListResponse_")
    }
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
