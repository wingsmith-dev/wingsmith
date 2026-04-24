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
├── core/           auth seam, request context, exceptions, health routes
├── db/             engine, declarative base, session provider, LISTEN/NOTIFY
├── config/         typed settings loaded from the environment
└── utils/          msgspec Struct base, logging setup
migrations/         alembic, async env
tests/
```

`app/modules/` is the only place allowed to write declared state; `app/ingestion/` is the only one
allowed to write observed state. `app/modules/placement/` writes nothing — it takes a snapshot and
returns a decision. A module whose object is **observed** (nodes, devices, topology domains,
replicas, allocations, events) is read-only: its service has no write methods at all.

## Running

Both processes read the same environment and the same database.

```bash
uv sync
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

Environment only, typed by `app/config/settings.py`. Each field reads `HANGAR_<NAME>` first and
then the bare `<NAME>`, so `DATABASE_URL` works unprefixed.

| Variable | Default |
|---|---|
| `APP_ENV` | `local` |
| `APP_DEBUG` | `false` |
| `APP_LOG_LEVEL` / `APP_LOG_FORMAT` | `INFO` / `console` (`json` in production) |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` |
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
| `HF_TOKEN` | unset |
| `OPERATOR_TOKEN` | unset — falls back to `hangar-local-operator-token` only when `APP_ENV=local` |
| `ADMIN_AUTH` | unset — resolves to `disabled` when `APP_ENV=local`, `enabled` otherwise |
| `ADMIN_PASSWORD_HASH` | unset |
| `SESSION_SIGNING_SECRET` | unset — required outside `local` when admin auth is enabled |
| `SESSION_TTL_SECONDS` | `900` |
| `ALLOWED_ORIGINS` | `http://localhost:5273,http://127.0.0.1:5273` |

The default database URL matches `compose.yml` at the repo root.

## Auth

Two principal kinds, resolved through one seam: `resolve_principal` in `app/core/auth.py`.

- **operator** — cluster agents, a static bearer token compared with `hmac.compare_digest`. The
  hub only ever accepts this; it has no disable switch.
- **admin** — humans (console, `hgr`), who `POST /v1/auth/login` with a password and get back a
  short-lived signed session token to use as the bearer credential on every subsequent request.
  The password itself is never sent again.

Outside `APP_ENV=local`, an unset `OPERATOR_TOKEN` refuses to boot rather than accept anything.
Admin auth on the HTTP API can be turned off with `ADMIN_AUTH=disabled` for a day-1 deployment
behind a perimeter with no ingress (`kubectl port-forward` only) — every request then resolves to
an admin principal, and the process logs a warning naming that assumption on every startup. Every
handler receives a `context` carrying the resolved identity, so no route is written without one.

Generate a password hash for `ADMIN_PASSWORD_HASH`:

```bash
uv run python -c "from app.core.auth import hash_password; print(hash_password('your password'))"
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
uv run mypy app
uv run pytest                       # the default suite
uv run pytest -m database           # needs Postgres; `just test-db` starts one
```
