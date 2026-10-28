from logging import getLogger
from importlib import metadata

from litestar import Litestar
from litestar.di import Provide
from litestar.config.cors import CORSConfig
from litestar.datastructures import ResponseHeader
from litestar.openapi.config import OpenAPIConfig

from app.db import engine
from app.api import routes
from app.auth import AuthManager
from app.utils import logs
from app.config import settings
from app.api.metrics import http_metrics
from app.core.params import reject_unknown_params
from app.core.context import authenticate
from app.core.context import request_context
from app.core.handlers import EXCEPTION_HANDLERS
from app.config.settings import validate


logger = getLogger(__name__)


async def on_startup() -> None:
    logger.info("api.startup")
    if not AuthManager.admin_auth_enabled():
        logger.warning(
            "api.admin_auth_disabled *** ADMIN AUTHENTICATION IS DISABLED *** "
            "every request to this API without a credential resolves to the admin "
            "principal. This is allowed under APP_ENV=local only, on a developer's "
            "machine. Set WINGSMITH_ADMIN_AUTH=enabled and WINGSMITH_ADMIN_PASSWORD_HASH "
            "to sign in locally."
        )


async def on_shutdown() -> None:
    await engine.dispose()
    logger.info("api.shutdown")


def _allowed_origins() -> list[str]:
    return [origin.strip() for origin in settings.ALLOWED_ORIGINS.split(",") if origin.strip()]


def _version() -> str:
    try:
        return metadata.version("wingsmith-plane")
    except metadata.PackageNotFoundError:
        return "0.0.0+unknown"


def _openapi_config() -> OpenAPIConfig:
    """The schema endpoints, served in every environment under `/schema`.

    The API's shape is the published contract, so the schema is public like the
    source it is generated from. It names routes and fields, never data, and
    every route it lists still answers `401` without a credential. The version
    is the package's own, so a client can tell which plane it is reading.
    """
    return OpenAPIConfig(title="Wingsmith", version=_version())


SECURITY_HEADERS = [
    ResponseHeader(name="Content-Security-Policy", value="frame-ancestors 'none'"),
    ResponseHeader(name="X-Frame-Options", value="DENY"),
    ResponseHeader(name="X-Content-Type-Options", value="nosniff"),
    ResponseHeader(name="Referrer-Policy", value="no-referrer"),
]
"""Sent on every response, the console's included. No other site may frame the
console, so a page cannot overlay it to steer an operator's clicks. These are
the headers that hold for every response; a script and style policy for the
console's bundle belongs with the bundle."""


def create_app() -> Litestar:
    """Builds the HTTP application.

    `authenticate` guards every route: anything under `/v1` other than the auth
    routes needs a credential whether or not its handler asks for `context`, and
    each principal kind is held to the routes `app.core.context.authorize` allows
    it. Handlers that want the caller take the `context` dependency.
    """
    validate(settings)
    logs.configure("api")

    # Every header `libs/packages/contract/API.md` lets a client send has to be listed. A preflight naming one
    # that is missing is answered with 400, and a 400 preflight reaches the browser as a CORS
    # error against the endpoint, so an omission here reads as "the API is unreachable" instead
    # of "this header is not allowed", and points the search at the origin, which is fine.
    cors_config = CORSConfig(
        allow_origins=_allowed_origins(),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "If-Match",
            "Idempotency-Key",
        ],
    )

    app = Litestar(
        logging_config=None,
        exception_handlers=EXCEPTION_HANDLERS,
        before_request=reject_unknown_params,
        cors_config=cors_config,
        openapi_config=_openapi_config(),
        on_startup=[on_startup],
        on_shutdown=[on_shutdown],
        guards=[authenticate],
        middleware=[http_metrics],
        response_headers=SECURITY_HEADERS,
        dependencies={"context": Provide(request_context)},
    )
    return routes.init_app(app)
