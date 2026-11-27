# Running Wingsmith on a Linux host

A bare Linux GPU machine with no Kubernetes joins the plane as a cluster of kind `host` with
exactly one node. `wingsmith-host` dials out to the hub (it never listens), reports the machine
and its GPUs, and keeps a resume cursor under `/var/lib/wingsmith`.

**Status:** a host reports supply from the moment it enrols, and serves `runtime.kind: container`
workloads once serving is turned on (below). It never runs Dynamo: placement refuses Dynamo
workloads on a host with `cluster_kind`. A host that has not turned serving on reports no runtimes,
so placement refuses container workloads there with `cluster_runtime`.

## Requirements

- Linux, x86_64 or arm64, with glibc 2.28 or later (RHEL/Rocky 8+, Debian 10+, Ubuntu 20.04+) and
  the NVIDIA driver installed (`nvidia-smi` works).
- systemd.
- Docker, to serve (see [Prerequisites](#prerequisites)); a supply-only host does not need it.
- Outbound access to the hub's `wss://` URL.

## Install

Each release attaches `wingsmith-host` packages for amd64 and arm64 to its
[GitHub release](https://github.com/wingsmith-dev/wingsmith/releases), with
`wingsmith-host-SHA256SUMS`. There is no apt or rpm repository yet.

```sh
VERSION=0.1.0 ARCH=amd64   # or arm64
base=https://github.com/wingsmith-dev/wingsmith/releases/download/v$VERSION
# Debian / Ubuntu
curl -fLO "$base/wingsmith-host_${VERSION}_${ARCH}.deb"
sudo apt install "./wingsmith-host_${VERSION}_${ARCH}.deb"
# RHEL / Rocky / Fedora (the rpm names arm64 aarch64 and amd64 x86_64)
curl -fLO "$base/wingsmith-host-${VERSION}-1.x86_64.rpm"
sudo dnf install "./wingsmith-host-${VERSION}-1.x86_64.rpm"
```

The package installs `/usr/bin/wingsmith-host`, the unit
`/usr/lib/systemd/system/wingsmith-host.service` and a placeholder `/etc/wingsmith/host.json`, and
its post-install step:

- creates the `wingsmith` system user and group, and adds the user to the `docker` group if it
  exists (install Docker first, or run `usermod -aG docker wingsmith` afterwards);
- creates `/etc/wingsmith` (root:wingsmith, 0750), `/etc/wingsmith/credentials` (wingsmith, 0700)
  and `/var/lib/wingsmith` (wingsmith, 0750);
- runs `systemctl daemon-reload`. It does not enable or start the service.

### Who the agent runs as

The unit runs the agent as `wingsmith`, with the `docker` group as a supplementary group so it can
reach the Engine API socket. **Membership of the `docker` group is root-equivalent:** anything that
can talk to the Docker socket can start a privileged container with the host's filesystem mounted.
The agent is the only member the package adds; treat the host's `docker` group like root's
password. The unit's sandbox (an empty capability bounding set, a read-only `/usr` and `/etc`,
`ProtectKernel*`, `ProtectControlGroups`, `RestrictSUIDSGID`, `LockPersonality`, and device access
limited to `/dev/nvidia*` for NVML) limits what the agent process itself can do, not what the
Docker daemon does on its behalf.

Credential files (the enrolment token and `credentials/*.token`) must be regular files, mode 0600,
owned by root or by the account the agent runs as; anything else is refused. Prefetch containers
run as the agent's uid:gid so the agent can later evict what they wrote.

Podman's Docker-compatible API should work with `docker_socket` pointed at its socket, but neither
rootful nor rootless Podman is tested yet.

## Enrol

1. Register the host: `POST /v1/clusters` with `{"name": "gpu-box-1", "region": "...", "kind": "host"}`.
   `kind` is fixed at registration.
2. Mint its token: `POST /v1/clusters/gpu-box-1/enrolment-token`. The plaintext is shown once.
3. On the machine:

   ```sh
   sudo wingsmith-host enrol wso1.… --plane wss://hub.example.com/v1/hub/sync --cluster gpu-box-1
   ```

   This writes the token to `/etc/wingsmith/token` (mode 0600, `--token-file` to move it) and the
   config to `/etc/wingsmith/host.json` (`--config`). `--ca-file` pins a private CA. Run as root
   with the `wingsmith` user present, it hands the token to that user.
4. `sudo systemctl enable --now wingsmith-host`, then `journalctl -u wingsmith-host -f`.

`wingsmith-host inventory` prints what the host would report, without connecting.

## Upgrade

Install the newer package over the old one and restart the agent:

```sh
sudo apt install ./wingsmith-host_${NEW}_${ARCH}.deb   # or dnf install ./wingsmith-host-….rpm
sudo systemctl restart wingsmith-host
```

The package keeps `/etc/wingsmith/host.json`, the token and `/var/lib/wingsmith`. Replica
containers keep running and serving through the restart: they belong to Docker, and the agent
adopts them by label when it comes back. A host first installed from a bare binary running as root
can move to the package the same way; the post-install step hands the token and state to
`wingsmith`. Remove the old unit from `/etc/systemd/system` if you installed one there, since it
overrides the packaged unit.

## Uninstall

```sh
sudo apt remove wingsmith-host      # or: sudo dnf remove wingsmith-host
```

This stops and disables the agent. Replica and prefetch containers are left to Docker; remove them
with `docker rm -f $(docker ps -aq --filter label=wingsmith.dev/managed-by)` if you are done with
them. To purge everything else, including the enrolment token and the model cache:

```sh
sudo rm -rf /etc/wingsmith /var/lib/wingsmith
sudo userdel wingsmith
```

Delete the cluster on the plane (`DELETE /v1/clusters/gpu-box-1`) to revoke its token.

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
  move it). The `wingsmith` user must be in the `docker` group (see [Who the agent runs as](#who-the-agent-runs-as)).
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

A change of generation, serving digest or layout replaces each replica. When the host has enough
free GPUs for one more replica, the new container starts beside the old one, the route moves once it
is ready, and the old one is removed after that, so there is no gap. Otherwise the old container is
removed first, since it holds the GPUs the new one needs, and the rollout has a gap per replica.

Pausing the workload stops its containers on the host and frees their GPUs; resuming starts them
again.

### Weights

The host keeps its own model cache under `/var/lib/wingsmith/models` (`model_root`), laid out as in
a cluster: a pinned Hugging Face artifact under `hf/hub/models--<org>--<repo>/snapshots/<revision>`
(a private one under `p/<project>/hf/...`), any other artifact under `<artifact-id>` (private:
`p/<project>/<artifact-id>`).

Every artifact in the plane's warm set, and every artifact a declared deployment serves, is fetched
by a short-lived container named `ws-prefetch-<artifact-id>`. It runs the same download script as the
cluster prefetch Job, in `prefetch_image` if set and otherwise in the image of a deployment serving
the artifact; either must pass `allowed_image_prefixes`. Only the artifact's cache subtree is
mounted, read-write, and the container has no capabilities and a read-only root. At most
`max_prefetches` (default 2) run at once. When one exits cleanly and left the weights directory
non-empty, the host writes a completion marker under `.wingsmith/artifacts/` (a directory no
container mounts) and removes the container. A deployment starts only once its artifact has a
marker; until then it is `pending` with `WeightsNotResident`. Each artifact's state is reported as
`cache.observed` (`fetching`, `resident`, or `absent` with a reason such as `PrefetchFailed`,
`CredentialUnavailable`, `NoRuntimeImage`, `ImageNotAllowed`), so the plane shows why it waits.

A failed download is retried with backoff (1 minute doubling to 30). After a restart, a prefetch
container that finished while the host was down is collected as usual; a Hugging Face download
resumes from its partial files, and any other download starts again from an empty directory.

Weights copied in by hand are not trusted on their own: the first pass runs the prefetch, which
for Hugging Face verifies what is there and fetches only what is missing, then writes the marker.

#### Tokens for private artifacts

Tokens never come from the plane. An admin drops them on the host, as on a cluster they apply the
project's Secret:

```sh
sudo install -m 600 -o wingsmith -g wingsmith /dev/stdin /etc/wingsmith/credentials/<project-id>.token <<< "hf_..."
```

A private artifact whose source names a credential is fetched with `<project-id>.token` and is
refused with `CredentialUnavailable` while that file is missing. Public artifacts use
`_default.token` if present. A file not mode 0600, or not owned by root or `wingsmith`, is refused. The token reaches only
the prefetch container, as `HF_TOKEN`; serving containers run with `HF_HUB_OFFLINE=1` and no token.
The directory is `credentials_dir` in host.json.

#### Eviction

When the cache filesystem is above `cache_high_watermark` (default 0.90) of its size, the host
removes resident artifacts, least recently used first, until it is at `cache_low_watermark`
(default 0.80). It never removes an artifact in the warm set, one a declared deployment serves, or
one mounted by any replica container on the host. A Hugging Face repo directory is removed whole
only when no other artifact or replica uses it; otherwise only the snapshot goes. Replica containers
created before this version carry no model-path label, and while one exists the host does not evict.

### Not supported

Multi-tenant isolation (every project's containers share the host), Dynamo, disaggregated or
multi-node replicas, rootless Podman, cache quotas per project, and NVLink topology.
