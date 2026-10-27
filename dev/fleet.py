"""Fake operators: three clusters' worth of hardware that exists, waiting to be enrolled.

There is nothing to virtualise here. The plane does not know what Kubernetes is, so a cluster
*is* a socket that speaks the contract and a device *is* a `device.observed` frame, which
means a fleet you can place on needs no kind, no OrbStack, and no GPU. This is that fleet.

Each process here behaves like a rack that has been delivered and racked but not yet enrolled:
it waits for a cluster of its name to be registered, mints an enrolment token the way the
register-cluster screen does, connects with it, and reports what it has. Register the cluster
in the console and it attaches within a couple of seconds. Delete it and the agent goes back
to waiting.

The session then stays up, and that is what matters. `reachable` is a column the hub writes on
connect and clears on disconnect, and placement refuses an unreachable cluster before it looks
at capacity, so a reporter that hangs up leaves a fleet nothing can ever be placed on.

Demand is answered, not just received: a `deployment.desired` arriving here is answered with
`deployment.observed`, a `replica.observed` per replica, and the `allocation.observed` that
commits the devices. That is what makes a workload actually go ready locally.

    just fleet                  # all three, waiting
    just fleet eu-west-1a       # just one

It is not the operator and renders nothing. For the reconcile half (whether a Deployment comes
out right), run the real operator against kind (`just cluster-up`).
"""

from __future__ import annotations

import os
import sys
import json
import time
import random
import signal
import asyncio
import hashlib
import argparse
import contextlib
import urllib.error
import urllib.request

from typing import Any
from dataclasses import field
from dataclasses import dataclass

import websockets


API = os.environ.get("HANGAR_API", "http://localhost:8000")
HUB = os.environ.get("HANGAR_HUB", "ws://localhost:8001/v1/hub/sync")

API_TOKEN = os.environ.get("HANGAR_API_TOKEN", "")
"""An admin session token, sent to the API as a bearer when set.

The compose stack runs with `APP_ENV=local`, which turns admin auth off, so locally this is
empty. Against a plane with admin auth on, sign in and export the session token here. The hub
never sees it: each cluster connects with its own enrolment token, minted below.
"""

OPERATOR_VERSION = "0.0.0-fake"

HEARTBEAT_SECONDS = float(os.environ.get("HANGAR_FLEET_HEARTBEAT", "10"))
POLL_SECONDS = float(os.environ.get("HANGAR_FLEET_POLL", "2"))

STARTUP_SECONDS = float(os.environ.get("HANGAR_FLEET_STARTUP", "3"))
"""How long a replica spends `starting` before it goes `ready`.

Not zero: a deployment that is ready in the tick it was declared makes the pending and
progressing states unreachable, and those are the ones worth watching a flow through.
"""

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def uid(prefix: str, key: str | None = None) -> str:
    """A 30-character prefixed id. With a key it is stable, so reconnecting converges."""
    if key is None:
        body = "".join(random.choice(ALPHABET) for _ in range(26))
    else:
        digest = hashlib.sha256(key.encode()).digest()
        value = int.from_bytes(digest[:16], "big")
        body = ""
        for _ in range(26):
            body = ALPHABET[value % 32] + body
            value //= 32
    return f"{prefix}{body}"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


# ── The hardware ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NodeSpec:
    name: str
    pool: str
    device_class: str
    model: str
    memory_mib: int
    devices: int

    @property
    def nvlink(self) -> bool:
        """Whether the node's devices sit in one NVLink domain, so TP>1 has somewhere to land."""
        return self.devices >= 8


@dataclass(frozen=True)
class ClusterSpec:
    """One fake cluster: what to call it, and what is in it.

    `region`, `zone` and `residency` are what to register it as. They are here so the agent can
    print the registration it is waiting for, and so `--register` can do it unattended.
    """

    name: str
    region: str
    zone: str
    residency: str
    nodes: list[NodeSpec] = field(default_factory=list)


FLEET: list[ClusterSpec] = [
    ClusterSpec(
        name="eu-west-1a",
        region="eu-west-1",
        zone="eu-west-1a",
        residency="eu",
        nodes=[
            NodeSpec("gpu-h100-a-01", "h100-sxm-8x", "h100-80gb-sxm", "NVIDIA H100 80GB HBM3", 81559, 8),
            NodeSpec("gpu-h100-a-02", "h100-sxm-8x", "h100-80gb-sxm", "NVIDIA H100 80GB HBM3", 81559, 8),
            NodeSpec("gpu-l40s-a-01", "l40s-4x", "l40s-48gb-pcie", "NVIDIA L40S", 46068, 4),
        ],
    ),
    ClusterSpec(
        name="us-east-2b",
        region="us-east-2",
        zone="us-east-2b",
        residency="us",
        nodes=[
            NodeSpec("gpu-h200-b-01", "h200-sxm-8x", "h200-141gb-sxm", "NVIDIA H200 141GB HBM3e", 143771, 8),
            NodeSpec("gpu-h200-b-02", "h200-sxm-8x", "h200-141gb-sxm", "NVIDIA H200 141GB HBM3e", 143771, 8),
        ],
    ),
    # Two small classes instead of one, by design: "this artifact fits an H100 or an RTX 6000,
    # at different parallelism" is the question the fit work has to answer, and it is not a
    # question a homogeneous fleet can be asked.
    ClusterSpec(
        name="lab-1",
        region="eu-central-1",
        zone="eu-central-1c",
        residency="eu",
        nodes=[
            NodeSpec("gpu-a100-lab-01", "a100-pcie-4x", "a100-40gb-pcie", "NVIDIA A100-PCIE-40GB", 40960, 4),
            NodeSpec(
                "gpu-6000-lab-01", "rtx6000-4x", "rtx-6000-ada-48gb-pcie", "NVIDIA RTX 6000 Ada Generation", 49140, 4
            ),
        ],
    ),
]


# ── The API, as a person using the console would ──────────────────────────────


class ApiError(RuntimeError):
    """A non-2xx answer from the API, with the status kept so a caller can tell 409 apart."""

    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        super().__init__(f"{method} {path} -> {status} {detail[:300]}")
        self.status = status
        self.detail = detail


def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    request = urllib.request.Request(
        f"{API}/v1{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raise ApiError(method, path, error.code, error.read().decode()) from error


def registered(name: str) -> dict[str, Any] | None:
    """The registered cluster of this name, or None while nobody has registered it."""
    for item in call("GET", "/clusters?limit=100")["items"]:
        if item["name"] == name:
            return item
    return None


def enrol(spec: ClusterSpec) -> str:
    """Mints this cluster's enrolment token, exactly as the register screen's button does.

    Rotating on every attach is fine and is what a re-installed operator would cause: the
    plaintext is returned once and only the hashes are stored, so there is nothing to reuse.
    """
    setup = call("POST", f"/clusters/{spec.name}/enrolment-token", {})
    token = setup.get("enrolment_token")
    if not token:
        raise RuntimeError(f"{spec.name}: enrolment-token returned no plaintext")
    return token


# ── Devices ───────────────────────────────────────────────────────────────────


class Devices:
    """The devices one cluster has, and which are spoken for.

    Handed out per node, not per cluster: a replica's devices have to come from one
    node to be a shape anything could run. This fake is allowed to be crude about which node
    it picks and not about whether the answer is coherent.
    """

    def __init__(self, spec: ClusterSpec) -> None:
        self.nodes: list[tuple[str, str, list[str]]] = []
        for node in spec.nodes:
            self.nodes.append((node_id(spec, node), node.device_class, device_ids(spec, node)))
        self.taken: set[str] = set()

    def claim(self, device_class: str, count: int) -> tuple[str, list[str]] | None:
        """A node id and `count` free devices of that class, or None if nothing fits."""
        for node, klass, ids in self.nodes:
            if klass != device_class:
                continue
            free = [d for d in ids if d not in self.taken]
            if len(free) >= count:
                chosen = free[:count]
                self.taken.update(chosen)
                return node, chosen
        return None

    def release(self, ids: list[str]) -> None:
        self.taken.difference_update(ids)


def node_id(spec: ClusterSpec, node: NodeSpec) -> str:
    return uid("nod_", f"{spec.name}/{node.name}")


def device_ids(spec: ClusterSpec, node: NodeSpec) -> list[str]:
    return [uid("dev_", f"{spec.name}/{node.name}/{i}") for i in range(node.devices)]


# ── Demand this cluster was handed ────────────────────────────────────────────


class Replica:
    def __init__(self, replica_id: str, name: str, node: str, devices: list[str]) -> None:
        self.id = replica_id
        self.name = name
        self.node_id = node
        self.device_ids = devices
        self.allocation_id = uid("alc_", replica_id)
        self.phase = "starting"
        self.started_at = now()


class Deployment:
    def __init__(self, desired: dict[str, Any]) -> None:
        self.desired = desired
        self.replicas: list[Replica] = []
        self.short = 0
        """Replicas asked for that no node could hold. Reported unavailable, never hidden."""

    @property
    def id(self) -> str:
        return self.desired["deployment_id"]

    @property
    def generation(self) -> int:
        return int(self.desired.get("generation") or 1)

    @property
    def wanted(self) -> int:
        return int(self.desired.get("min_replicas") or 0)

    @property
    def phase(self) -> str:
        if self.short:
            return "degraded" if self.replicas else "failed"
        if not self.replicas:
            return "pending" if self.wanted else "running"
        return "running" if all(r.phase == "ready" for r in self.replicas) else "placed"


# ── One fake operator ─────────────────────────────────────────────────────────


class Agent:
    """One cluster's agent: wait to be enrolled, report supply, then serve demand."""

    def __init__(self, spec: ClusterSpec) -> None:
        self.spec = spec
        self.cluster_id = ""
        self.seq = 0
        self.ack = 0
        self.devices = Devices(spec)
        self.deployments: dict[str, Deployment] = {}
        self.socket: Any = None

    def log(self, message: str) -> None:
        print(f"  {self.spec.name}: {message}", flush=True)

    # ── wire ──

    def frame(self, type_name: str, payload: dict[str, Any]) -> str:
        self.seq += 1
        return json.dumps(
            {
                "v": 1,
                "type": type_name,
                "id": uid("")[:26],
                "ts": now(),
                "seq": self.seq,
                "ack": self.ack,
                "payload": payload,
            }
        )

    def unsequenced(self, type_name: str, payload: dict[str, Any]) -> str:
        return json.dumps({"v": 1, "type": type_name, "id": uid("")[:26], "ts": now(), "payload": payload})

    async def send(self, type_name: str, payload: dict[str, Any]) -> None:
        await self.socket.send(self.frame(type_name, payload))

    # ── lifecycle ──

    async def run(self) -> None:
        announced = False
        while True:
            try:
                cluster = registered(self.spec.name)
                if cluster is None:
                    if not announced:
                        self.log(
                            "waiting to be enrolled: register it "
                            f"(region {self.spec.region}, zone {self.spec.zone}, residency {self.spec.residency})"
                        )
                        announced = True
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                announced = False
                self.cluster_id = cluster["id"]
                token = enrol(self.spec)
                await self.session(token)
            except Exception as error:  # noqa: BLE001 (a rack does not power off because the plane restarted)
                self.log(f"{type(error).__name__}: {error}")
            await asyncio.sleep(POLL_SECONDS)

    async def session(self, token: str) -> None:
        headers = {"Authorization": f"Bearer {token}"}
        async with websockets.connect(HUB, additional_headers=headers, ping_interval=None) as socket:
            self.socket = socket
            self.deployments.clear()
            self.devices.taken.clear()

            await socket.send(
                self.unsequenced(
                    "hello", {"cluster_id": self.cluster_id, "operator_version": OPERATOR_VERSION, "lossy": True}
                )
            )
            welcome = json.loads(await socket.recv())
            self.ack = welcome.get("seq") or 0

            supply = self.supply()
            for message in supply:
                await socket.send(message)
            devices = sum(n.devices for n in self.spec.nodes)
            self.log(f"enrolled and reporting: {len(self.spec.nodes)} node(s), {devices} device(s)")

            heartbeat = asyncio.create_task(self.heartbeat())
            try:
                async for raw in socket:
                    await self.handle(json.loads(raw))
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            self.log("session closed")

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self.socket.send(self.unsequenced("ping", {}))

    # ── supply ──

    def supply(self) -> list[str]:
        """Every observed frame describing this cluster's supply, in stream order."""
        frames: list[str] = []
        for node in self.spec.nodes:
            nid = node_id(self.spec, node)
            dids = device_ids(self.spec, node)
            frames.append(
                self.frame(
                    "node.observed",
                    {
                        "node_id": nid,
                        "cluster_id": self.cluster_id,
                        "name": node.name,
                        "pool": node.pool,
                        "membership": "joined",
                        "ready": True,
                        "cordoned": False,
                        "conditions": [
                            {
                                "type": "memory_pressure",
                                "status": "false",
                                "reason": "sufficient_memory",
                                "message": "node has sufficient memory available",
                                "since": now(),
                            }
                        ],
                        "kernel_version": "6.8.0-45-generic",
                        "driver_version": "570.86.15",
                        "runtime_version": "containerd://1.7.27",
                        "operator_version": OPERATOR_VERSION,
                        "device_ids": dids,
                        "labels": {"pool": node.pool},
                        "observed_at": now(),
                    },
                )
            )
            for index, did in enumerate(dids):
                frames.append(
                    self.frame(
                        "device.observed",
                        {
                            "device_id": did,
                            "node_id": nid,
                            "cluster_id": self.cluster_id,
                            "uuid": "GPU-" + hashlib.sha256(did.encode()).hexdigest()[:32],
                            "index": index,
                            "model": node.model,
                            "device_class": node.device_class,
                            "memory_total_mb": node.memory_mib,
                            "health": "healthy",
                            "ecc": {
                                "mode_enabled": True,
                                "volatile_corrected": 0,
                                "volatile_uncorrected": 0,
                                "aggregate_uncorrected": 0,
                                "retired_pages": 0,
                                "remapped_rows_pending": 0,
                                "remapped_rows_failed": 0,
                            },
                            "throttled": False,
                            "throttle_reasons": [],
                            "mig": {"enabled": False, "profile": None, "instances": []},
                            "observed_at": now(),
                        },
                    )
                )
            frames.append(
                self.frame(
                    "node.event",
                    {
                        "node_id": nid,
                        "cluster_id": self.cluster_id,
                        "event": "joined",
                        "reason": "registered",
                        "message": f"{node.name} reported by the fake operator",
                        "occurred_at": now(),
                    },
                )
            )
            if node.nvlink:
                frames.append(
                    self.frame(
                        "topology.observed",
                        {
                            "domain_id": uid("tpd_", f"{self.spec.name}/{node.name}/nvlink"),
                            "cluster_id": self.cluster_id,
                            "kind": "nvlink",
                            "node_ids": [nid],
                            "spans_nodes": False,
                            "device_ids": dids,
                            "observed_at": now(),
                        },
                    )
                )
        return frames

    # ── demand ──

    async def handle(self, message: dict[str, Any]) -> None:
        if seq := message.get("seq"):
            self.ack = max(self.ack, int(seq))
        payload = message.get("payload") or {}
        match message.get("type"):
            case "ping":
                await self.socket.send(self.unsequenced("pong", {}))
            case "deployment.desired":
                await self.apply(payload)
            case "deployment.deleted":
                await self.remove(payload.get("deployment_id"))
            case "error":
                self.log(f"hub error: {payload.get('code')} {payload.get('message')}")

    async def apply(self, desired: dict[str, Any]) -> None:
        deployment = Deployment(desired)
        previous = self.deployments.get(deployment.id)
        if previous is not None:
            for replica in previous.replicas:
                self.devices.release(replica.device_ids)
        self.deployments[deployment.id] = deployment

        placement = desired.get("placement") or {}
        device_class = placement.get("device_class") or ""
        per_replica = max(int(placement.get("devices_per_replica") or 1), 1)

        for index in range(deployment.wanted):
            claimed = self.devices.claim(device_class, per_replica)
            if claimed is None:
                deployment.short += 1
                continue
            node, devices = claimed
            replica_id = uid("rep_", f"{self.spec.name}/{deployment.id}/{index}")
            name = f"{desired.get('name') or deployment.id}-{replica_id[-5:]}"
            deployment.replicas.append(Replica(replica_id, name, node, devices))

        short = f", {deployment.short} unplaceable" if deployment.short else ""
        self.log(f"deployment {desired.get('name')}: {len(deployment.replicas)} replica(s){short}")

        await self.report(deployment)
        for replica in deployment.replicas:
            await self.send("replica.observed", self.replica_frame(deployment, replica))
        asyncio.create_task(self.ready(deployment))

    async def ready(self, deployment: Deployment) -> None:
        await asyncio.sleep(STARTUP_SECONDS)
        if self.deployments.get(deployment.id) is not deployment:
            return
        for replica in deployment.replicas:
            replica.phase = "ready"
            await self.send("replica.observed", self.replica_frame(deployment, replica))
            await self.send(
                "replica.event",
                {
                    "replica_id": replica.id,
                    "deployment_id": deployment.id,
                    "cluster_id": self.cluster_id,
                    "event": "ready",
                    "reason": "engine_started",
                    "message": f"{replica.name} reported ready by the fake operator",
                    "occurred_at": now(),
                },
            )
            await self.send(
                "allocation.observed",
                {
                    "allocation_id": replica.allocation_id,
                    "cluster_id": self.cluster_id,
                    "replica_id": replica.id,
                    "deployment_id": deployment.id,
                    "project_id": deployment.desired["project_id"],
                    "node_id": replica.node_id,
                    "device_ids": replica.device_ids,
                    "exclusive": True,
                    "granted_at": replica.started_at,
                    "observed_at": now(),
                },
            )
        await self.report(deployment)
        self.log(f"deployment {deployment.desired.get('name')}: ready")

    async def remove(self, deployment_id: str | None) -> None:
        deployment = self.deployments.pop(deployment_id or "", None)
        if deployment is None:
            return
        for replica in deployment.replicas:
            self.devices.release(replica.device_ids)
        deployment.replicas.clear()
        deployment.short = 0
        await self.send(
            "deployment.observed",
            {
                "deployment_id": deployment.id,
                "cluster_id": self.cluster_id,
                "generation": deployment.generation,
                "phase": "terminating",
                "desired_replicas": 0,
                "ready_replicas": 0,
                "updated_replicas": 0,
                "unavailable_replicas": 0,
                "replica_ids": [],
                "reason": "deleted",
                "message": "removed by the fake operator",
                "observed_at": now(),
            },
        )
        self.log(f"deployment {deployment.desired.get('name')}: removed")

    async def report(self, deployment: Deployment) -> None:
        ready = sum(1 for r in deployment.replicas if r.phase == "ready")
        await self.send(
            "deployment.observed",
            {
                "deployment_id": deployment.id,
                "cluster_id": self.cluster_id,
                "generation": deployment.generation,
                "phase": deployment.phase,
                "desired_replicas": deployment.wanted,
                "ready_replicas": ready,
                "updated_replicas": len(deployment.replicas),
                "unavailable_replicas": max(deployment.wanted - ready, 0),
                "replica_ids": [r.id for r in deployment.replicas],
                "reason": "no_capacity" if deployment.short else None,
                "message": (
                    f"{deployment.short} replica(s) found no node with enough free devices"
                    if deployment.short
                    else None
                ),
                "observed_at": now(),
            },
        )

    def replica_frame(self, deployment: Deployment, replica: Replica) -> dict[str, Any]:
        return {
            "replica_id": replica.id,
            "name": replica.name,
            "deployment_id": deployment.id,
            "cluster_id": self.cluster_id,
            "node_id": replica.node_id,
            "device_ids": replica.device_ids,
            "phase": replica.phase,
            "ready": replica.phase == "ready",
            "engine": "vllm",
            "engine_runtime_version": OPERATOR_VERSION,
            "recipe_id": deployment.desired["recipe_id"],
            "restart_count": 0,
            "observed_generation": deployment.generation,
            "started_at": replica.started_at,
            "observed_at": now(),
        }


# ── Entry ─────────────────────────────────────────────────────────────────────


def registration(spec: ClusterSpec) -> dict[str, Any]:
    """The `POST /v1/clusters` body that registers this cluster."""
    return {
        "name": spec.name,
        "display_name": spec.name,
        "region": spec.region,
        "zone": spec.zone,
        "residency": spec.residency,
        "phase": "active",
        "labels": {"fake": "true"},
        "capabilities": {"rdma": "true"},
    }


def register(spec: ClusterSpec) -> None:
    """Registers the cluster, for when walking the screen by hand is not the point."""
    if registered(spec.name) is not None:
        print(f"  {spec.name}: already registered")
        return
    call("POST", "/clusters", registration(spec))
    print(f"  {spec.name}: registered")


async def serve(specs: list[ClusterSpec]) -> None:
    print(f"fleet: {', '.join(s.name for s in specs)} -> {HUB}")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    agents = [asyncio.create_task(Agent(spec).run()) for spec in specs]
    await stop.wait()
    print("\nfleet: stopping")
    for task in agents:
        task.cancel()
    await asyncio.gather(*agents, return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="fleet", description="fake operators for local development")
    parser.add_argument("cluster", nargs="*", help="only these clusters (default: all of them)")
    parser.add_argument(
        "--register",
        action="store_true",
        help="register the clusters first, instead of waiting for someone to do it",
    )
    args = parser.parse_args()

    specs = [s for s in FLEET if not args.cluster or s.name in args.cluster]
    unknown = set(args.cluster) - {s.name for s in FLEET}
    if unknown:
        print(f"unknown cluster(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        print(f"known: {', '.join(s.name for s in FLEET)}", file=sys.stderr)
        return 1

    if args.register:
        print("registering:")
        for spec in specs:
            register(spec)

    asyncio.run(serve(specs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
