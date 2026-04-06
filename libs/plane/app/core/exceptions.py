class PlaneError(Exception):
    """Base class for errors raised by the plane.

    `code` is the contract and is what clients branch on; `status` exists only so
    that anything speaking HTTP status codes gets the same information. The
    message is for people and is never parsed.
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
    """A request that is well-formed but asks for something incoherent.

    Writing to an observed resource is the canonical case: `POST /v1/devices` is
    not a validation failure, it is a category error, and per `libs/packages/contract/API.md`
    it answers 409 rather than 400.
    """

    code = "invalid"
    status = 409


class UnknownParameter(PlaneError):
    """A request carrying a query parameter the matched handler does not declare.

    400 rather than 409: unlike `Invalid`, nothing is being asked of the resource
    at all — the request itself is malformed, and answering it would mean
    returning a set the caller believes is filtered when it is not.
    """

    code = "unknown_parameter"
    status = 400
