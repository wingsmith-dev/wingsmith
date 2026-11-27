# Running Wingsmith on a Linux host

A bare Linux GPU machine with no Kubernetes joins the plane as a cluster of kind `host` with
exactly one node. `wingsmith-host` dials out to the hub (it never listens), reports the machine
and its GPUs, and keeps a resume cursor under `/var/lib/wingsmith`.

**Status:** a host reports supply from the moment it enrols, and serves `runtime.kind: container`
workloads once serving is turned on (below). It never runs Dynamo: placement refuses Dynamo
workloads on a host with `cluster_kind`. A host that has not turned serving on reports no runtimes,
so placement refuses container workloads there with `cluster_runtime`.

## Requirements

- Linux, x86_64 or arm64, with the NVIDIA driver installed (`nvidia-smi` works).
- Outbound access to the hub's `wss://` URL.

## Enrol

1. Register the host: `POST /v1/clusters` with `{"name": "gpu-box-1", "region": "...", "kind": "host"}`.
   `kind` is fixed at registration.
2. Mint its token: `POST /v1/clusters/gpu-box-1/enrolment-token`. The plaintext is shown once.
3. On the machine:

   ```sh
   sudo wingsmith-host enrol wso1.… --plane wss://hub.example.com/v1/hub/sync --cluster gpu-box-1
   ```

   This writes the token to `/etc/wingsmith/token` (mode 0600, `--token-file` to move it) and the
   config to `/etc/wingsmith/host.json` (`--config`). `--ca-file` pins a private CA.
4. Install the example unit from `libs/operator/deploy/host/wingsmith-host.service` and run
   `systemctl enable --now wingsmith-host`.

`wingsmith-host inventory` prints what the host would report, without connecting.

## What it reports

- `cluster.observed` with `distribution: host`, and `read_only: true` with no `runtimes` until
  serving is on, then `read_only: false` and `runtimes: [container]`.
- One `node.observed`: hostname, kernel, OS, CPU, memory, driver version, and any `labels`/`pool`
  set in `host.json`. If the hostname changes, the plane replaces the node instead of adding one.
- `device.observed` per GPU and `capacity.observed`, read from NVML (go-nvml). If NVML cannot be
  loaded, it falls back to parsing `nvidia-smi -q -x`. Card names resolve to device classes through
  the same table as DCGM (the `nvml` dialect). A card with volatile uncorrectable ECC errors is
  reported unhealthy. A driver that cannot be read at all leaves the node not ready with a
  `driver_unavailable` condition.

## Serving

With serving on, `wingsmith-host run` holds two connections from one process: the control
session to the hub, and the data-path tunnel to the gateway (the same tunnel the in-cluster agent
dials). Either can drop without taking the other down, and replicas keep serving while the hub is
unreachable: the declared state is checkpointed in `/var/lib/wingsmith/desired.json`.

### Prerequisites

- Docker Engine 24 or later, with the daemon socket at `/var/run/docker.sock` (`docker_socket` to
  move it). Podman's Docker-compatible API may work and is not tested.
- The [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html),
  configured for Docker (`nvidia-ctk runtime configure --runtime=docker`, then restart Docker).
  `docker run --rm --gpus all ubuntu nvidia-smi` should list the cards. To request GPUs as CDI
  devices instead (`nvidia.com/gpu=<uuid>`), generate the CDI spec and set `"cdi": true`.
- Outbound access to the gateway's tunnel address, as well as the hub.

### Turn it on

Add to `/etc/wingsmith/host.json` (or pass `--gateway` to `enrol`) and restart the service:

```json
{
  "serve": true,
  "gateway": "tunnel.example.com:443",
  "allowed_image_prefixes": ["ghcr.io/acme/"]
}
```

`wingsmith-host run --serve` turns it on for one run without editing the file. Other keys:
`gateway_ca_file` (private CA for the tunnel), `container_image_tags_allowed` (accept images named
by tag; digests are required otherwise), `model_root` (default `/var/lib/wingsmith/models`) and
`cdi`.

`allowed_image_prefixes` fails closed: with no prefixes, every container workload is refused with
`RenderFailed`, as on the operator.

### What runs

Each replica is one container named `ws-<deployment>-<replica>`, labelled with the deployment id,
generation, serving digest and the GPUs it holds. It runs with:

- the GPUs it was granted, pinned by UUID; grants are kept in `/var/lib/wingsmith/allocations.json`
  under a lock, and rebuilt from container labels after a restart;
- the model's cache directory bind-mounted read-only at the same path as in a cluster
  (`/models/...`), with `$WS_MODEL_PATH`, `$WS_SERVED_MODEL_NAME` and `$WS_PORT` set and `$(VAR)`
  references in args expanded;
- its `http` port published on `127.0.0.1` only, on an ephemeral port;
- `no-new-privileges`, every capability dropped, a read-only root filesystem with a tmpfs `/tmp`,
  and `restart: unless-stopped`.

Readiness is the workload's `readinessProbe` (`httpGet` only; default `GET /health` on `http`), run
by `wingsmith-host` against the loopback port. The tunnel routes a request for a deployment to its
ready replicas in turn; there is no shared frontend on a host.

A change of generation, serving digest or layout replaces each replica in place (old container
removed first, since it holds the GPUs the new one needs), so a host rollout has a gap per replica.

### Weights

Prefetch on hosts is not available yet. The weights must already be in the cache, laid out as in a
cluster: a pinned Hugging Face artifact under
`/var/lib/wingsmith/models/hf/hub/models--<org>--<repo>/snapshots/<revision>` (a private one under
`p/<project>/hf/...`), any other artifact under `/var/lib/wingsmith/models/<artifact-id>`. Until
the directory exists and is not empty, the deployment stays `pending` with reason
`WeightsNotResident` and a message naming the path. One way to fill it:

```sh
HF_HOME=/var/lib/wingsmith/models/hf huggingface-cli download <org>/<repo> --revision <revision>
```

### Not supported

Multi-tenant isolation (every project's containers share the host), Dynamo, disaggregated or
multi-node replicas, rootless Podman, prefetch and eviction, and NVLink topology.
