# Demo image

The world `just demo` builds, as one container image: the fake fleet and the reset job. Plane api, hub, Postgres and the console come from their own images.

    docker build -f dev/demo/Dockerfile -t wingsmith-demo .     # context is the repo root

The image ships `fleet.py`, `seed.py`, `inference.py`, the plane's locked Python env (plus its migrations), and prebuilt `gateway`, `agent` and `wingstransit` binaries. Released as `ghcr.io/<owner>/demo:<version>`.

## Modes

The first argument picks one (default `fleet`).

- `fleet`: waits for the plane, then runs `fleet.py --serve`. Long-running. One fake operator per cluster, one gateway, and an agent plus stand-in engine per cluster, all in this one process tree. Extra args go to `fleet.py` (a cluster name, `--register`).
- `reset`: a run-to-completion job. Waits for the plane, drops and recreates schema `public`, runs `alembic upgrade head`, then `seed.py --demo`, which registers the clusters and waits for the fleet to connect before creating workloads and the endpoint.

Anything else is exec'd as a command.

## Environment

| Variable | Used by | Meaning |
| --- | --- | --- |
| `WINGSMITH_API` | both | Base URL of the plane api to write to, for example `http://plane-admin:8000`. Must accept admin writes. |
| `WINGSMITH_HUB` | fleet | Hub websocket URL, for example `ws://hub:8001/v1/hub/sync`. |
| `WINGSMITH_ADMIN_PASSWORD` | both | Admin password. The scripts sign in as `admin` and sign in again on a 401. |
| `WINGSMITH_API_TOKEN` | both | An existing admin session token; wins over the password. Not needed in practice. |
| `WINGSMITH_GATEWAY_SERVICE_TOKEN` | fleet | Token the gateway presents to the plane. Must equal the plane's `WINGSMITH_GATEWAY_SERVICE_TOKEN`. Unset means the local-only default, which a production plane rejects. |
| `WINGSMITH_DATABASE_URL` | reset | SQLAlchemy URL of the plane database, `postgresql+psycopg://user:pass@host:5442/db`. The role must be able to drop and create a schema. |
| `WINGSMITH_DEMO_WAIT` | both | Seconds to wait for `GET /health` on the plane (default 300). |
| `WINGSMITH_SEED_WAIT` | reset | Seconds `seed.py --demo` waits for every cluster to connect (default 300). |
| `WINGSMITH_FLEET_HEARTBEAT`, `_POLL`, `_STARTUP` | fleet | Fake operator timings (defaults 10, 2 and 3 seconds). |
| `WINGSMITH_DEMO_GATEWAY` | fleet | Gateway listener (image default `0.0.0.0:8080`). |
| `WINGSMITH_DEMO_TUNNEL` | fleet | Gateway tunnel listener (image default `0.0.0.0:8443`). |
| `WINGSMITH_DEMO_TUNNEL_DIAL` | fleet | Where the in-pod agents dial the tunnel (image default `127.0.0.1:8443`). |
| `WINGSMITH_DEMO_GATEWAY_METRICS` | fleet | Gateway metrics listener (image default `0.0.0.0:9090`). |
| `WINGSMITH_DEMO_BIND` | fleet | Bind address for the per-cluster engines and agent health endpoints (default `127.0.0.1`). |
| `WINGSMITH_DEMO_BIN` | fleet | Directory of prebuilt binaries; set to `/opt/bin` in the image, which skips the Go build. |
| `WINGSMITH_DEMO_LOGS` | fleet | `stdout` (set in the image) sends gateway, agent and engine output to the container log. |

## Ports

The fleet container listens on 8080 (gateway, unauthenticated internal listener), 8443 (tunnel, in-pod only), 9090 (gateway metrics), 9901 and up (one stand-in engine per cluster) and 9801 and up (agent health), the last two on loopback. The reset job listens on nothing.

## What a reset needs

- Network access to the plane at `WINGSMITH_API` and the admin password. That plane must be a writable one: if the public demo plane refuses writes from admin sessions, run the fleet and reset against a second, internal plane instance on the same database.
- Direct access to the Postgres database through `WINGSMITH_DATABASE_URL`.
- A running fleet container, since `seed.py --demo` waits for it. After the wipe the fleet's clusters disappear from the plane, the agents go back to waiting, and they re-enrol once the seed registers the clusters again.

The plane api keeps running through a reset, and admin sessions issued before it stop working because refresh tokens live in the database. Schedule it as a CronJob.
