# plane

Python control plane. Three concerns, deliberately kept apart:

- **Modules / intent** (`app/modules/`) — one package per object in the model. CRUD over declared
  objects. Should stay boring.
- **Placement** (`app/modules/placement/`) — which cluster a workload goes to. A pure function over
  observed state, so it is testable without a cluster.
- **Ingestion** (`app/ingestion/`) — writes arriving from the hub. High frequency, idempotent,
  last-writer-wins per cluster.

Runs as two processes sharing one package and one database: the HTTP API, and the **hub** that
terminates the long-lived bidirectional operator streams. They are separate because they have
different lifecycles and different failure modes — the API is request/response, the hub holds N
persistent sessions with resumption and backpressure. A third, the **reactor**, derives alerts on
its own loop under a lease.

## Layout

```
app/
├── api/            HTTP process — Litestar app, route registration, __main__
├── hub/            WebSocket process — operator sessions, __main__
├── reactor/        derived alerts, leased loop, notification channels, __main__
├── modules/        one package per object in the model; placement lives here too
├── ingestion/      observed-state writes from the hub
├── integrations/   outbound third parties — Hugging Face artifact resolution
├── auth/           principals, credentials, sessions, the auth routes, `python -m app.auth hash`
├── core/           request context and the auth guard, exceptions, throttles, health routes
├── db/             engine, declarative base, session provider, LISTEN/NOTIFY
├── config/         typed settings loaded from the environment
└── utils/          msgspec Struct base, logging setup
migrations/         alembic, async env
tests/
```

`app/modules/` is the only place allowed to write declared state; `app/ingestion/` is the only one
allowed to write observed state. Placement's decision code (`engine`, `snapshot`, `decision`)
writes nothing — it takes a snapshot and returns a decision. A module whose object is **observed**
(nodes, devices, topology domains, allocations, events) is read-only: its service has no write
methods at all.

Most modules have the same five files — `routes`, `schemas`, `services` (owns the commit),
`repository` (flushes only) and `depends` — and the exceptions are listed in
`app/modules/__init__.py`: `metrics` has no repository, and `tenants`, `serving` and `metrics`
issue some reads straight from their services.

## Running

Both processes read the same environment and the same database. `APP_ENV` defaults to
`production`, which will not boot without its secrets, so a checkout says it is local:

```bash
uv sync
export HANGAR_APP_ENV=local
uv run python -m app.api            # HTTP API on :8000
uv run python -m app.api --reload   # with autoreload
uv run python -m app.hub            # operator hub on :8001
```

`--check` on either process builds it, runs its lifecycle, and exits — enough to prove wiring
without binding a long-lived listener.

### Scaling the hub

The hub runs N replicas under Kubernetes. Per
[ADR 0005](../../docs/decisions/0005-the-hub-scales-by-shedding-session-state.md), it scales by
**shedding** session state rather than sharing it: `SessionState` — cursors and the replay buffer
— stays in one pod's memory, never in Postgres. Reconnecting to the pod you left resumes exactly;
reconnecting to a different one, which a Service makes routine, costs a `full_resync`
(`libs/packages/contract/PROTOCOL.md` §4) — expensive but always correct, never a silent gap.

What *is* shared, in Postgres, is a single fact: exactly one hub session may be the writer for a
given `cluster_id` at a time. `hello` claims that cluster with one atomic `INSERT ... ON CONFLICT
DO UPDATE` (`app/hub/ownership.py`); the session it displaces has no channel back to the one that
displaced it, so it discovers the loss itself, polling its own claim row on
`HUB_OWNERSHIP_CHECK_SECONDS`, and closes the connection the moment the row names someone else. No
sticky routing is required or assumed — correctness does not depend on the Service routing an
operator back to the same pod, only on the claim table.

A session whose cluster never reconnects is reaped after `HUB_SESSION_TTL_SECONDS` of no attached
connection (`app/hub/state.py: reaper_loop`), so an abandoned replay buffer does not sit in memory
for the life of the process. `HUB_INSTANCE_ID` names a pod in the claim table; left unset it falls
back to `$HOSTNAME` and then a random id.

## Configuration

Environment only, typed by `app/config/settings.py`. Each field reads `HANGAR_<NAME>` and nothing
else; a bare `DATABASE_URL` or `APP_ENV` is ignored.

| Variable | Default |
|---|---|
| `APP_ENV` | `production` — `local` is opt-in, and compose sets it |
| `APP_DEBUG` | `false` |
| `APP_LOG_LEVEL` / `APP_LOG_FORMAT` | `INFO` / `console` (`json` in production) |
| `API_HOST` / `API_PORT` | unset — `127.0.0.1` when `APP_ENV=local`, `0.0.0.0` otherwise / `8000` |
| `HUB_HOST` / `HUB_PORT` / `HUB_PATH` | `0.0.0.0` / `8001` / `/v1/hub/sync` |
| `HUB_INSTANCE_ID` | unset — falls back to `$HOSTNAME`, then a random id |
| `HUB_SESSION_TTL_SECONDS` | `600` |
| `HUB_REAPER_INTERVAL_SECONDS` | `60` |
| `HUB_OWNERSHIP_CHECK_SECONDS` | `10` |
| `DATABASE_URL` | `postgresql+psycopg://hangar:hangar@localhost:5442/hangar` |
| `DB_ECHO` / `DB_POOL_SIZE` / `DB_POOL_MAX_OVERFLOW` / `DB_POOL_PRE_PING` | `false` / `10` / `10` / `true` |
| `REACTOR_ENABLED` | `true` |
| `REACTOR_INTERVAL_SECONDS` / `REACTOR_LEASE_SECONDS` | `60` / `180` |
| `REACTOR_EVENT_BATCH` / `REACTOR_NOTIFY_TIMEOUT` | `500` / `5` |
| `METRICS_SOURCES` | unset — `default=<url>\|token=...`; the per-cluster label is `Cluster.metrics_label` |
| `METRICS_TIMEOUT` / `METRICS_WINDOW` / `METRICS_STEP` | `5` / `5m` / `60s` |
| `METRICS_REMOTE_WRITE_URL` | unset — where the fleet pushes; rendered into the setup values. Empty renders `monitoring.enabled: false` |
| `REMOTE_WRITE_AUTH_ENABLED` | `false` — optional: keep an htpasswd Secret for a basic-auth write receiver (ADR 0012) |
| `REMOTE_WRITE_AUTH_NAMESPACE` / `REMOTE_WRITE_AUTH_SECRET` / `REMOTE_WRITE_AUTH_KEY` | unset / `prom-remote-write-auth` / `users` |
| `SETUP_CHART_REF` / `SETUP_CHART_VERSION` / `SETUP_CHART_PRIVATE` | `oci://ghcr.io/hangar-dev/charts/cluster-setup` / unset / `false` |
| `BENCHMARK_CONFORMANCE_IMAGE` / `BENCHMARK_EVALSCOPE_IMAGE` | `ghcr.io/hangar-dev/conformance:0.1.0` / `ghcr.io/hangar-dev/evalscope:1.11.1` |
| `HF_TOKEN` | unset |
| `GATEWAY_SERVICE_TOKEN` | unset — falls back to `hangar-local-gateway-token` only when `APP_ENV=local` |
| `ADMIN_AUTH` | unset — `disabled` when `APP_ENV=local`; always `enabled` otherwise |
| `ADMIN_PASSWORD_HASH` | unset — required outside `local` |
| `SESSION_SIGNING_SECRET` | unset — required outside `local`; a random per-process key in `local` |
| `SESSION_TTL_SECONDS` / `REFRESH_TTL_SECONDS` | `900` / `604800` (absolute session lifetime) |
| `TRUSTED_PROXIES` | unset — proxies whose `X-Forwarded-For` is believed when charging failed sign-ins |
| `ALLOWED_ORIGINS` | `http://localhost:5273,http://127.0.0.1:5273` |

The default database URL matches `compose.yml` at the repo root.

## Auth

Hangar is single-operator software: there is one admin identity, and every person signed in as
admin can do everything. There are no per-user accounts, roles or scopes. What there is instead is
four principal kinds (`app/auth/schemas.py`), each held to a fixed set of routes in one place,
`authorize` in `app/core/context.py`:

| Kind | Credential | Reaches |
|---|---|---|
| `admin` | a session from `POST /v1/auth/login` with the admin password | every route except the gateway's |
| `tenant` | a session from `POST /v1/auth/login` with a tenant's console token (`hgrt1.`) | `GET /v1/serving/...` only |
| `gateway` | `GATEWAY_SERVICE_TOKEN`, a static bearer | `GET /v1/gateway/tenants`, `GET /v1/endpoints/resolved`, `POST /v1/clusters/tunnel-auth` |
| `operator` | the cluster's own enrolment token (`hgro1.`), minted at registration | the hub only, bound to that one cluster; never the HTTP API |

There is no shared operator token. Each cluster's enrolment token is stored as a hash and resolves
to an operator principal bound to that cluster, and the hub refuses a `hello` that names any other.
Rotating it (`POST /v1/clusters/{ref}/enrolment-token`) is an admin action.

Authentication is an app-wide guard, so a handler written without a `context` parameter is still
authenticated. Everything under `/v1` needs a credential except `/v1/auth/login`, `/v1/auth/refresh`
and `/v1/auth/logout`; `/health`, the OpenAPI schema at `/schema` and the console's files are
public. A missing or unknown credential is `401`; a valid credential of the wrong kind is `403`.
`tests/test_route_auth.py` walks every registered route and checks both.

**Sessions.** Login returns a short-lived access token (`hgrs1.`, `SESSION_TTL_SECONDS`) and a
refresh token (`hgrr1.`). Refreshing rotates both atomically; presenting a refresh token that was
already rotated ends the whole session, unless it was rotated in the last ten seconds, which is two
copies of one client (a duplicated tab) refreshing together: the late one gets `401` and the session
stands. A session lasts at most `REFRESH_TTL_SECONDS` from sign-in,
however often it is refreshed. The guard checks the session on every request, so
`POST /v1/auth/logout` with `{"refresh_token": ...}`, a revoked tenant token or a disabled tenant ends
access on the next request. Changing `ADMIN_PASSWORD_HASH` ends every admin session.

**Failed sign-ins** are budgeted per caller address and account (`app/core/throttle.py`), so one
caller spending its budget locks out itself, not the operator. Behind a proxy, set `TRUSTED_PROXIES`
or every caller shares the proxy's budget.

**Cross-site writes.** A `POST`, `PUT`, `PATCH` or `DELETE` whose `Origin` is neither the plane's
own nor in `ALLOWED_ORIGINS` is `403`, and a request body that is not JSON is `415`. While admin
auth is off, any request whose `Host` is not loopback, `PUBLIC_HOST` or a host in `ALLOWED_ORIGINS`
is `403` too, so a page that rebinds its own DNS name to `127.0.0.1` cannot drive the API. Every response
carries `frame-ancestors 'none'` and `X-Frame-Options: DENY`, so no other site can frame the console.

**Local mode.** `APP_ENV` defaults to `production`, so a plane started with no environment fails
closed. Under `APP_ENV=local`, which has to be asked for, `ADMIN_AUTH` defaults to `disabled`: a request with no
credential resolves to the admin principal, the API binds `127.0.0.1`, and the process logs a
warning at startup. Outside `local` the plane refuses to boot with `ADMIN_AUTH=disabled`, without
`SESSION_SIGNING_SECRET`, or without `ADMIN_PASSWORD_HASH`. Keep the admin API off the public
internet anyway: one password is one password.

Generate `ADMIN_PASSWORD_HASH` (prompts twice; `--stdin` reads one line from a pipe):

```bash
uv run python -m app.auth hash
```

## Migrations

```bash
uv run alembic revision --autogenerate -m "..."
uv run alembic upgrade head
```

`migrations/env.py` imports `app.db.models` only if it exists.

## Checks

```bash
uv run ruff check .
uv run ruff format .
uv run pytest                       # the default suite
uv run pytest -m database           # needs Postgres; `just test-db` starts one
```
