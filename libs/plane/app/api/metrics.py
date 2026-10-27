"""The API process's own Prometheus metrics, served at `GET /metrics`.

This is the plane measuring itself: requests, latency and the database pool of
the process that answers the scrape. It is not `/v1/metrics`, which reads the
fleet's metrics back out of a store. `libs/plane/METRICS.md` lists every series.

Like `/health`, the route is outside `/v1` and needs no credential, so a
Prometheus in the same cluster can scrape the pod directly. Labels carry route
templates (`/v1/clusters/{ref}`), never a raw path, so no object id or name
appears in a series and the label set stays bounded by the route table.
"""

import time

from importlib import metadata

from litestar import Response
from litestar import Controller
from litestar import get
from litestar.types import Send
from litestar.types import Scope
from litestar.types import ASGIApp
from litestar.types import Message
from litestar.types import Receive
from prometheus_client import CONTENT_TYPE_LATEST
from prometheus_client import Gauge
from prometheus_client import Counter
from prometheus_client import Histogram
from prometheus_client import GCCollector
from prometheus_client import ProcessCollector
from prometheus_client import CollectorRegistry
from prometheus_client import PlatformCollector
from prometheus_client import generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from app.db import engine as db_engine
from app.core.params import ACCEPTS_ANY_QUERY


UNMATCHED = "unmatched"
"""The `route` label when a request reached the middleware without a template."""

REGISTRY = CollectorRegistry(auto_describe=True)
"""Our own registry, not the client's global one, so importing this module
twice in a test run cannot register a series twice, and so the scrape carries
exactly what is listed in `METRICS.md`."""

ProcessCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)
GCCollector(registry=REGISTRY)

REQUESTS = Counter(
    "hangar_plane_http_requests_total",
    "HTTP requests the API answered, by method, route template and status code.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
DURATION = Histogram(
    "hangar_plane_http_request_duration_seconds",
    "Time from the request reaching the API to the end of its response body, by method and route template.",
    ["method", "route"],
    registry=REGISTRY,
)
IN_FLIGHT = Gauge(
    "hangar_plane_http_requests_in_flight",
    "HTTP requests the API is answering now.",
    registry=REGISTRY,
)


def _version() -> str:
    try:
        return metadata.version("hangar-plane")
    except metadata.PackageNotFoundError:
        return "0.0.0+unknown"


class _StateCollector(Collector):
    """Read at scrape time: the build, and the database pool if this process has one.

    The pool is read from the engine already open, never by opening one, so a scrape
    before the first query reports no pool and does not connect to the database.
    """

    def collect(self):
        build = GaugeMetricFamily("hangar_plane_build_info", "The plane build answering, always 1.", labels=["version"])
        build.add_metric([_version()], 1)
        yield build

        current = db_engine._engine
        pool = current.pool if current is not None else None
        if pool is None or not hasattr(pool, "checkedout"):
            return
        connections = GaugeMetricFamily(
            "hangar_plane_db_pool_connections",
            "Connections in the API's database pool, by state.",
            labels=["state"],
        )
        connections.add_metric(["checked_out"], pool.checkedout())
        connections.add_metric(["idle"], pool.checkedin())
        yield connections
        yield GaugeMetricFamily(
            "hangar_plane_db_pool_size",
            "The pool's configured size, before overflow.",
            value=pool.size(),
        )
        yield GaugeMetricFamily(
            "hangar_plane_db_pool_overflow",
            "Connections open beyond the pool's size; negative while the pool is not yet full.",
            value=pool.overflow(),
        )


REGISTRY.register(_StateCollector())


def http_metrics(app: ASGIApp) -> ASGIApp:
    """App-wide middleware that counts and times every HTTP request a route answers.

    It runs after routing, so the route template is on the scope, and outside each
    route's exception handling, so a refused request is counted under the status it
    was answered with.
    """

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        route = scope.get("path_template") or UNMATCHED
        status = 500

        async def observed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        started = time.perf_counter()
        IN_FLIGHT.inc()
        try:
            await app(scope, receive, observed_send)
        finally:
            IN_FLIGHT.dec()
            DURATION.labels(method, route).observe(time.perf_counter() - started)
            REQUESTS.labels(method, route, str(status)).inc()

    return middleware


class PrometheusRoutes(Controller):
    tags = ["Main"]

    # A scrape job can add `params:` to the URL; they are not filters, so they must not 400.
    @get(
        "/metrics",
        media_type=CONTENT_TYPE_LATEST,
        include_in_schema=False,
        sync_to_thread=False,
        opt={ACCEPTS_ANY_QUERY: True},
    )
    def prometheus_metrics(self) -> Response[bytes]:
        """The API process's own metrics in the Prometheus text format; see `libs/plane/METRICS.md`."""
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
