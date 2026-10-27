"""Serving the console from the API process, so the plane is one thing to reach.

The console is a browser application: wherever its bundle is served from, the
browser still calls the API directly, so putting it behind its own origin buys
no isolation and costs a CORS list. Serving it here collapses both surfaces
onto one port, which is what makes `kubectl port-forward` a complete access
story: one forward, a relative API base URL, and no origin to keep in sync.

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

from app.core.params import ACCEPTS_ANY_QUERY


STATIC_DIR = pathlib.Path(__file__).parent / "static"

RESERVED_PREFIXES = {"v1", "health", "schema"}
"""First path segments the API owns. The SPA route is a catch-all, so without
this an unmatched `/v1/...` would answer with `index.html` and a 200: a
client would parse the console's HTML as a response body instead of seeing the
404 the API meant to send.
"""


CSP_DIRECTIVES = (
    "default-src 'self'",
    "script-src 'self'",
    # Inline style: toast and popover libraries insert <style> elements at runtime.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
)
CONTENT_SECURITY_POLICY = "; ".join(CSP_DIRECTIVES)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
"""Sent with every file the console route serves.

The console holds an admin session in the browser, so it gets the headers a
page holding a credential should: no framing (clickjacking), no script from
anywhere but this origin, no MIME sniffing of a bundle file into something
executable, and no URL (which can carry a cluster or tenant name) leaked in
`Referer`. `connect-src 'self'` holds because, served from here, the console
calls the API on its own origin.
"""


def has_console() -> bool:
    return (STATIC_DIR / "index.html").is_file()


def _file(path: pathlib.Path, media_type: str | None) -> File:
    return File(path=path, media_type=media_type, content_disposition_type="inline", headers=SECURITY_HEADERS)


def _index() -> File:
    return _file(STATIC_DIR / "index.html", "text/html")


class ConsoleRoutes(Controller):
    """Serves the built console, falling back to `index.html` for client routes."""

    tags = ["Console"]
    include_in_schema = False

    @get(["/", "/{path:path}"], sync_to_thread=False, opt={ACCEPTS_ANY_QUERY: True})
    def console(self, path: str = "") -> File:
        """A query string here belongs to the client-side router, so it is not checked."""
        normalized = path.strip("/")
        if normalized.split("/", 1)[0] in RESERVED_PREFIXES:
            raise NotFoundException()
        if not normalized:
            return _index()
        target = (STATIC_DIR / normalized).resolve()
        # A path parameter reaches here as whatever the client sent, so `..` segments
        # are resolved before the file is opened and anything landing outside the
        # bundle is treated as unknown, not served.
        if not target.is_relative_to(STATIC_DIR.resolve()):
            return _index()
        # The router is client-side, so a path like /clusters/abc names a page the
        # bundle can render but no file that was ever built. Anything that is not a
        # real asset is therefore the SPA's problem, not a 404.
        if not target.is_file():
            return _index()
        media_type, _ = mimetypes.guess_type(str(target))
        return _file(target, media_type)
