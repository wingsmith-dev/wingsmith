"""Token format constants and cost factors.

Every token minted by the plane carries a version prefix so a format change can
be told apart from a malformed token instead of silently failing signature
verification.
"""

LOCAL_GATEWAY_SERVICE_TOKEN = "hangar-local-gateway-token"
"""Fallback gateway service token, used only when `APP_ENV == 'local'` and
`GATEWAY_SERVICE_TOKEN` is unset, so a fresh checkout's gateway can read the
plane with no configuration. Outside `local` an unset token means no gateway
principal resolves at all; nothing falls back to this value.
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

TENANT_API_KEY_PREFIX = "hgrk1"
"""Version tag on tenant API keys. A gateway inference credential, distinct from
`TENANT_PREFIX`: the console token signs in here, the API key calls the gateway,
and the two must never resolve against each other's table."""

REFRESH_PREFIX = "hgrr1"
"""Version tag on refresh tokens, so a session token and a refresh token cannot
be confused."""

REMOTE_WRITE_BCRYPT_ROUNDS = 8
"""Cost factor for the bcrypt verifier the metrics write ingress checks.

Below the library's default of 12. The credential is a 256-bit
machine-generated token, so an attacker holding the hash gains nothing from a
slower KDF: there is no dictionary to walk and no human-chosen password to
recover. What the cost does buy is CPU spent on Traefik for *every* remote-write
request on a public endpoint, which is a denial-of-service lever. 8 keeps the
hash a bcrypt hash instead of a bare digest while costing roughly a sixteenth
of the default per verification (ADR 0012).
"""
