"""Which metric store answers for which cluster.

The reference fleet settles this. `main-cluster/inference` keeps Prometheus
*inside* each cluster — `us.yml` says this cluster still stores its own metrics
and will hand them off once `remote_write` lands — and neither cluster sets
`externalLabels`. So there is no single store to query with a `cluster="us"`
selector, and pretending otherwise would silently blend two fleets into one
number. The mapping is therefore one source per cluster, and a cluster with no
source configured reports that plainly rather than reporting zero.

Configuration is an environment variable for now, by intent: the shape of this
is still moving, and a column would have to be migrated every time it does. The
`Cluster` model is also emphatic that the plane never dials a cluster — its
identity is the credential its agent presents, not a URL. A metrics store is a
different thing from a cluster agent and dialling it breaks no invariant, but it
does not belong on that table either.

    HANGAR_METRICS_SOURCES="us=https://prom.us.example:9090,ap=https://prom.ap.example:9090"

Keys match `Cluster.name` or `Cluster.uid`. A bearer token may be attached
per source with `|token=...`; it is never returned to a client.
"""

from dataclasses import dataclass

from app.config.settings import settings


@dataclass(frozen=True)
class Source:
    cluster: str
    url: str
    kind: str = "prometheus"
    token: str = ""

    def public(self) -> dict:
        """What a client is allowed to know: where it points, never how it authenticates."""
        return {"cluster": self.cluster, "url": self.url, "kind": self.kind}


def parse(spec: str) -> dict[str, Source]:
    """Reads `cluster=url[|token=...][|kind=...]` pairs, comma separated."""
    sources: dict[str, Source] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        cluster, _, rest = entry.partition("=")
        cluster = cluster.strip()
        if not cluster:
            continue
        parts = rest.split("|")
        url = parts[0].strip().rstrip("/")
        if not url:
            continue
        options = {
            k.strip(): v.strip()
            for k, _, v in (p.partition("=") for p in parts[1:])
            if k.strip()
        }
        sources[cluster] = Source(
            cluster=cluster,
            url=url,
            kind=options.get("kind", "prometheus"),
            token=options.get("token", ""),
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
