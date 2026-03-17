# plane

Python control plane. Three concerns, deliberately kept apart:

- **Registry / intent** (`app/registry/`) — CRUD over declared objects. Should stay boring.
- **Placement** (`app/placement/`) — which cluster a workload goes to. A pure function over
  observed state, so it is testable without a cluster.
- **Ingestion** (`app/ingestion/`) — writes arriving from the hub. High frequency, idempotent,
  last-writer-wins per cluster.

Runs as two processes sharing one package and one database: the HTTP API, and the **hub** that
terminates the long-lived bidirectional operator streams. They are separate because they have
different lifecycles and different failure modes — the API is request/response, the hub holds N
persistent sessions with resumption and backpressure.

This is the skeleton. No domain objects yet; the object model lands on top of it.

## Layout

```
app/
├── api/            HTTP process — Litestar app, route registration, __main__
├── hub/            WebSocket process — operator sessions, __main__
├── registry/       declared-object CRUD modules (empty)
├── placement/      cluster selection, pure, no I/O (empty)
├── ingestion/      observed-state writes from the hub (empty)
├── core/           auth seam, request context, exceptions, health routes
├── db/             engine, declarative base, session provider
├── config/         typed settings loaded from the environment
└── utils/          msgspec Struct base, logging setup
migrations/         alembic, async env, empty revision history
tests/
```

`app/registry/` is the only package allowed to write declared state; `app/ingestion/` is the only
one allowed to write observed state. `app/placement/` writes nothing and reads nothing — it takes
a snapshot and returns a decision.

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

## Configuration

Environment only, typed by `app/config/settings.py`. Each field reads `HANGAR_<NAME>` first and
then the bare `<NAME>`, so `DATABASE_URL` works unprefixed.

| Variable | Default |
|---|---|
| `APP_ENV` | `local` |
| `APP_LOG_LEVEL` / `APP_LOG_FORMAT` | `INFO` / `console` (`json` in production) |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` |
| `HUB_HOST` / `HUB_PORT` / `HUB_PATH` | `0.0.0.0` / `8001` / `/v1/hub/sync` |
| `DATABASE_URL` | `postgresql+asyncpg://hangar:hangar@localhost:5442/hangar` |
| `OPERATOR_TOKEN` | `hangar-local-operator-token` |

The default database URL matches `compose.yml` at the repo root.

## Auth

Deliberately stubbed. There is one static bearer token and one principal; `resolve_principal` in
`app/core/auth.py` is the single seam where real credential resolution goes. Every handler still
receives a `context` carrying that identity, so no route is written without one and adding real
auth later touches no call site.

## Migrations

```bash
uv run alembic revision --autogenerate -m "..."
uv run alembic upgrade head
```

The revision history is intentionally empty. The initial schema migration belongs with the object
model, not with the skeleton. `migrations/env.py` imports `app.db.models` only if it exists.

## Checks

```bash
uv run ruff check .
uv run ruff format .
uv run mypy app
uv run pytest
```
