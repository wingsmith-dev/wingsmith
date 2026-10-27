"""The htpasswd behind the metrics write ingress, rendered from the registry.

Enrolling a cluster is meant to be: register it, paste the values, `helm install`.
The last step that was not, until this existed, was a human appending a bcrypt line
to an htpasswd file in a different repository so the cluster's metrics agent could
authenticate to the central store's write ingress. A per-cluster manual step is the
one thing the enrolment flow cannot have.

So the plane is the source of that file. It already mints the credential, the
enrolment token, and it already knows the fleet, which is exactly the pair of
facts the file is made of. One line per cluster holding a bcrypt verifier:

    <cluster-name>:<bcrypt-hash>

This runs in the reactor, not at mint time. Mint time is a single
HTTP request that must not fail because the API server was briefly unavailable, and
the file is a function of the whole fleet, not of one cluster: a
reconciliation, not an event. The reactor's lease already makes exactly one process
fleet-wide do it, which is what keeps two replicas from writing one Secret.

Unconfigured, or with no in-cluster service account, the pass is a no-op that logs
once. That is the local-development and test path and it must never be a failure.
"""

from __future__ import annotations

from logging import getLogger

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Cluster
from app.integrations.kubernetes import SecretStore
from app.integrations.kubernetes import KubernetesError
from app.integrations.kubernetes import in_cluster


logger = getLogger(__name__)

_announced = False
"""Whether the "not reconciling" reason has been logged in this process.

The pass runs every tick, and a disabled reconciler that said so once a minute
forever would be noise that trains people to ignore the log it is written in.
"""


def render_htpasswd(rows: list[tuple[str, str | None]]) -> str:
    """Renders `(cluster_name, bcrypt_hash)` pairs as an htpasswd file.

    Sorted by cluster name so the same fleet always renders the same bytes: the
    comparison that decides whether to write depends on it, and a query's row order
    is not a guarantee. Clusters with no hash are omitted, not emitted empty:
    a line with no verifier is a line Traefik would try to parse, and a cluster that
    has never issued a token has no credential to authenticate with anyway.
    """
    present = [(name, digest) for name, digest in rows if digest]
    lines = [f"{name}:{digest}" for name, digest in sorted(present, key=lambda row: row[0])]
    return "".join(f"{line}\n" for line in lines)


async def htpasswd(db: AsyncSession) -> str:
    """The file the fleet's registry currently implies."""
    result = await db.execute(select(Cluster.name, Cluster.remote_write_hash))
    return render_htpasswd([(name, digest) for name, digest in result.all()])


def _skip_reason() -> str | None:
    """Why this pass is not going to do anything, or `None` if it is."""
    if not settings.REMOTE_WRITE_AUTH_ENABLED:
        return "disabled"
    if not settings.REMOTE_WRITE_AUTH_SECRET or not settings.REMOTE_WRITE_AUTH_NAMESPACE:
        return "unconfigured"
    return None


def _announce(reason: str) -> None:
    global _announced
    if not _announced:
        logger.info("reactor.remote_write.inactive reason=%s", reason)
        _announced = True


async def reconcile(db: AsyncSession, store: SecretStore | None = None) -> bool:
    """Brings the write ingress's htpasswd in line with the registry.

    Returns whether a write happened. The unchanged case is the common one, the
    fleet changes when a cluster is registered or rotates a token, which is rarely,
    so the current value is read and compared first. Patching a Secret every tick
    would churn its `resourceVersion` and, with it, anything watching the object.

    Every failure is contained: a missing Secret, an API server that refuses the
    patch, or no service account at all leaves the file as it was and logs. The next
    tick tries again, which is the reactor's contract for every pass it runs.
    """
    reason = _skip_reason()
    if reason is not None:
        _announce(reason)
        return False

    api = store if store is not None else in_cluster()
    if api is None:
        _announce("no in-cluster service account")
        return False

    namespace = settings.REMOTE_WRITE_AUTH_NAMESPACE
    name = settings.REMOTE_WRITE_AUTH_SECRET
    key = settings.REMOTE_WRITE_AUTH_KEY
    desired = await htpasswd(db)

    try:
        current = await api.read_secret_key(namespace, name, key)
        if current == desired:
            return False
        await api.patch_secret_key(namespace, name, key, desired)
    except KubernetesError as error:
        logger.warning("reactor.remote_write.failed secret=%s/%s error=%s", namespace, name, error)
        return False

    logger.info(
        "reactor.remote_write.written secret=%s/%s key=%s lines=%s",
        namespace,
        name,
        key,
        len(desired.splitlines()),
    )
    return True
