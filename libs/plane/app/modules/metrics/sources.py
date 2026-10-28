"""Which metric store answers for which cluster, and under which label.

The fleet remote-writes into one central store which stamps `cluster` and
`region` on every series, and that store holds more than the inference fleet, so
a query carrying no `cluster` matcher does not read one cluster: it reads all of
them added together. Answering for a cluster is therefore two facts: an endpoint,
and the label value that cluster's series carry.

The two live in different places, because they are different kinds of fact. The
endpoint and its credential are process-level and belong to the deployment:

    WINGSMITH_METRICS_SOURCES="default=https://prom.central:9090|token=…"

The label is per-cluster and belongs to the cluster, so it is a column:
`Cluster.metrics_label`. Null there means "not observable" and is honoured as
such: no source, reported plainly. It is not defaulted at read time
to whatever the cluster happens to be called, because a guessed label returns a
clean empty that reads as a healthy idle fleet.

An env entry per cluster still parses, still means what it meant, and still
wins over the column, so a wrong or missing label is fixable from either side:

    WINGSMITH_METRICS_SOURCES="default=https://prom.central:9090,us|label=us-cluster-1"
    WINGSMITH_METRICS_SOURCES="us=https://prom.us.example:9090"

An entry with no URL of its own inherits `default`'s endpoint and carries the
`cluster` matcher it names, defaulting to its own key. An entry with a URL and no
`label=` gets no matcher at all, because a store holding one cluster has no such
label to match on: that is the per-cluster Prometheus form, kept working while
those stores are decommissioned. Adding `label=` to one moves it onto the shared
store with no other change.

Entry keys match `Cluster.name` or `Cluster.uid`. A bearer token may be attached
per source with `|token=…`; it is never returned to a client.

Nothing here reads the database. `resolve` takes an already-loaded `Cluster` so
this module stays a pure function of its inputs and the environment.
"""

from dataclasses import dataclass

from app.db.models import Cluster
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


def _entries(spec: str) -> list[tuple[str, str, dict[str, str]]]:
    """Splits the spec into `(key, url, options)` triples, dropping the malformed."""
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
    return entries


def _shared(entries: list[tuple[str, str, dict[str, str]]]) -> tuple[str, dict[str, str]] | None:
    """The `default=` entry's endpoint and options, or `None` when none is configured."""
    return next(((url, opts) for key, url, opts in entries if key == DEFAULT_KEY and url), None)


def parse(spec: str) -> dict[str, Source]:
    """Reads `cluster[=url][|label=…][|token=…][|kind=…]` entries, comma separated.

    An entry with no URL uses the `default` endpoint and must therefore be
    scoped by a label; the key stands in when none is given. An entry with a URL
    and no label keeps the pre-`remote_write` behaviour of querying a store that
    holds nothing else.
    """
    entries = _entries(spec)
    shared = _shared(entries)
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


def shared(name: str, label: str) -> Source | None:
    """The shared `default=` store narrowed to one `cluster` label.

    How a cluster that carries a `metrics_label` and no entry of its own is
    answered. `None` when no `default=` endpoint is configured, which is the same
    "no source" the caller reports for an unlabelled cluster.
    """
    endpoint = _shared(_entries(settings.METRICS_SOURCES))
    if endpoint is None:
        return None
    url, options = endpoint
    return Source(
        cluster=name,
        url=url,
        kind=options.get("kind") or "prometheus",
        token=options.get("token", ""),
        label=label,
    )


def resolve(cluster: Cluster) -> Source | None:
    """The store that answers for a cluster, and the label to narrow it by.

    An explicit env entry keyed by the cluster's name or uid wins, so a store
    still being decommissioned and a label the column has wrong are both fixable
    without a migration. Otherwise the cluster's own `metrics_label` narrows the
    shared store. A cluster with neither has no source: it is not pointed at the
    shared store under a guessed label, because a clean empty there reads as a
    healthy idle fleet.
    """
    sources = configured()
    for ref in (cluster.name, cluster.uid):
        if ref and ref in sources:
            return sources[ref]
    if not cluster.metrics_label:
        return None
    return shared(cluster.name, cluster.metrics_label)
