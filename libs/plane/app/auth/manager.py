"""Pure authentication logic: no database, no I/O.

`AuthManager` owns credential verification, token minting and verification,
and principal resolution. Every method is a staticmethod — nothing here holds
state; the only inputs are the arguments and `settings`.

Four principal kinds exist (`app.auth.schemas`): `admin`, `tenant`, `operator`
and `gateway`. The HTTP API resolves a bearer credential through
`resolve_principal`, which only ever yields an admin, a tenant or the gateway.
An operator principal comes from a per-cluster enrolment token, which the hub
resolves against the database by its hash and binds with `operator_principal`;
there is no shared operator credential.
"""

import hmac
import time
import base64
import hashlib
import secrets

import bcrypt

from app.config import settings
from app.core.exceptions import NotAuthorized

from .const import TENANT_PREFIX
from .const import REFRESH_PREFIX
from .const import SESSION_PREFIX
from .const import ENROLMENT_PREFIX
from .const import TENANT_API_KEY_PREFIX
from .const import REMOTE_WRITE_BCRYPT_ROUNDS
from .const import LOCAL_GATEWAY_SERVICE_TOKEN
from .schemas import ADMIN
from .schemas import TENANT
from .schemas import GATEWAY
from .schemas import OPERATOR
from .schemas import Principal


_LOCAL_SIGNING_SECRET = secrets.token_urlsafe(32)
"""The per-process session key used under `APP_ENV=local` when none is configured."""


class AuthManager:
    """Credential verification and token operations for every principal kind."""

    @staticmethod
    def operator_principal(cluster_uid: str, cluster_name: str) -> Principal:
        """The operator identity a hub session runs as, bound to one cluster.

        Built only from a per-cluster enrolment token the hub has resolved by its
        hash. `scope` carries the cluster's uid, and the handshake refuses a
        `hello` that names a different cluster, so the token *is* the identity
        rather than a fleet-wide pass. There is no unbound form.
        """
        if not cluster_uid:
            raise ValueError("an operator principal is always bound to a cluster")
        return Principal(
            id=cluster_uid,
            kind=OPERATOR,
            name=cluster_name or cluster_uid,
            scope=cluster_uid,
        )

    @staticmethod
    def admin_principal(name: str = "admin") -> Principal:
        return Principal(
            id="00000000-0000-0000-0000-000000000002",
            kind=ADMIN,
            name=name,
        )

    @staticmethod
    def gateway_principal() -> Principal:
        """The identity the inference gateway reads the plane as.

        It may call three routes and nothing else (`app.core.context.GATEWAY_ROUTES`),
        and holds no admin rights on the plane API.
        """
        return Principal(
            id="00000000-0000-0000-0000-000000000003",
            kind=GATEWAY,
            name="gateway",
        )

    @staticmethod
    def tenant_principal(tenant_uid: str, slug: str) -> Principal:
        """The identity a tenant's console session runs as.

        `scope` is the tenant's uid and is the whole of this principal's authority:
        it holds no wildcard, and every route outside the serving view refuses it
        (`app.core.context.authorize`). The uid rather than the slug, because a
        slug is a handle somebody reads and the scope is compared.
        """
        return Principal(
            id=tenant_uid,
            kind=TENANT,
            name=slug,
            scope=tenant_uid,
        )

    @staticmethod
    def mint_tenant_token() -> tuple[str, str]:
        """Mints a tenant console token: `(plaintext, sha256_hex)`.

        Only the hash is stored; the plaintext is shown once at mint. Fast-hashed
        for the same reason as the enrolment token — 256 bits of machine-generated
        randomness has no dictionary to walk, and the login needs one indexed
        lookup rather than a bcrypt over every live row.
        """
        token = f"{TENANT_PREFIX}.{secrets.token_urlsafe(32)}"
        return token, AuthManager.hash_tenant_token(token)

    @staticmethod
    def hash_tenant_token(token: str) -> str:
        """The sha256 hex digest stored for, and looked up by, a tenant token."""
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def mint_tenant_api_key() -> tuple[str, str]:
        """Mints a gateway inference key: `(plaintext, sha256_hex)`.

        Same shape and reasoning as `mint_tenant_token` — high-entropy,
        fast-hashed, shown once — under `TENANT_API_KEY_PREFIX` so the two
        credential kinds can never resolve against the wrong table.
        """
        key = f"{TENANT_API_KEY_PREFIX}.{secrets.token_urlsafe(32)}"
        return key, AuthManager.hash_tenant_api_key(key)

    @staticmethod
    def hash_tenant_api_key(key: str) -> str:
        """The sha256 hex digest stored for, and looked up by, a tenant API key."""
        return hashlib.sha256(key.encode()).hexdigest()

    @staticmethod
    def looks_like_tenant(token: str | None) -> bool:
        """Whether a credential is shaped like a tenant token, without a lookup.

        Lets the login route tell "this is not a tenant token" from "this token is
        not known" before it spends a query, and keeps a mistyped admin password
        out of the token table's index.
        """
        return token is not None and token.startswith(f"{TENANT_PREFIX}.")

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
    def effective_gateway_token() -> str:
        """The gateway service token this process checks against, or empty for none.

        `GATEWAY_SERVICE_TOKEN` when set; `LOCAL_GATEWAY_SERVICE_TOKEN` under
        `APP_ENV=local` only. Empty anywhere else, which resolves no gateway
        principal at all rather than matching an empty bearer.
        """
        if settings.GATEWAY_SERVICE_TOKEN:
            return settings.GATEWAY_SERVICE_TOKEN
        if settings.APP_ENV == "local":
            return LOCAL_GATEWAY_SERVICE_TOKEN
        return ""

    @staticmethod
    def resolve_gateway(token: str | None) -> Principal | None:
        """Resolves a bearer token to the gateway principal, constant-time, or `None`."""
        expected = AuthManager.effective_gateway_token()
        if not token or not expected:
            return None
        if hmac.compare_digest(token.encode(), expected.encode()):
            return AuthManager.gateway_principal()
        return None

    @staticmethod
    def resolve_operator(token: str | None) -> Principal | None:
        """Always `None`: there is no shared operator credential to resolve.

        Every operator principal is bound to one cluster by its enrolment token,
        which needs a database lookup this pure seam cannot do; the hub makes it
        (`app.hub.server._resolve_connection`). Kept because the hub's pre-upgrade
        check still calls it, so a token that is not enrolment-shaped is refused
        before the handshake.
        """
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
    def signing_secret() -> str:
        """The HMAC key session tokens are signed and verified with.

        `SESSION_SIGNING_SECRET` when set. Under `APP_ENV=local` an unset secret
        is a random key drawn once per process, so a developer's sessions end
        when the process restarts and no constant anyone can read off GitHub ever
        signs one. Anywhere else an unset secret raises: `validate` refuses to
        boot first, and a caller that skipped it must still fail closed.
        """
        if settings.SESSION_SIGNING_SECRET:
            return settings.SESSION_SIGNING_SECRET
        if settings.APP_ENV == "local":
            return _LOCAL_SIGNING_SECRET
        raise RuntimeError("SESSION_SIGNING_SECRET is unset outside APP_ENV=local")

    @staticmethod
    def _sign(payload: bytes) -> str:
        secret = AuthManager.signing_secret()
        digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    @staticmethod
    def issue_session_token(
        name: str,
        ttl_seconds: int | None = None,
        kind: str = "admin",
        scope: str | None = None,
    ) -> str:
        """Mints a signed, expiring session token naming a principal.

        The payload carries the kind and the scope because the token is the only
        thing a request presents: a session that resolved to whatever the server
        chose to assume would make the tenant view a guess rather than a claim, and
        the signature is what makes the claim safe to read back.
        """
        ttl = settings.SESSION_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        expires_at = int(time.time()) + ttl
        payload = f"{name}:{kind}:{scope or ''}:{expires_at}".encode()
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
            fields = payload.decode().split(":")
        except UnicodeDecodeError:
            return None
        if len(fields) == 2:
            # A session minted before the payload carried a kind. Admin, as it was
            # then, so that shipping this does not sign every operator out.
            fields = [fields[0], "admin", "", fields[1]]
        if len(fields) != 4:
            return None
        name, kind, scope, expires_raw = fields
        try:
            expires_at = int(expires_raw)
        except ValueError:
            return None
        if not name or time.time() >= expires_at:
            return None
        if kind == "tenant":
            if not scope:
                return None
            return AuthManager.tenant_principal(tenant_uid=scope, slug=name)
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

        Tries the gateway service token, then a session token (admin or tenant).
        If neither matches and admin auth is disabled (see `admin_auth_enabled`,
        allowed under `APP_ENV=local` only), the caller is treated as the
        console/CLI on a developer's machine and resolves to an admin principal.
        Otherwise raises `NotAuthorized` — this function never returns a
        fabricated identity for a bad or missing credential, and never an
        operator: operator credentials open hub sessions and nothing else.
        """
        gateway = AuthManager.resolve_gateway(token)
        if gateway is not None:
            return gateway

        if token:
            session = AuthManager.verify_session_token(token)
            if session is not None:
                return session

        if not AuthManager.admin_auth_enabled():
            return AuthManager.admin_principal()

        raise NotAuthorized("missing or invalid credentials")
