# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability reporting on the
[`hangar-dev/hangar`](https://github.com/hangar-dev/hangar/security/advisories/new) repository
(the **Security** tab, then **Report a vulnerability**). Please do not open a public issue, pull
request or discussion for a security problem.

Include what you can of: the component and commit, the configuration involved, steps to reproduce,
and what an attacker gains. A maintainer will acknowledge the report, keep you updated in the
advisory thread, and credit you in the advisory unless you ask not to be.

## Supported versions

Hangar is pre-release. There are no tagged releases yet, so only the `main` branch receives
security fixes. Once releases exist, this section will say which ones are supported.

## Security model

### Components and trust boundaries

| Component | Runs | Trusts |
|---|---|---|
| plane `api` | central, behind your ingress | callers presenting a valid admin session, tenant credential or gateway service token |
| plane `hub` | central, behind your ingress | operators presenting a per-cluster enrolment token |
| plane `reactor` | central, no listener | the database |
| Postgres | central | the plane processes only; it holds all declared state and every credential hash |
| `gateway` | central, in the request path | see "Gateway listeners" below |
| `operator`, `agent` | one each per cluster | the plane and gateway they are configured to dial |

Clusters only dial out: the operator to the hub over a WebSocket, the agent to the gateway over a
TLS tunnel. Neither the plane nor the gateway ever opens a connection into a cluster or holds a
kubeconfig.

### Principals

The plane resolves every request to one of four principal kinds. There is no roles or scopes
system beyond these.

- **admin**: the single human operator of the installation, using the console or `hgr`. Signs in
  with a password (`POST /v1/auth/login`) and receives a short-lived signed session token plus a
  server-side refresh token. Hangar assumes one operator, or a small team sharing that role; it has
  no per-user accounts, SSO or audit trail by person. Admin auth can be turned off with
  `ADMIN_AUTH=disabled`, which makes every HTTP request an admin. Only do that for an API that is
  reachable solely through `kubectl port-forward`.
- **tenant**: a tenant's own credential, limited to that tenant's own resources.
- **operator**: a cluster's operator, authenticated by that cluster's enrolment token. Each token is
  minted per cluster (`POST /v1/clusters/{ref}/enrolment-token`) and binds the session to that one
  cluster: an operator can only write observed state for the cluster its token names. Operator
  tokens are accepted by the hub only and refused on the HTTP API. There is no shared fleet-wide
  operator token.
- **gateway**: a service principal for the gateway, configured as `HANGAR_GATEWAY_SERVICE_TOKEN`
  on both the plane and the gateway. It is accepted on exactly three routes,
  `GET /v1/gateway/tenants`, `GET /v1/endpoints/resolved` and `POST /v1/clusters/tunnel-auth`, and
  has no admin rights.

Outside `HANGAR_APP_ENV=local`, the plane refuses to start without the session signing secret and
admin password hash (when admin auth is enabled), so a misconfigured deployment fails closed.

### Gateway listeners

The gateway decides trust by which listener accepted a connection
([ADR 0016](docs/decisions/0016-the-listener-is-the-guard-tenant-identity-replaces-the-shared-token.md)):

- **The internal listener** has no authentication by design. Reaching it is the proof of trust, so
  it must never be exposed outside your own network. Run it as a ClusterIP Service with no ingress,
  and restrict which pods can reach it with a NetworkPolicy.
- **The public listener** (off unless `HANGAR_GATEWAY_PUBLIC_LISTEN` is set) identifies the tenant
  from the `Host` header and, for tenants that declare `auth: api_key`, requires one of that
  tenant's keys. Another tenant's model reads as `404`.
- **The tunnel listener** accepts agent dials. Each agent presents its cluster's enrolment token,
  which the gateway verifies with the plane before carrying any traffic.

The gateway does not keep request or response bodies in its logs. See
[`libs/gateway/README.md`](libs/gateway/README.md) for what it records.

## Known limitations

These are known and documented rather than fixed. Take them into account when you deploy.

- **The metrics `cluster` label is not authenticated.** Clusters push metrics with `remote_write`,
  and a cluster holding valid write credentials can write series under any `cluster` label,
  including the gateway's. Consumers must not treat that label as an authenticated claim
  ([ADR 0012](docs/decisions/0012-the-plane-is-the-source-of-the-metrics-write-htpasswd.md)). In
  practice an enrolled cluster could distort another cluster's dashboards or the gateway figures a
  tenant's service report is built from. A label-enforcing write proxy is on the
  [roadmap](docs/roadmap.md).
- **The gateway's internal listener needs a NetworkPolicy.** Without one, any pod in the cluster
  that can reach the Service can send unauthenticated inference requests on behalf of any tenant
  that declares `auth: none`.
- **`compose.yml` is for local development only.** It runs with `HANGAR_APP_ENV=local`, which turns
  admin auth off and relaxes the credentials the plane requires at boot. Never expose it to a
  network.
- **Human access is a single admin role.** Anyone with the admin password can do everything. Roles,
  scopes and SSO are on the [roadmap](docs/roadmap.md), not built.
