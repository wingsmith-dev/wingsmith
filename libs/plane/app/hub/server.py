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
from app.hub.sink import NullSink
from app.core.auth import bearer_token
from app.core.auth import resolve_principal
from app.hub.state import SessionRegistry
from app.hub.session import OperatorSession


logger = getLogger(__name__)

REGISTRY = SessionRegistry()
"""Sessions the hub can resume, shared by every connection this process serves."""

SINK = NullSink()
"""Where inbound frames go until the ingestion track supplies a database-backed `Sink`."""


def _process_request(connection: ServerConnection, request: Request) -> Response | None:
    """Answers plain HTTP on /health so the hub is probeable without a socket."""
    if request.path == "/health":
        return connection.respond(http.HTTPStatus.OK, "ok\n")
    if request.path != settings.HUB_PATH:
        return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")
    return None


async def _handler(connection: ServerConnection) -> None:
    principal = resolve_principal(bearer_token(connection.request.headers.get("Authorization")))
    await OperatorSession(connection, principal, registry=REGISTRY, sink=SINK).run()


async def serve_forever(stop: asyncio.Future[None] | None = None) -> None:
    """Runs the hub until `stop` resolves, then closes every session and the pool."""
    loop = asyncio.get_running_loop()
    if stop is None:
        stop = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))

    server: Server
    async with serve(
        _handler,
        settings.HUB_HOST,
        settings.HUB_PORT,
        process_request=_process_request,
        ping_interval=20,
        ping_timeout=20,
    ) as server:
        logger.info("hub.startup host=%s port=%s path=%s", settings.HUB_HOST, settings.HUB_PORT, settings.HUB_PATH)
        await stop
        server.close()

    await engine.dispose()
    logger.info("hub.shutdown")


def run() -> None:
    logs.configure("hub")
    asyncio.run(serve_forever())
