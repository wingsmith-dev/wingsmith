# Running Wingsmith on a Linux host

A bare Linux GPU machine with no Kubernetes joins the plane as a cluster of kind `host` with
exactly one node. `wingsmith-host` dials out to the hub (it never listens), reports the machine
and its GPUs, and keeps a resume cursor under `/var/lib/wingsmith`.

**Status:** supply only. The host shows up in the console and API with its devices and capacity,
and placement refuses it with `cluster_kind` ("hosts do not serve workloads yet"). Serving lands
with P2.4.

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

- `cluster.observed` with `distribution: host` and `read_only: true` (it applies nothing yet).
- One `node.observed`: hostname, kernel, OS, CPU, memory, driver version, and any `labels`/`pool`
  set in `host.json`. If the hostname changes, the plane replaces the node instead of adding one.
- `device.observed` per GPU and `capacity.observed`, read from NVML (go-nvml). If NVML cannot be
  loaded, it falls back to parsing `nvidia-smi -q -x`. Card names resolve to device classes through
  the same table as DCGM (the `nvml` dialect). A card with volatile uncorrectable ECC errors is
  reported unhealthy. A driver that cannot be read at all leaves the node not ready with a
  `driver_unavailable` condition.

## Not yet

Serving containers, the data-plane tunnel, model cache and NVLink topology are P2.4 and later.
Multi-tenant isolation on a host is not supported.
