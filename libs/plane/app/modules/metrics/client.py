"""A small PromQL client over `urllib`, run in a worker thread per query.

A metrics store that is down, slow or misconfigured is not a plane failure: the services catch
`MetricsUnavailable` and report its reason as `unavailable` on the answer, so the console can say
why a panel is empty instead of drawing a zero that looks like an idle GPU.
"""

import json
import asyncio
import urllib.error
import urllib.parse
import urllib.request

from app.core.exceptions import PlaneError

from .sources import Source


class MetricsUnavailable(PlaneError):
    """The metrics store could not answer. Services report it on the answer; if it escapes, it is a 503."""

    code = "metrics_unavailable"
    status = 503


def _fetch(url: str, params: dict[str, str], token: str, timeout: float) -> dict:
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


class Client:
    def __init__(self, source: Source, timeout: float) -> None:
        self.source = source
        self.timeout = timeout

    async def _call(self, path: str, params: dict[str, str]) -> dict:
        url = f"{self.source.url}/api/v1/{path}"
        try:
            payload = await asyncio.to_thread(_fetch, url, params, self.source.token, self.timeout)
        except urllib.error.HTTPError as error:
            raise MetricsUnavailable(
                f"metrics store answered {error.code}", {"cluster": self.source.cluster}
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise MetricsUnavailable(
                f"cannot reach the metrics store for {self.source.cluster}", {"reason": str(error)}
            ) from error
        except json.JSONDecodeError as error:
            raise MetricsUnavailable("metrics store returned something that is not JSON") from error
        if payload.get("status") != "success":
            raise MetricsUnavailable(payload.get("error", "query failed"))
        return payload.get("data", {})

    async def instant(self, query: str, at: float | None = None) -> list[dict]:
        params = {"query": query}
        if at is not None:
            params["time"] = f"{at:.3f}"
        data = await self._call("query", params)
        return data.get("result", [])

    async def range(self, query: str, start: float, end: float, step: str) -> list[dict]:
        data = await self._call(
            "query_range",
            {"query": query, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": step},
        )
        return data.get("result", [])
