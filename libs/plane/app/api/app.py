from logging import getLogger

from litestar import Litestar
from litestar.di import Provide
from litestar.config.cors import CORSConfig

from app.db import engine
from app.api import routes
from app.utils import logs
from app.core.context import request_context


logger = getLogger(__name__)


async def on_startup() -> None:
    logger.info("api.startup")


async def on_shutdown() -> None:
    await engine.dispose()
    logger.info("api.shutdown")


def create_app() -> Litestar:
    """Builds the HTTP application.

    Every handler gets a `context` dependency carrying the caller identity, so
    no route is ever written without one even while auth is stubbed.
    """
    logs.configure("api")

    cors_config = CORSConfig(
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app = Litestar(
        logging_config=None,
        cors_config=cors_config,
        on_startup=[on_startup],
        on_shutdown=[on_shutdown],
        dependencies={"context": Provide(request_context)},
    )
    return routes.init_app(app)
