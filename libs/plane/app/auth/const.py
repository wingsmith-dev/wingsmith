"""Token format constants and cost factors.

Every token minted by the plane carries a version prefix so a format change can
be told apart from a malformed token instead of silently failing signature
verification.
"""

LOCAL_OPERATOR_TOKEN = "hangar-local-operator-token"
"""Fallback operator token used only when `APP_ENV == 'local'` and
`OPERATOR_TOKEN` is unset. Everywhere else an unset token refuses to boot —
see `app.config.settings.validate`.
"""

SESSION_PREFIX = "hgrs1"
"""Version tag on session tokens."""

ENROLMENT_PREFIX = "hgro1"
"""Version tag on per-cluster operator enrolment tokens. The prefix lets the hub
tell a well-formed enrolment token apart from junk before it spends a database
lookup on it, and lets a future format change be distinguished instead of
silently failing to resolve.
"""

TENANT_PREFIX = "hgrt1"
"""Version tag on tenant console tokens. A tenant token and an enrolment token are
resolved against different tables and grant unrelated things, so they must never be
told apart by a failed lookup."""

REFRESH_PREFIX = "hgrr1"
"""Version tag on refresh tokens, so a session token and a refresh token cannot
be confused."""

REMOTE_WRITE_BCRYPT_ROUNDS = 8
"""Cost factor for the bcrypt verifier the metrics write ingress checks.

Deliberately below the library's default of 12. The credential is a 256-bit
machine-generated token, so an attacker holding the hash gains nothing from a
slower KDF — there is no dictionary to walk and no human-chosen password to
recover. What the cost does buy is CPU spent on Traefik for *every* remote-write
request on a public endpoint, which is a denial-of-service lever. 8 keeps the
hash a bcrypt hash rather than a bare digest while costing roughly a sixteenth
of the default per verification (ADR 0012).
"""
