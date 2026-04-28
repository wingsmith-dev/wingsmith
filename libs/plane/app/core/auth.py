"""Authentication for the plane: the single seam every caller resolves through.

Two principal kinds:

- **operator** — cluster agents, authenticated to the hub and the API with a
  static service token sent as `Authorization: Bearer <token>`, compared in
  constant time. This check cannot be disabled.
- **admin** — humans (console, `hgr`), authenticated by posting a password to
  `POST /v1/auth/login`, which returns a short-lived signed session token. The
  password is never a per-request credential; only the session token is.

`resolve_principal` is what every HTTP handler goes through (`app.core.context`).
The hub uses the narrower `resolve_operator`, which never accepts an admin
session token and is never subject to `ADMIN_AUTH` — see module docstring on
`app.hub.server` for why the two surfaces are allowed to diverge.
"""

import hmac
import time
import base64
import hashlib
import secrets

import bcrypt

from app.config import settings
from app.utils.schemas import Struct
from app.core.exceptions import NotAuthorized


LOCAL_OPERATOR_TOKEN = "hangar-local-operator-token"
"""Fallback operator token used only when `APP_ENV == 'local'` and
`OPERATOR_TOKEN` is unset. Everywhere else an unset token refuses to boot —
see `app.config.settings.validate`.
"""

SESSION_PREFIX = "hgrs1"
"""Version tag on session tokens, so a future format change can be told apart
from this one instead of failing signature verification silently.
"""

ENROLMENT_PREFIX = "hgro1"
"""Version tag on per-cluster operator enrolment tokens. The prefix lets the hub
tell a well-formed enrolment token apart from junk before it spends a database
lookup on it (see `looks_like_enrolment`), and lets a future format change be
distinguished instead of silently failing to resolve.
"""


class Principal(Struct):
    """The identity of a caller.

    Kept deliberately thin so a future OIDC/SSO integration can replace how a
    `Principal` is produced without touching a single call site that consumes
    one. `scope` is a seam for future tenant/project scoping — nothing in this
    change reads it; per-request project filtering stays out of scope here.
    """

    id: str
    kind: str
    name: str
    scopes: tuple[str, ...] = ()
    scope: str | None = None


def operator_principal(cluster_uid: str | None = None, cluster_name: str | None = None) -> Principal:
    """The operator identity a hub session runs as.

    Unbound (`cluster_uid is None`) is the legacy shared fleet token: the session
    trusts whichever cluster the `hello` frame names, as it always has. A bound
    principal comes from a per-cluster enrolment token — `scope` carries the
    cluster's uid, and the handshake refuses a `hello` that names a different
    cluster, so the token *is* the identity rather than a fleet-wide pass.
    """
    return Principal(
        id="00000000-0000-0000-0000-000000000001",
        kind="operator",
        name=cluster_name or "operator",
        scopes=("*",),
        scope=cluster_uid,
    )


STATIC_OPERATOR = operator_principal()
"""Convenience constant for tests and fixtures that need *an* operator
principal without going through token resolution.
"""


def admin_principal(name: str = "admin") -> Principal:
    return Principal(
        id="00000000-0000-0000-0000-000000000002",
        kind="admin",
        name=name,
        scopes=("*",),
    )


def bearer_token(header: str | None) -> str | None:
    """Extracts the credential from an Authorization header, if there is one."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value or None


def effective_operator_token() -> str:
    """The operator token this process actually checks against.

    Raises rather than falling back silently outside `local`; that path should
    be unreachable in practice because `app.config.settings.validate` refuses
    to boot first, but a library caller that skipped `validate` must still fail
    closed rather than compare against an empty string.
    """
    if settings.OPERATOR_TOKEN:
        return settings.OPERATOR_TOKEN
    if settings.APP_ENV == "local":
        return LOCAL_OPERATOR_TOKEN
    raise RuntimeError("OPERATOR_TOKEN is unset outside APP_ENV=local")


def resolve_operator(token: str | None) -> Principal | None:
    """Resolves a bearer token to the *unbound* operator principal, constant-time.

    This is the shared fleet token only (`OPERATOR_TOKEN`). Per-cluster enrolment
    tokens are resolved against the database by the hub, not here, because this
    seam is pure and has no session. Returns `None` rather than raising so callers
    with different failure behaviour (hub closes the socket, API tries an admin
    session next) can each decide what "no operator match" means for them.
    """
    if not token:
        return None
    expected = effective_operator_token()
    if hmac.compare_digest(token.encode(), expected.encode()):
        return operator_principal()
    return None


def mint_enrolment_token() -> tuple[str, str]:
    """Mints a per-cluster enrolment token: returns `(plaintext, sha256_hex)`.

    Only the hash is ever stored; the plaintext is shown once at register/rotate
    and cannot be recovered. The token is high-entropy random, so a fast hash is
    safe against precomputation and lets the hub resolve it with one indexed
    lookup — bcrypt would forbid the lookup and buys nothing over a password.
    """
    token = f"{ENROLMENT_PREFIX}.{secrets.token_urlsafe(32)}"
    return token, hash_enrolment_token(token)


def hash_enrolment_token(token: str) -> str:
    """The sha256 hex digest stored for, and looked up by, an enrolment token."""
    return hashlib.sha256(token.encode()).hexdigest()


REMOTE_WRITE_BCRYPT_ROUNDS = 8
"""Cost factor for the bcrypt verifier the metrics write ingress checks.

Deliberately below the library's default of 12. The credential is a 256-bit
machine-generated token, so an attacker holding the hash gains nothing from a
slower KDF — there is no dictionary to walk and no human-chosen password to
recover. What the cost does buy is CPU spent on Traefik for *every* remote-write
request on a public endpoint, which is a denial-of-service lever. 8 keeps the
hash a bcrypt hash rather than a bare digest while costing roughly a sixteenth of
the default per verification (ADR 0012).
"""


def hash_remote_write_token(token: str) -> str:
    """Bcrypt-hashes an enrolment token for the metrics write ingress's htpasswd.

    A second verifier over the same credential, minted alongside the sha256 the
    hub compares. Traefik's `basicAuth` middleware can only check a crypt-style
    hash, and the hub needs an indexed lookup — neither hash can serve both, so
    both are stored and the plaintext still exists only at mint time.
    """
    salt = bcrypt.gensalt(rounds=REMOTE_WRITE_BCRYPT_ROUNDS)
    return bcrypt.hashpw(token.encode(), salt).decode()


def looks_like_enrolment(token: str | None) -> bool:
    """Whether a token is shaped like an enrolment token, without a lookup.

    Lets the hub reject obvious junk before the WebSocket upgrade while still
    admitting a well-formed-but-unknown token far enough to be resolved (and
    then closed) — the authoritative check is the database lookup.
    """
    return bool(token) and token.startswith(f"{ENROLMENT_PREFIX}.")


def hash_password(password: str) -> str:
    """Bcrypt-hashes an admin password for `ADMIN_PASSWORD_HASH`.

    Generate one with:

        uv run python -c "from app.core.auth import hash_password; print(hash_password('...'))"
    """
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def _sign(payload: bytes) -> str:
    secret = settings.SESSION_SIGNING_SECRET or LOCAL_OPERATOR_TOKEN
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def issue_session_token(name: str, ttl_seconds: int | None = None) -> str:
    """Mints a signed, expiring session token naming an admin principal."""
    ttl = settings.SESSION_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    expires_at = int(time.time()) + ttl
    payload = f"{name}:{expires_at}".encode()
    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    signature = _sign(payload)
    return f"{SESSION_PREFIX}.{payload_b64}.{signature}"


def verify_session_token(token: str) -> Principal | None:
    """Verifies a session token's signature and expiry, constant-time.

    Returns `None` on any malformed, tampered, unsigned-for, or expired token
    rather than raising, so `resolve_principal` can try it as one option among
    several credential kinds.
    """
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != SESSION_PREFIX:
        return None
    _, payload_b64, signature = parts
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    except Exception:  # noqa: BLE001 - any decode failure means "not a valid token"
        return None
    expected_signature = _sign(payload)
    if not hmac.compare_digest(signature.encode(), expected_signature.encode()):
        return None
    try:
        name, _, expires_raw = payload.decode().rpartition(":")
        expires_at = int(expires_raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not name or time.time() >= expires_at:
        return None
    return admin_principal(name=name)


def admin_auth_enabled() -> bool:
    """Whether the HTTP API requires admin credentials at all.

    `ADMIN_AUTH` unset resolves to `disabled` under `APP_ENV=local` and
    `enabled` everywhere else, matching `app.config.settings.resolved_admin_auth`.
    Disabling it never touches the hub: `resolve_operator` has no such switch.
    """
    mode = settings.ADMIN_AUTH or ("disabled" if settings.APP_ENV == "local" else "enabled")
    return mode == "enabled"


def resolve_principal(token: str | None) -> Principal:
    """Resolves a bearer token to a caller identity for the HTTP API.

    Tries the operator token, then an admin session token. If neither matches
    and admin auth is disabled (see `admin_auth_enabled`), the caller is
    treated as the console/CLI operating behind a trusted perimeter (no
    ingress, `kubectl port-forward` only) and resolves to an admin principal.
    Otherwise raises `NotAuthorized` — this function never returns a fabricated
    identity for a bad or missing credential.
    """
    operator = resolve_operator(token)
    if operator is not None:
        return operator

    if token:
        session = verify_session_token(token)
        if session is not None:
            return session

    if not admin_auth_enabled():
        return admin_principal()

    raise NotAuthorized("missing or invalid credentials")
