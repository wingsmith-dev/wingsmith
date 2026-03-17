from logging import getLogger

from websockets.asyncio.server import ServerConnection

from app.core.auth import Principal


logger = getLogger(__name__)


class OperatorSession:
    """One live operator connection.

    Holds the identity of the connected cluster and the socket. Framing,
    resumption cursors, backpressure and dispatch into `app.ingestion` land here
    once the wire contract in `proto/` exists.
    """

    __slots__ = ("connection", "principal")

    def __init__(self, connection: ServerConnection, principal: Principal) -> None:
        self.connection = connection
        self.principal = principal

    async def run(self) -> None:
        logger.info("hub.session.open principal=%s", self.principal.name)
        try:
            async for _message in self.connection:
                logger.debug("hub.session.message principal=%s", self.principal.name)
        finally:
            logger.info("hub.session.close principal=%s", self.principal.name)
