"""Pure authentication logic: no database, no I/O.

`AuthManager` owns credential verification, token minting and verification,
and principal resolution. Every method is a staticmethod — nothing here holds
state; the only inputs are the arguments and `settings`.

The hub uses `resolve_operator` and `resolve_enrolment`; the HTTP API goes
through `resolve_principal`, which tries each kind in turn.
"""

import hmac
import time
import base64
import hashlib
import secrets

import bcrypt

from app.config import settings
from .const import SESSION_PREFIX
from .const import ENROLMENT_PREFIX
from .const import REFRESH_PREFIX
from .const import LOCAL_OPERATOR_TOKEN
from .const import REMOTE_WRITE_BCRYPT_ROUNDS
from .schemas import Principal
from app.core.exceptions import NotAuthorized


class AuthManager:
    """Credential verification and token operations for both principal kinds."""

    @staticmethod
    def operator_principal(
        cluster_uid: str | None = None,
        cluster_name: str | None = None,
    ) -> Principal:
        """The operator identity a hub session runs as.

        Unbound (`cluster_uid is None`) is the legacy shared fleet token: the
        session trusts whichever cluster the `hello` frame names, as it always
        has. A bound principal comes from a per-cluster enrolment token —
        `scope` carries the cluster's uid, and the handshake refuses a `hello`
        that names a different cluster, so the token *is* the identity rather
        than a fleet-wide pass.
        """
        return Principal(
            id="00000000-0000-0000-0000-000000000001",
            kind="operator",
            name=cluster_name or "operator",
            scopes=("*",),
            scope=cluster_uid,
        )

    @staticmethod
    def admin_principal(name: str = "admin") -> Principal:
        return Principal(
            id="00000000-0000-0000-0000-000000000002",
            kind="admin",
            name=name,
            scopes=("*",),
        )

    @staticmethod
    def bearer_token(header: str | None) -> str | None:
        """Extracts the credential from an Authorization header, if there is one."""
        if not header:
            return None
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return value or None

    @staticmethod
    def effective_operator_token() -> str:
        """The operator token this process actually checks against.

        Raises rather than falling back silently outside `local`; that path
        should be unreachable in practice because `app.config.settings.validate`
        refuses to boot first, but a library caller that skipped `validate`
        must still fail closed rather than compare against an empty string.
        """
        if settings.OPERATOR_TOKEN:
            return settings.OPERATOR_TOKEN
        if settings.APP_ENV == "local":
            return LOCAL_OPERATOR_TOKEN
        raise RuntimeError("OPERATOR_TOKEN is unset outside APP_ENV=local")

    @staticmethod
    def resolve_operator(token: str | None) -> Principal | None:
        """Resolves a bearer token to the *unbound* operator principal, constant-time.

        This is the shared fleet token only (`OPERATOR_TOKEN`). Per-cluster
        enrolment tokens are resolved against the database by the hub, not here,
        because this seam is pure and has no session. Returns `None` rather than
        raising so callers with different failure behaviour (hub closes the
        socket, API tries an admin session next) can each decide what "no
        operator match" means for them.
        """
        if not token:
            return None
        expected = AuthManager.effective_operator_token()
        if hmac.compare_digest(token.encode(), expected.encode()):
            return AuthManager.operator_principal()
        return None

    @staticmethod
    def mint_enrolment_token() -> tuple[str, str]:
        """Mints a per-cluster enrolment token: `(plaintext, sha256_hex)`.

        Only the hash is ever stored; the plaintext is shown once at
        register/rotate and cannot be recovered. The token is high-entropy
        random, so a fast hash is safe against precomputation and lets the hub
        resolve it with one indexed lookup — bcrypt would forbid the lookup
        and buys nothing over a password.
        """
        token = f"{ENROLMENT_PREFIX}.{secrets.token_urlsafe(32)}"
        return token, AuthManager.hash_enrolment_token(token)

    @staticmethod
    def hash_enrolment_token(token: str) -> str:
        """The sha256 hex digest stored for, and looked up by, an enrolment token."""
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def hash_remote_write_token(token: str) -> str:
        """Bcrypt-hashes an enrolment token for the metrics write ingress's htpasswd.

        A second verifier over the same credential, minted alongside the sha256
        the hub compares. Traefik's `basicAuth` middleware can only check a
        crypt-style hash, and the hub needs an indexed lookup — neither hash
        can serve both, so both are stored and the plaintext still exists only
        at mint time.
        """
        salt = bcrypt.gensalt(rounds=REMOTE_WRITE_BCRYPT_ROUNDS)
        return bcrypt.hashpw(token.encode(), salt).decode()

    @staticmethod
    def looks_like_enrolment(token: str | None) -> bool:
        """Whether a token is shaped like an enrolment token, without a lookup.

        Lets the hub reject obvious junk before the WebSocket upgrade while
        still admitting a well-formed-but-unknown token far enough to be
        resolved (and then closed) — the authoritative check is the database
        lookup.
        """
        return token is not None and token.startswith(f"{ENROLMENT_PREFIX}.")

    @staticmethod
    def mint_refresh_token() -> tuple[str, str]:
        """Mints a refresh token: `(plaintext, sha256_hex)`.

        Only the hash is ever stored; the plaintext is returned to the client
        once at login/rotate and cannot be recovered.
        """
        token = f"{REFRESH_PREFIX}.{secrets.token_urlsafe(32)}"
        return token, AuthManager.hash_refresh_token(token)

    @staticmethod
    def hash_refresh_token(token: str) -> str:
        """The sha256 hex digest stored for, and looked up by, a refresh token."""
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def looks_like_refresh(token: str | None) -> bool:
        """Whether a token is shaped like a refresh token, without a lookup."""
        return token is not None and token.startswith(f"{REFRESH_PREFIX}.")

    @staticmethod
    def hash_admin_password(password: str) -> str:
        """Bcrypt-hashes an admin password for `ADMIN_PASSWORD_HASH`.

        Generate one with:

            uv run python -c "from app.auth.manager import AuthManager; print(AuthManager.hash_admin_password('...'))"
        """
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_admin_password(password: str, password_hash: str) -> bool:
        if not password_hash:
            return False
        try:
            return bcrypt.checkpw(password.encode(), password_hash.encode())
        except ValueError:
            return False

    @staticmethod
    def _sign(payload: bytes) -> str:
        secret = settings.SESSION_SIGNING_SECRET or LOCAL_OPERATOR_TOKEN
        digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    @staticmethod
    def issue_session_token(name: str, ttl_seconds: int | None = None) -> str:
        """Mints a signed, expiring session token naming an admin principal."""
        ttl = settings.SESSION_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        expires_at = int(time.time()) + ttl
        payload = f"{name}:{expires_at}".encode()
        payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
        signature = AuthManager._sign(payload)
        return f"{SESSION_PREFIX}.{payload_b64}.{signature}"

    @staticmethod
    def verify_session_token(token: str) -> Principal | None:
        """Verifies a session token's signature and expiry, constant-time.

        Returns `None` on any malformed, tampered, unsigned-for, or expired
        token rather than raising, so `resolve_principal` can try it as one
        option among several credential kinds.
        """
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != SESSION_PREFIX:
            return None
        _, payload_b64, signature = parts
        try:
            payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        except Exception:  # noqa: BLE001 - any decode failure means "not a valid token"
            return None
        expected_signature = AuthManager._sign(payload)
        if not hmac.compare_digest(signature.encode(), expected_signature.encode()):
            return None
        try:
            name, _, expires_raw = payload.decode().rpartition(":")
            expires_at = int(expires_raw)
        except (ValueError, UnicodeDecodeError):
            return None
        if not name or time.time() >= expires_at:
            return None
        return AuthManager.admin_principal(name=name)

    @staticmethod
    def admin_auth_enabled() -> bool:
        """Whether the HTTP API requires admin credentials at all.

        `ADMIN_AUTH` unset resolves to `disabled` under `APP_ENV=local` and
        `enabled` everywhere else, matching `app.config.settings.resolved_admin_auth`.
        Disabling it never touches the hub: `resolve_operator` has no such switch.
        """
        mode = settings.ADMIN_AUTH or ("disabled" if settings.APP_ENV == "local" else "enabled")
        return mode == "enabled"

    @staticmethod
    def resolve_principal(token: str | None) -> Principal:
        """Resolves a bearer token to a caller identity for the HTTP API.

        Tries the operator token, then an admin session token. If neither
        matches and admin auth is disabled (see `admin_auth_enabled`), the
        caller is treated as the console/CLI operating behind a trusted
        perimeter (no ingress, `kubectl port-forward` only) and resolves to an
        admin principal. Otherwise raises `NotAuthorized` — this function never
        returns a fabricated identity for a bad or missing credential.
        """
        operator = AuthManager.resolve_operator(token)
        if operator is not None:
            return operator

        if token:
            session = AuthManager.verify_session_token(token)
            if session is not None:
                return session

        if not AuthManager.admin_auth_enabled():
            return AuthManager.admin_principal()

        raise NotAuthorized("missing or invalid credentials")


STATIC_OPERATOR = AuthManager.operator_principal()
"""Convenience constant for tests and fixtures that need *an* operator
principal without going through token resolution.
"""
