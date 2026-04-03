from logging import getLogger

from litestar import Litestar
from litestar.di import Provide
from litestar.config.cors import CORSConfig

from app.db import engine
from app.api import routes
from app.utils import logs
from app.config import settings
from app.core.auth import admin_auth_enabled
from app.core.params import reject_unknown_params
from app.core.context import request_context
from app.core.handlers import EXCEPTION_HANDLERS
from app.config.settings import validate


logger = getLogger(__name__)


async def on_startup() -> None:
    logger.info("api.startup")
    if not admin_auth_enabled():
        logger.warning(
            "api.admin_auth_disabled *** ADMIN AUTHENTICATION IS DISABLED *** "
            "every request to this API resolves to a full-scope admin principal "
            "with no credential check. This is safe ONLY behind a perimeter with "
            "no ingress (kubectl port-forward only). Set HANGAR_ADMIN_AUTH=enabled "
            "and HANGAR_ADMIN_PASSWORD_HASH before this plane is ever exposed."
        )


async def on_shutdown() -> None:
    await engine.dispose()
    logger.info("api.shutdown")


def _allowed_origins() -> list[str]:
    return [origin.strip() for origin in settings.ALLOWED_ORIGINS.split(",") if origin.strip()]


def create_app() -> Litestar:
    """Builds the HTTP application.

    Every handler gets a `context` dependency carrying the caller identity,
    resolved through the single seam in `app.core.auth.resolve_principal`.
    """
    validate(settings)
    logs.configure("api")

    # Every header `contract/API.md` lets a client send has to be listed. A preflight naming one
    # that is missing is answered with 400, and a 400 preflight reaches the browser as a CORS
    # error against the endpoint — so an omission here reads as "the API is unreachable" rather
    # than "this header is not allowed", and points the search at the origin, which is fine.
    cors_config = CORSConfig(
        allow_origins=_allowed_origins(),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Hangar-Project", "If-Match"],
    )

    app = Litestar(
        logging_config=None,
        exception_handlers=EXCEPTION_HANDLERS,
        before_request=reject_unknown_params,
        cors_config=cors_config,
        on_startup=[on_startup],
        on_shutdown=[on_shutdown],
        dependencies={"context": Provide(request_context)},
    )
    return routes.init_app(app)
