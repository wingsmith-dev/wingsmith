"""Fails when the generated HTTP reference in `libs/packages/contract/` has drifted from the plane.

`openapi.json` and `REFERENCE.md` are generated from the plane's routes, and `API.md` §6 must name
every route group the plane serves. The check lives with the contract
(`libs/packages/contract/scripts/openapi.py --check`); this runs it with the plane's suite so a
route change that forgets to regenerate them fails here. It runs in a subprocess because the
script builds the app under `APP_ENV=local`, which must not leak into this process's settings.
"""

import sys
import subprocess

from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "packages" / "contract" / "scripts" / "openapi.py"


def test_the_generated_reference_matches_the_routes_the_plane_serves():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "the HTTP contract has drifted from the plane; run libs/packages/contract/scripts/openapi.sh "
        f"and commit the result:\n{result.stderr}{result.stdout}"
    )
