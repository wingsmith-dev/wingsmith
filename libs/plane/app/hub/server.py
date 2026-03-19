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
from app.core.auth import bearer_token
from app.core.auth import resolve_operator
from app.hub.state import SessionRegistry
from app.hub.state import reaper_loop
from app.ingestion import DatabaseSink
from app.ingestion import DatabaseDesiredSource
from app.hub.session import OperatorSession
from app.hub.identity import instance_id
from app.hub.ownership import DatabaseClaimStore
from app.config.settings import validate


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

INSTANCE = instance_id()
"""This process's identity in the claim table — the pod name under Kubernetes, or a random id."""


def _process_request(connection: ServerConnection, request: Request) -> Response | None:
    """Answers plain HTTP on /health and rejects the upgrade before it completes.

    The hub's operator-token check has no disable switch, unlike the API's
    admin auth — a port-forward perimeter does not protect a socket that many
    cluster agents dial in over the network, and this is the credential that
    authorises writing observed state for the whole fleet. Rejecting here,
    ahead of the WebSocket handshake, means a bad token never gets a socket at
    all, not even one that immediately closes.
    """
    if request.path == "/health":
        return connection.respond(http.HTTPStatus.OK, "ok\n")
    if request.path != settings.HUB_PATH:
        return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")
    token = bearer_token(request.headers.get("Authorization"))
    if resolve_operator(token) is None:
        return connection.respond(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")
    return None


async def _handler(connection: ServerConnection) -> None:
    principal = resolve_operator(bearer_token(connection.request.headers.get("Authorization")))
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
    ).run()


async def serve_forever(stop: asyncio.Future[None] | None = None) -> None:
    """Runs the hub until `stop` resolves, then closes every session and the pool."""
    loop = asyncio.get_running_loop()
    if stop is None:
        stop = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))

    reaper = asyncio.create_task(
        reaper_loop(REGISTRY, settings.HUB_SESSION_TTL_SECONDS, settings.HUB_REAPER_INTERVAL_SECONDS),
        name="hub-reaper",
    )

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
            "hub.startup host=%s port=%s path=%s instance=%s", settings.HUB_HOST, settings.HUB_PORT, settings.HUB_PATH, INSTANCE
        )
        await stop
        server.close()

    reaper.cancel()
    await asyncio.gather(reaper, return_exceptions=True)
    await engine.dispose()
    logger.info("hub.shutdown")


def run() -> None:
    validate(settings)
    logs.configure("hub")
    asyncio.run(serve_forever())
