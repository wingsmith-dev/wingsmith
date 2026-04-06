"""Serving the console from the API process, so the plane is one thing to reach.

The console is a browser application: wherever its bundle is served from, the
browser still calls the API directly, so putting it behind its own origin buys
no isolation and costs a CORS list. Serving it here collapses both surfaces
onto one port, which is what makes `kubectl port-forward` a complete access
story — one forward, a relative API base URL, and no origin to keep in sync.

`libs/console/Dockerfile` still builds a standalone nginx image. It is kept for
the day the console wants a CDN or an origin of its own; nothing in the default
deployment uses it.
"""

import pathlib
import mimetypes

from litestar import Controller
from litestar import get
from litestar.response import File
from litestar.exceptions import NotFoundException


STATIC_DIR = pathlib.Path(__file__).parent / "static"

RESERVED_PREFIXES = {"v1", "health", "schema"}
"""First path segments the API owns. The SPA route is a catch-all, so without
this an unmatched `/v1/...` would answer with `index.html` and a 200 — a
client would parse the console's HTML as a response body instead of seeing the
404 the API meant to send.
"""


def has_console() -> bool:
    return (STATIC_DIR / "index.html").is_file()


def _index() -> File:
    return File(path=STATIC_DIR / "index.html", media_type="text/html", content_disposition_type="inline")


class ConsoleRoutes(Controller):
    """Serves the built console, falling back to `index.html` for client routes."""

    tags = ["Console"]
    include_in_schema = False

    @get(["/", "/{path:path}"], sync_to_thread=False)
    def console(self, path: str = "") -> File:
        normalized = path.strip("/")
        if normalized.split("/", 1)[0] in RESERVED_PREFIXES:
            raise NotFoundException()
        if not normalized:
            return _index()
        target = (STATIC_DIR / normalized).resolve()
        # A path parameter reaches here as whatever the client sent, so `..` segments
        # are resolved before the file is opened and anything landing outside the
        # bundle is treated as unknown rather than served.
        if not target.is_relative_to(STATIC_DIR.resolve()):
            return _index()
        # The router is client-side, so a path like /clusters/abc names a page the
        # bundle can render but no file that was ever built. Anything that is not a
        # real asset is therefore the SPA's problem, not a 404.
        if not target.is_file():
            return _index()
        media_type, _ = mimetypes.guess_type(str(target))
        return File(path=target, media_type=media_type, content_disposition_type="inline")
