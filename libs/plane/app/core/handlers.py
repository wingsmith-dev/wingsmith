from logging import getLogger

from litestar import Request
from litestar import Response
from litestar.exceptions import HTTPException
from litestar.exceptions import ValidationException

from app.core.schemas import ErrorResponse
from app.core.exceptions import PlaneError


logger = getLogger(__name__)


def plane_error_handler(request: Request, exc: PlaneError) -> Response[ErrorResponse]:
    return Response(
        ErrorResponse(code=exc.code, message=exc.message, detail=exc.detail),
        status_code=exc.status,
    )


def validation_handler(request: Request, exc: ValidationException) -> Response[ErrorResponse]:
    return Response(
        ErrorResponse(code="invalid_request", message=str(exc.detail), detail={"extra": exc.extra or []}),
        status_code=400,
    )


# Framework errors that have a name in the contract's vocabulary.
HTTP_CODES = {
    400: "invalid_request",
    404: "not_found",
    405: "method_not_allowed",
}


def http_error_handler(request: Request, exc: HTTPException) -> Response[ErrorResponse]:
    return Response(
        ErrorResponse(code=HTTP_CODES.get(exc.status_code, "http_error"), message=exc.detail, detail={}),
        status_code=exc.status_code,
    )


def unhandled_error_handler(request: Request, exc: Exception) -> Response[ErrorResponse]:
    """Last resort. Logs the traceback and never leaks it to the caller."""
    logger.exception("api.unhandled path=%s", request.url.path)
    return Response(
        ErrorResponse(code="internal", message="internal error", detail={}),
        status_code=500,
    )


EXCEPTION_HANDLERS = {
    PlaneError: plane_error_handler,
    ValidationException: validation_handler,
    HTTPException: http_error_handler,
    Exception: unhandled_error_handler,
}
