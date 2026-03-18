"""Rejection of query parameters no handler declares.

Litestar ignores query parameters it does not recognise, so a misspelt or
retired filter answers `200 OK` with the whole collection and the caller has no
way to tell. That failure is silent on both sides of the wire, which makes it
the worst kind: the response looks exactly like a correct one. This turns it
into a `400` before the handler runs.

The accepted set is derived from the handler's own signature at request time —
Litestar builds a `KwargsModel` that already knows which fields resolve from the
query string, including aliases and sequence params — so there is no list to
keep in step with the routes.
"""

from typing import Any
from weakref import WeakKeyDictionary

from litestar import Request

from app.core.exceptions import UnknownParameter


_cache: WeakKeyDictionary[Any, dict[frozenset[str], frozenset[str]]] = WeakKeyDictionary()


def _accepted(handler: Any, path_params: frozenset[str]) -> frozenset[str]:
    per_handler = _cache.setdefault(handler, {})
    cached = per_handler.get(path_params)
    if cached is None:
        model = handler.create_kwargs_model(path_parameters=dict.fromkeys(path_params))
        cached = frozenset(param.field_alias for param in model.expected_query_params)
        per_handler[path_params] = cached
    return cached


async def reject_unknown_params(request: Request) -> None:
    """`before_request` hook: 400 on any query parameter the handler cannot use."""
    query = request.query_params
    if not query:
        return

    handler = request.route_handler
    if getattr(handler.fn, "__module__", "").startswith("litestar."):
        return

    accepted = _accepted(handler, frozenset(request.path_params))
    unknown = sorted(name for name in query if name not in accepted)
    if not unknown:
        return

    raise UnknownParameter(
        f"unknown query parameter(s): {', '.join(unknown)}",
        {"unknown": unknown, "accepted": sorted(accepted)},
    )
