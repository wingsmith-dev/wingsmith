class PlaneError(Exception):
    """Base class for errors raised by the plane."""


class NotAuthorized(PlaneError):
    pass


class NotFound(PlaneError):
    pass


class Conflict(PlaneError):
    pass
