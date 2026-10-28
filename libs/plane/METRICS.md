# Plane metrics

The `api` process serves its own Prometheus metrics at `GET /metrics` on its HTTP port (`:8000` by
default), in the Prometheus text format. This is the plane measuring itself. It is unrelated to
`/v1/metrics`, which reads the fleet's metrics back out of a metrics store
([`libs/packages/contract/API.md`](../packages/contract/API.md)).

The code is [`app/api/metrics.py`](app/api/metrics.py).

## Scraping

`/metrics` sits outside `/v1` and needs no credential, like `/health`, so a Prometheus in the same
cluster can scrape the api pods directly. Each pod reports only its own requests, so scrape every
pod, not the Service.

The series carry route templates and status codes, never object ids, names or tenant data. They do
show how much traffic each route gets. If the api is exposed through an ingress and that is more
than you want public, refuse `/metrics` at the ingress and scrape the pods on the cluster network.

The `hub` and `reactor` processes do not serve `/metrics` yet.

## Series

| Name | Type | Labels | What |
|---|---|---|---|
| `wingsmith_plane_http_requests_total` | counter | `method`, `route`, `status` | HTTP requests answered. `route` is the matched template, e.g. `/v1/clusters/{ref}`, and `status` the response code |
| `wingsmith_plane_http_request_duration_seconds` | histogram | `method`, `route` | Time from the request reaching the api to the end of its response, with the client library's default buckets (5 ms to 10 s) |
| `wingsmith_plane_http_requests_in_flight` | gauge | | Requests being answered now |
| `wingsmith_plane_build_info` | gauge | `version` | Always 1; `version` is the `wingsmith-plane` package version |
| `wingsmith_plane_db_pool_connections` | gauge | `state` (`checked_out`, `idle`) | Connections in the database pool. Absent until the process opens its pool |
| `wingsmith_plane_db_pool_size` | gauge | | The pool's configured size (`WINGSMITH_DB_POOL_SIZE`). Absent until the pool opens |
| `wingsmith_plane_db_pool_overflow` | gauge | | Connections open beyond the pool size, up to `WINGSMITH_DB_POOL_MAX_OVERFLOW`. Negative while the pool is not yet full |
| `process_*` | various | | CPU, memory, open file descriptors and start time of the process (Linux only) |
| `python_info`, `python_gc_*` | various | | Interpreter version and garbage collector counts |

Requests that match no route (a `404` before routing) are not counted, because there is no
template to label them with.

## Queries

```promql
# Error ratio by route over five minutes
sum by (route) (rate(wingsmith_plane_http_requests_total{status=~"5.."}[5m]))
  / sum by (route) (rate(wingsmith_plane_http_requests_total[5m]))

# p95 latency by route
histogram_quantile(0.95, sum by (route, le) (rate(wingsmith_plane_http_request_duration_seconds_bucket[5m])))

# A pool into its overflow: every pooled connection is in use
wingsmith_plane_db_pool_connections{state="checked_out"} >= on(job, instance) wingsmith_plane_db_pool_size
```
