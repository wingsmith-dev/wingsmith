# gateway

Hangar's data plane. One hop: it takes an OpenAI-compatible request, reads the `model` out of the
body, and forwards it to a cluster currently serving that model.

It is the only part of hangar that sits in the request path, and it holds nothing the control plane
holds. The plane derives the routing table; the gateway keeps a copy in memory and refreshes it on
a timer. Nothing is joined per request.

## What it does not do

- **No authentication, metering, or billing.** Whoever puts a gateway in front of their users
  already has a place where that happens. Two systems counting tokens is two systems disagreeing
  about an invoice. The only check here is `HANGAR_GATEWAY_TOKEN`, and it asks one question: is
  this hop being called by the system that is supposed to call it.
- **No replica selection.** The serving runtime inside a cluster routes on live queue depth and
  KV-cache residency. That state does not leave the cluster and should not.
- **No catalog.** `GET /v1/models` lists names, not context lengths or quantization. Describing a
  model is the job of whatever is selling it.

## Configuration

| Flag / variable | What |
|---|---|
| `-listen` | listen address, default `:8080` |
| `-plane-url` / `HANGAR_PLANE_URL` | the plane's base URL |
| `HANGAR_OPERATOR_TOKEN` | how it reads the plane |
| `-resync` | how often to re-read the table, default 30s |
| `HANGAR_GATEWAY_TOKEN` | shared token callers must present; unset means the gateway trusts its network |
| `-credentials` / `HANGAR_GATEWAY_CREDENTIALS` | JSON file of `{"<credential id>": {"<header>": "<value>"}}` |

The credential file is how a reference on a cluster's serving address becomes headers on the wire.
The plane stores the reference and never the secret, so the material is mounted next to the process
that redeems it.

## Failure behaviour

- **The plane is down.** The table already loaded keeps serving. What it is built from changes on
  the order of days; a control-plane outage must not stop traffic to models that are running fine.
- **Boot with no table.** Fatal. Coming up empty would answer everything with `model_not_found`,
  which reads as an outage of the models rather than the misconfiguration it is.
- **One serving address is unusable.** Skipped, with the rest of the refresh applied. One bad URL
  must not take every other model down with it.
- **An endpoint with no upstreams.** `503 no_capacity`, never `404`. "No such model" and "this
  model has nowhere to go" are different answers and a caller acts differently on each.
- **A deployment the plane reports as not running.** Deprioritised, not excluded. Readiness is
  observed and can lag; refusing to route to a model that is in fact serving is the worse failure.
