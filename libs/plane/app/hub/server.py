import hmac
import http
import signal
import asyncio

from logging import getLogger

from websockets.asyncio.server import Server
from websockets.asyncio.server import Request
from websockets.asyncio.server import Response
from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve

from app.db import engine
from app.auth import Principal
from app.auth import AuthManager
from app.utils import logs
from app.config import settings
from app.db.engine import session_factory
from app.hub.state import SessionRegistry
from app.hub.state import reaper_loop
from app.ingestion import DatabaseSink
from app.ingestion import DatabaseDesiredSource
from app.hub.session import SessionConfig
from app.hub.session import OperatorSession
from app.hub.dispatch import LiveSessions
from app.hub.dispatch import listen_loop
from app.hub.identity import instance_id
from app.hub.standing import DatabaseStanding
from app.reactor.loop import reactor_loop
from app.hub.ownership import DatabaseClaimStore
from app.config.settings import validate
from app.modules.clusters.repository import ClusterRepository


logger = getLogger(__name__)

REGISTRY = SessionRegistry()
"""Sessions the hub can resume, shared by every connection this process serves."""

SINK = DatabaseSink()
"""Where inbound frames land: Postgres, one transaction per frame."""

DESIRED = DatabaseDesiredSource()
"""Where a full resync is drawn from. `NullSink` and `EmptyDesiredSource` remain, for tests."""

CLAIMS = DatabaseClaimStore()
"""Durable single-writer ownership per `cluster_id` (ADR 0005). Shared across pods via Postgres,
unlike `REGISTRY`, which is deliberately not shared.
"""

STANDING = DatabaseStanding()
"""Whether a cluster may hold a session: registered, not disabled, and still on the token it opened with."""

LIVE = LiveSessions()
"""Connections this process can push declared state to right now, keyed by cluster.

Separate from `REGISTRY` on purpose: that one holds sessions that outlive their
connection, this one holds the objects that have one. See `app.hub.dispatch`.
"""

INSTANCE = instance_id()
"""This process's identity in the claim table — the pod name under Kubernetes, or a random id."""


def _process_request(connection: ServerConnection, request: Request) -> Response | None:
    """Answers plain HTTP on /health and rejects the upgrade before it completes.

    The hub's credential check has no disable switch, unlike the API's admin
    auth — a port-forward perimeter does not protect a socket that many cluster
    agents dial in over the network.

    This is the cheap gate: anything that is not shaped like a per-cluster
    enrolment token is turned away before the handshake, without a database
    lookup. That includes the shared fleet token (`OPERATOR_TOKEN`), which named
    no cluster and so let its holder write any cluster's observed state; it opens
    no session here. A well-formed enrolment token is resolved authoritatively,
    by hash, in `_resolve_connection`, and one that resolves to no cluster gets a
    socket that closes at 4401.
    """
    if request.path == "/health":
        return connection.respond(http.HTTPStatus.OK, "ok\n")
    if request.path != settings.HUB_PATH:
        return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")
    token = AuthManager.bearer_token(request.headers.get("Authorization"))
    if not AuthManager.looks_like_enrolment(token):
        return connection.respond(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")
    return None


async def _resolve_connection(token: str | None) -> tuple[Principal, str] | None:
    """The cluster-bound identity for a connecting agent and its token's digest, or `None` to close.

    Only a per-cluster enrolment token resolves. The digest travels with the
    principal so the session can notice the token being rotated while it is
    connected; whether the cluster is admitted at all (registered, not disabled)
    is the session's handshake to decide, and it asks again for as long as the
    session lives.
    """
    if not AuthManager.looks_like_enrolment(token):
        return None
    assert token is not None
    digest = AuthManager.hash_enrolment_token(token)
    async with session_factory()() as db:
        cluster = await ClusterRepository(db).by_token_hash(digest)
    if cluster is None or not hmac.compare_digest(cluster.token_hash or "", digest):
        return None
    return AuthManager.operator_principal(cluster_uid=cluster.uid, cluster_name=cluster.name), digest


def session_config() -> SessionConfig:
    """The protocol's timings, with the ones an operator of the hub can tune read from settings."""
    return SessionConfig(ownership_check_interval=settings.HUB_OWNERSHIP_CHECK_SECONDS)


async def _handler(connection: ServerConnection) -> None:
    resolved = await _resolve_connection(AuthManager.bearer_token(connection.request.headers.get("Authorization")))
    if resolved is None:
        await connection.close(code=4401, reason="unauthorized")
        return
    principal, digest = resolved
    await OperatorSession(
        connection,
        principal,
        registry=REGISTRY,
        sink=SINK,
        desired=DESIRED,
        config=session_config(),
        claims=CLAIMS,
        instance=INSTANCE,
        live=LIVE,
        standing=STANDING,
        credential=digest,
    ).run()


async def serve_forever(stop: asyncio.Future[None] | None = None) -> None:
    """Runs the hub until `stop` resolves, then closes every session and the pool.

    The reactor is started here as a co-tenant rather than as a process of its own.
    Its lease already allows exactly one worker fleet-wide, so every replica may
    start one and only the holder does any work — which makes a separate Deployment
    for a loop that ticks once a minute infrastructure nobody asked for.
    `python -m app.reactor` and `HANGAR_REACTOR_ENABLED=false` are the escape hatch.
    """
    loop = asyncio.get_running_loop()
    if stop is None:
        stop = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))

    reaper = asyncio.create_task(
        reaper_loop(REGISTRY, settings.HUB_SESSION_TTL_SECONDS, settings.HUB_REAPER_INTERVAL_SECONDS),
        name="hub-reaper",
    )
    listener = asyncio.create_task(listen_loop(LIVE), name="hub-dispatch")

    tasks = [reaper, listener]
    if settings.REACTOR_ENABLED:
        tasks.append(asyncio.create_task(reactor_loop(INSTANCE), name="hub-reactor"))

    server: Server
    async with serve(
        _handler,
        settings.HUB_HOST,
        settings.HUB_PORT,
        process_request=_process_request,
        ping_interval=20,
        ping_timeout=20,
    ) as server:
        logger.info(
            "hub.startup host=%s port=%s path=%s instance=%s",
            settings.HUB_HOST,
            settings.HUB_PORT,
            settings.HUB_PATH,
            INSTANCE,
        )
        await stop
        server.close()

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await engine.dispose()
    logger.info("hub.shutdown")


def run() -> None:
    validate(settings)
    logs.configure("hub")
    asyncio.run(serve_forever())
