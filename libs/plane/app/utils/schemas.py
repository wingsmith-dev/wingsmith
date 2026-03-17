from typing import Any

from msgspec import UNSET
from msgspec import Struct as MsgspecStruct
from msgspec import UnsetType
from msgspec import structs


__all__ = ["UNSET", "Struct", "UnsetType"]


class Struct(MsgspecStruct, forbid_unknown_fields=True, kw_only=True):
    """Base schema type for every request and response body."""

    def to_dict(self, exclude_unset: bool = False) -> dict[str, Any]:
        data = structs.asdict(self)
        if exclude_unset:
            return {k: v for k, v in data.items() if v is not UNSET}
        return data
