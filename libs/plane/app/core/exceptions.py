class PlaneError(Exception):
    """Base class for errors raised by the plane.

    `code` is the contract clients branch on; `status` carries the same information for HTTP-only
    callers: 400 for a malformed request, 422 for a valid one the plane refuses, 409 only for a
    conflict with current state.
    """

    code = "error"
    status = 500

    def __init__(self, message: str = "", detail: dict | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.detail = detail or {}


class NotAuthorized(PlaneError):
    code = "not_authorized"
    status = 401


class Forbidden(PlaneError):
    code = "forbidden"
    status = 403


class NotFound(PlaneError):
    code = "not_found"
    status = 404


class Conflict(PlaneError):
    code = "conflict"
    status = 409


class Invalid(PlaneError):
    """A well-formed request that asks for something the plane will not do (422)."""

    code = "invalid"
    status = 422


class TooManyRequests(PlaneError):
    """A caller that has spent a budget the plane keeps to bound its own work (`app.core.throttle`)."""

    code = "rate_limited"
    status = 429


class UnknownParameter(PlaneError):
    """A query parameter the matched handler does not declare (400, see API.md §1)."""

    code = "unknown_parameter"
    status = 400


class InvalidParameter(PlaneError):
    """A declared query parameter carrying a value it cannot take (400)."""

    code = "invalid_parameter"
    status = 400
