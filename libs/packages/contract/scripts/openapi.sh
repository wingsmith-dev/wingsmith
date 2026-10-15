#!/usr/bin/env bash
# Regenerate openapi.json and REFERENCE.md from the plane, or with --check fail when either is
# stale or API.md's resources table disagrees with the routes the plane serves.
#
# Runs in the plane's own environment (uv, libs/plane/pyproject.toml); set HANGAR_PLANE_PYTHON to an
# interpreter that already has the plane's dependencies to skip uv.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plane="$(cd "$here/../../../plane" && pwd)"
if [ -n "${HANGAR_PLANE_PYTHON:-}" ]; then
  exec "$HANGAR_PLANE_PYTHON" "$here/openapi.py" "$@"
fi
exec uv run --quiet --project "$plane" python "$here/openapi.py" "$@"
