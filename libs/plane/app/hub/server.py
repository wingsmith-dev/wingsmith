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
from app.utils import logs
from app.config import settings
from app.core.auth import Principal
from app.core.auth import bearer_token
from app.core.auth import resolve_operator
from app.core.auth import operator_principal
from app.core.auth import hash_enrolment_token
from app.core.auth import looks_like_enrolment
from app.db.engine import session_factory
from app.hub.state import SessionRegistry
from app.hub.state import reaper_loop
from app.ingestion import DatabaseSink
from app.ingestion import DatabaseDesiredSource
from app.hub.session import OperatorSession
from app.hub.dispatch import LiveSessions
from app.hub.dispatch import listen_loop
from app.hub.identity import instance_id
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

LIVE = LiveSessions()
"""Connections this process can push declared state to right now, keyed by cluster.

Separate from `REGISTRY` on purpose: that one holds sessions that outlive their
connection, this one holds the objects that have one. See `app.hub.dispatch`.
"""

INSTANCE = instance_id()
"""This process's identity in the claim table — the pod name under Kubernetes, or a random id."""


def _process_request(connection: ServerConnection, request: Request) -> Response | None:
    """Answers plain HTTP on /health and rejects the upgrade before it completes.

    The hub's operator-token check has no disable switch, unlike the API's
    admin auth — a port-forward perimeter does not protect a socket that many
    cluster agents dial in over the network, and this is the credential that
    authorises writing observed state for the whole fleet.

    This is the cheap gate: it rejects a missing, malformed, or non-fleet token
    before the handshake, without a database lookup. A well-formed *enrolment*
    token is admitted here and resolved authoritatively (by hash, against the
    database) in `_resolve_connection`; one that resolves to no cluster gets a
    socket that closes at 4401 rather than being turned away pre-upgrade. The
    fleet token is still fully decided here.
    """
    if request.path == "/health":
        return connection.respond(http.HTTPStatus.OK, "ok\n")
    if request.path != settings.HUB_PATH:
        return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")
    token = bearer_token(request.headers.get("Authorization"))
    if resolve_operator(token) is None and not looks_like_enrolment(token):
        return connection.respond(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")
    return None


async def _resolve_connection(token: str | None) -> Principal | None:
    """The operator identity for a connecting agent, or `None` to close.

    The shared fleet token resolves synchronously to an unbound principal
    (today's behaviour). A per-cluster enrolment token is resolved against the
    database by its hash and, on a hit, binds the session to that one cluster.
    An enrolment-shaped token that resolves to nothing gets no session.
    """
    principal = resolve_operator(token)
    if principal is not None:
        return principal
    if not looks_like_enrolment(token):
        return None
    assert token is not None
    async with session_factory()() as db:
        cluster = await ClusterRepository(db).by_token_hash(hash_enrolment_token(token))
    if cluster is None:
        return None
    return operator_principal(cluster_uid=cluster.uid, cluster_name=cluster.name)


async def _handler(connection: ServerConnection) -> None:
    principal = await _resolve_connection(bearer_token(connection.request.headers.get("Authorization")))
    if principal is None:
        await connection.close(code=4401, reason="unauthorized")
        return
    await OperatorSession(
        connection,
        principal,
        registry=REGISTRY,
        sink=SINK,
        desired=DESIRED,
        claims=CLAIMS,
        instance=INSTANCE,
        live=LIVE,
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
