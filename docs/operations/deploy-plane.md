# Deploying the control plane

How to run the plane and the gateway somewhere other than your laptop. There is no Helm chart for
the plane yet (it is on the [roadmap](../roadmap.md)), so this guide describes the processes, what
each needs, and the decisions you have to make. Translate it into whatever you deploy with:
Kubernetes manifests, a chart of your own, or another orchestrator.

`compose.yml` at the repository root is a local development stack. It turns admin auth off and must
not be used for a deployment.

## What you run

| Process | Image | Command | Listens on | Replicas |
|---|---|---|---|---|
| migrate | `plane` | `migrate upgrade head` | nothing; runs to completion | one, before each rollout |
| api | `plane` | `api` | `:8000` HTTP: the API, `/health`, `/metrics`, and the console bundle at `/` | one or more |
| hub | `plane` | `hub` | `:8001` WebSocket at `/v1/hub/sync`, `/health` | one or more |
| reactor | `plane` | `python -m app.reactor` | nothing | optional, see below |
| gateway | `gateway` | (default) | `:8080` internal, `:8443` tunnel, `:9090` metrics, optional public | one or more |

The reactor runs inside the hub by default. To run it separately, set
`HANGAR_REACTOR_ENABLED=false` on the hub and run the reactor process on its own. A lease in
Postgres means running more than one is wasteful but safe.

The hub can run more than one replica without sticky sessions. A reconnect that lands on a
different pod costs that cluster a full resync rather than a resume
([ADR 0005](../decisions/0005-the-hub-scales-by-shedding-session-state.md)).

Each cluster also needs the `operator` and `agent` images, which the
[`cluster-setup` chart](../../libs/chart/cluster-setup/README.md) installs.

## Prerequisites

- **Your own container registry.** No images are published yet. Build and push every image from a
  checkout:

  ```sh
  export HANGAR_REGISTRY=registry.example.com/hangar
  just push plane
  just push gateway
  just push operator
  just push agent
  HANGAR_CHART_OCI=oci://registry.example.com/hangar/charts just chart-push
  ```

  Images are built for `linux/amd64` unless `HANGAR_PLATFORM` says otherwise. Tags default to the
  short commit hash.
- **PostgreSQL**, version 17 is what the project tests against. Run it outside the plane's own
  deployment: a managed database, or an operator such as CloudNativePG. The database is the only
  copy of everything the plane has been told: clusters, enrolment token hashes, workloads,
  endpoints, tenants, quotas. **Backups are required.** Without them, losing the database means
  re-registering every cluster and re-declaring every workload. Use point-in-time recovery if your
  platform offers it, and test a restore.
- **An ingress or load balancer** for three things, covered under Networking below.
- **TLS certificates** for the plane's host and the gateway's tunnel host.
- **A metrics store** that accepts Prometheus `remote_write`, if you want metrics in the console.
  Optional: without one the console shows inventory only.

## Configuration

Every plane setting is an environment variable with a `HANGAR_` prefix; the full table is in
[`libs/plane/README.md`](../../libs/plane/README.md). The gateway's are in
[`libs/gateway/README.md`](../../libs/gateway/README.md). The ones a deployment must decide, for the
plane:

| Variable | Set on | What |
|---|---|---|
| `HANGAR_DATABASE_URL` | migrate, api, hub, reactor | `postgresql+psycopg://user:password@host:5432/db` |
| `HANGAR_APP_ENV` | all plane processes | `production` (the image default). Never `local` outside a laptop |
| `HANGAR_ADMIN_PASSWORD_HASH` | api | bcrypt hash of the admin password, see below |
| `HANGAR_SESSION_SIGNING_SECRET` | api | random secret that signs admin sessions |
| `HANGAR_GATEWAY_SERVICE_TOKEN` | api, gateway | random secret the gateway authenticates to the plane with |
| `HANGAR_HUB_PUBLIC_URL` | api | the `wss://` URL clusters reach the hub on; written into each cluster's setup values. Required outside `local`; `HANGAR_PUBLIC_HOST` does not stand in for it |
| `HANGAR_TUNNEL_GATEWAY` | api | `host:port` of the gateway's tunnel listener, written into setup values |
| `HANGAR_OPERATOR_IMAGE` / `HANGAR_AGENT_IMAGE` | api | the images clusters install, pinned to a tag |
| `HANGAR_SETUP_CHART_REF` / `HANGAR_SETUP_CHART_VERSION` | api | where clusters pull `cluster-setup` from |
| `HANGAR_SETUP_CHART_PRIVATE` | api | `true` if that chart registry needs a `helm registry login`, which adds the login step to every cluster's setup instructions |
| `HANGAR_IMAGE_PULL_SECRET_*` | api | only if your registry is private |
| `HANGAR_ALLOWED_ORIGINS` | api | only if the console is served from a different origin |

The plane refuses to start outside `local` when a required secret is missing, so a misconfigured
deployment fails at boot rather than running open.

To generate the admin password hash with the image you built:

```sh
docker run --rm -it registry.example.com/hangar/plane:<tag> python -m app.auth hash
```

For the gateway:

| Variable | What |
|---|---|
| `HANGAR_PLANE_URL` | the plane api's base URL as the gateway reaches it, e.g. the api's in-cluster Service. Required |
| `HANGAR_GATEWAY_SERVICE_TOKEN` | the same value as on the api. Required: the gateway exits at boot without this and `HANGAR_PLANE_URL` |
| `HANGAR_GATEWAY_REPLICAS` | the replica count, see Networking below |
| `HANGAR_GATEWAY_TUNNEL_TLS_CERT` / `_KEY` | PEM pair the tunnel listener terminates TLS with |
| `HANGAR_GATEWAY_PUBLIC_LISTEN` | address of the public, tenant-identified listener; off unless set |
| `HANGAR_GATEWAY_CREDENTIALS` | optional path to a mounted JSON file of `{"<credential id>": {"<header>": "<value>"}}`, for clusters whose serving address sits behind a credential the plane refers to by id. Leave it unset when no serving address does |

Human access is a single admin role; see [SECURITY.md](../../SECURITY.md) for the full auth model.

## Networking

| Path | Who connects | How to expose it |
|---|---|---|
| plane HTTP (`api`, `:8000`) | the console in a browser, `hgr` | HTTPS ingress. Or keep it private and use `kubectl port-forward` |
| plane metrics (`api`, `:8000`, `/metrics`) | your Prometheus | Scrape each api pod on the cluster network. `/metrics` needs no credential, so refuse it at the ingress that serves the plane HTTP path |
| hub (`:8001`, `/v1/hub/sync`) | every cluster's operator | HTTPS ingress that supports WebSocket upgrades and long-lived connections; raise idle timeouts well above a minute |
| gateway tunnel (`:8443`) | every cluster's agent | TCP passthrough (an L4 load balancer or an ingress TCP route), with TLS terminated by the gateway itself via `HANGAR_GATEWAY_TUNNEL_TLS_CERT`/`_KEY`. The tunnel is HTTP/2 after a custom handshake, so an HTTP ingress cannot proxy it |
| gateway internal (`:8080`) | your own services that call models | ClusterIP only. It has no authentication and reaches every enabled tenant's models, so restrict it with a NetworkPolicy and never give it an ingress |
| gateway public (`HANGAR_GATEWAY_PUBLIC_LISTEN`) | tenants calling directly | HTTPS ingress on a wildcard host, one subdomain per tenant. Off unless set |

Clusters only make outbound connections, to the hub and to the tunnel listener. The plane and the
gateway never connect into a cluster.

The gateway runs as a StatefulSet or similar with stable identities. Set `HANGAR_GATEWAY_REPLICAS`
to the replica count, because agents use it to know how many tunnels to hold. A replica without a
tunnel to a cluster cannot serve that cluster's models.

## The gateway's dependency on the plane

The gateway is the one Hangar process in the inference request path, and it is a single
deployment. Plan for both facts:

- **A running gateway survives a plane outage.** It keeps serving from the routing and tenant tables
  it last loaded, and accepts agent reconnects for tokens it verified within the last hour.
- **A gateway that starts during a plane outage does not come up.** It refuses to start without a
  routing table, so a restart, eviction or scale-up while the plane is down leaves that replica
  out. Keep the plane's availability in mind when you roll the gateway, and run more than one
  replica with a PodDisruptionBudget.
- **The gateway is one failure domain for all inference.** If every replica is down, every model is
  unreachable through Hangar, in every region. Spread replicas across zones. Regional gateways are
  on the [roadmap](../roadmap.md).

## Metrics (optional)

The api process serves its own request metrics at `/metrics`, unauthenticated, on the same port as
the API. Keep that path off the public listener, as in the networking table above.
[METRICS.md](../../libs/plane/METRICS.md) lists the series. The rest of this section is about the
fleet's metrics.

Set `HANGAR_METRICS_REMOTE_WRITE_URL` to your store's `remote_write` endpoint, and the cluster setup
values will turn on each cluster's metrics agent pointed at it. Set `HANGAR_METRICS_SOURCES` to
`default=<query url>` so the plane can read the store back for the console.

If your write endpoint sits behind an ingress with basic auth, the reactor can keep its htpasswd
Secret in step with the enrolled clusters
([ADR 0012](../decisions/0012-the-plane-is-the-source-of-the-metrics-write-htpasswd.md)). Set
`HANGAR_REMOTE_WRITE_AUTH_ENABLED=true`, set `HANGAR_REMOTE_WRITE_AUTH_NAMESPACE` and
`HANGAR_REMOTE_WRITE_AUTH_SECRET` to the Secret your ingress reads, and give the plane's service
account a Role that can read and update that one Secret. The `cluster` label on written series is
not authenticated; see the known limitations in [SECURITY.md](../../SECURITY.md).

## Upgrades

1. Build and push new images with a new tag.
2. Run `migrate upgrade head` with the new image and wait for it to finish.
3. Roll the hub, the api, the reactor if separate, and the gateway.
4. Update `HANGAR_OPERATOR_IMAGE`, `HANGAR_AGENT_IMAGE` and the chart version, then run
   `helm upgrade` on each cluster with its regenerated values.

Pre-release, migrations are not guaranteed to be reversible. Take a database backup before step 2.
