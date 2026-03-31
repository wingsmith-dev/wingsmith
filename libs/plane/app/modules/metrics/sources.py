"""Which metric store answers for which cluster, and under which label.

This module used to say there was no shared store: `remote_write` had not
landed and no cluster set `externalLabels`, so the only honest mapping was one
Prometheus per cluster. That is no longer true. The fleet remote-writes into one
central store which stamps `cluster` and `region` on every series, and it holds
four clusters — two inference and two database — so a query that carries no
`cluster` matcher does not read one fleet, it reads all of them added together.

So the normal configuration is now one endpoint plus a label per cluster:

    HANGAR_METRICS_SOURCES="default=https://prom.central:9090|token=…,
                            us|label=cluster-us,
                            ap|label=cluster-ap"

`default` is the shared endpoint. An entry with no URL of its own resolves
against it and carries the `cluster` label matcher it names; with no `label=`,
the matcher is the entry's own key, which is safe rather than convenient — it
selects nothing if it is wrong, and selecting nothing is a visibly empty panel
while selecting everything is a plausible wrong number.

The per-cluster stores still exist and are being decommissioned, so the old form
still parses and still means what it meant:

    HANGAR_METRICS_SOURCES="us=https://prom.us.example:9090"

An entry with its own URL and no `label=` gets no cluster matcher, because a
store that holds one cluster has no such label to match on. Adding `label=` to
one is how it moves onto the shared store without any other change.

Keys match `Cluster.name` or `Cluster.uid`. A bearer token may be attached per
source with `|token=…`; it is never returned to a client. A cluster with no
entry has no source and reports that plainly rather than reporting zero — it is
not silently pointed at the shared store, because a wrong label there would read
as a healthy, idle fleet.

Configuration is an environment variable for now, by intent: the shape of this
is still moving, and a column would have to be migrated every time it does. The
`Cluster` model is also emphatic that the plane never dials a cluster — its
identity is the credential its agent presents, not a URL. A metrics store is a
different thing from a cluster agent and dialling it breaks no invariant, but it
does not belong on that table either.
"""

from dataclasses import dataclass

from app.config.settings import settings


DEFAULT_KEY = "default"
"""The reserved entry naming the shared store every other entry falls back to."""


@dataclass(frozen=True)
class Source:
    cluster: str
    url: str
    kind: str = "prometheus"
    token: str = ""
    label: str = ""
    """The `cluster` label value queries are narrowed by; empty means a dedicated store."""

    def public(self) -> dict:
        """What a client is allowed to know: where it points, never how it authenticates."""
        return {"cluster": self.cluster, "url": self.url, "kind": self.kind}


def _options(parts: list[str]) -> dict[str, str]:
    return {k.strip(): v.strip() for k, _, v in (p.partition("=") for p in parts) if k.strip()}


def parse(spec: str) -> dict[str, Source]:
    """Reads `cluster[=url][|label=…][|token=…][|kind=…]` entries, comma separated.

    An entry with no URL uses the `default` endpoint and must therefore be
    scoped by a label; the key stands in when none is given. An entry with a URL
    and no label keeps the pre-`remote_write` behaviour of querying a store that
    holds nothing else.
    """
    entries: list[tuple[str, str, dict[str, str]]] = []
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry:
            continue
        head, _, rest = entry.partition("|")
        cluster, _, url = head.partition("=")
        cluster = cluster.strip()
        if not cluster:
            continue
        entries.append((cluster, url.strip().rstrip("/"), _options(rest.split("|") if rest else [])))

    shared = next(((url, opts) for key, url, opts in entries if key == DEFAULT_KEY and url), None)
    sources: dict[str, Source] = {}
    for cluster, url, options in entries:
        if cluster == DEFAULT_KEY:
            continue
        if not url and shared is None:
            continue
        token = options.get("token", "")
        kind = options.get("kind", "")
        if url:
            label = options.get("label", "")
        else:
            url, inherited = shared
            label = options.get("label", cluster)
            token = token or inherited.get("token", "")
            kind = kind or inherited.get("kind", "")
        sources[cluster] = Source(
            cluster=cluster,
            url=url,
            kind=kind or "prometheus",
            token=token,
            label=label,
        )
    return sources


def configured() -> dict[str, Source]:
    return parse(settings.METRICS_SOURCES)


def resolve(*refs: str | None) -> Source | None:
    """Finds the source for a cluster by any of the names it is known by."""
    sources = configured()
    for ref in refs:
        if ref and ref in sources:
            return sources[ref]
    return None
