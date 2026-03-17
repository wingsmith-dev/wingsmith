from app.utils.schemas import UNSET
from app.utils.schemas import Struct
from app.utils.schemas import UnsetType


__all__ = ["UNSET", "ErrorResponse", "ListResponse", "Struct", "UnsetType"]


class ListResponse[T](Struct):
    """The envelope every list response is wrapped in.

    Never a bare array. `count` is the size of this page; an array cannot grow a
    cursor or a total later without breaking every client that already reads it.
    """

    items: list[T]
    count: int
    next_cursor: str | None = None


class ErrorResponse(Struct):
    code: str
    message: str
    detail: dict = {}
