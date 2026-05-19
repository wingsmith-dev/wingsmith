"""Authentication: what an endpoint does when the credential is wrong or missing.

Short, and entirely generic — there is no model-shaped question in here. It is
kept as three separate observations rather than one pass or fail because the
standard asks about three cases and a supplier that rejects all of them with the
same status code as the original provider is the thing being checked.
"""

from __future__ import annotations

from typing import Any

from hgrconform.session import Session
from hgrconform.probes.registry import probe


@probe(
    id="auth.rejection",
    title="Status codes for a bad key, a missing header, and a malformed scheme",
    requirement="I. Authentication & Security — invalid key returns 401; requests without Authorization are rejected",
    compare=("invalid_key", "no_header", "bad_scheme", "error_shape"),
)
def rejection(session: Session) -> dict[str, Any]:
    body = session.body(max_tokens=8)
    base = {"Content-Type": "application/json"}

    invalid = session.post("/v1/chat/completions", body, headers=base | {"Authorization": "Bearer hgrconform-invalid"})
    missing = session.post("/v1/chat/completions", body, headers=base)
    malformed = session.post("/v1/chat/completions", body, headers=base | {"Authorization": "hgrconform-no-scheme"})

    error = invalid.at("error")
    return {
        "invalid_key": invalid.status,
        "no_header": missing.status,
        "bad_scheme": malformed.status,
        "error_shape": sorted(k for k in error if not k.startswith("__")) if isinstance(error, dict) else [],
        "www_authenticate": "www-authenticate" in {k.lower() for k in invalid.headers},
    }
