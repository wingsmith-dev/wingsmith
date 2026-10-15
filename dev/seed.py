"""Fills a local Hangar with something to look at.

Declared objects go in over the public API. Observed objects go in the only way
observed objects can: over the hub, as a cluster reporting what it sees. Nothing
here reaches into the database, so what the console renders is what the real
write paths produce.
"""

import os
import json
import time
import random
import asyncio
import hashlib
import argparse
import contextlib
import urllib.error
import urllib.request

from typing import Any

import websockets


API = os.environ.get("HANGAR_API", "http://localhost:8000")
HUB = os.environ.get("HANGAR_HUB", "ws://localhost:8001/v1/hub/sync")

OPERATOR_TOKEN = os.environ.get("HANGAR_OPERATOR_TOKEN", "hangar-local-operator-token")
"""`app.core.auth.LOCAL_OPERATOR_TOKEN` — what the hub accepts when APP_ENV is `local`.

The hub's operator-token check has no disable switch, so an unauthenticated seeder is a 401
before the upgrade rather than a cluster that connects and says nothing.
"""

HUB_HEADERS = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def uid(prefix: str, key: str | None = None) -> str:
    """A 30-character prefixed id. With a key it is stable, so re-seeding converges."""
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


def post(path: str, body: dict[str, Any]) -> dict[str, Any] | None:
    request = urllib.request.Request(
        f"{API}/v1{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read())
            print(f"  + {path} {body.get('name') or body.get('slug') or ''}")
            return payload
    except urllib.error.HTTPError as error:
        detail = error.read().decode()
        if error.code == 409 and "conflict" in detail:
            print(f"  = {path} {body.get('name') or body.get('slug') or ''} (exists)")
            return None
        print(f"  ! {path} {error.code} {detail[:300]}")
        return None


CLUSTERS = [
    {
        "name": "eu-west-1a",
        "display_name": "Dublin A",
        "region": "eu-west-1",
        "zone": "eu-west-1a",
        "residency": "eu",
        "phase": "active",
        "labels": {"tier": "production", "fabric": "nvlink"},
        "capabilities": {"mig": "true", "rdma": "true"},
    },
    {
        "name": "us-east-2b",
        "display_name": "Ohio B",
        "region": "us-east-2",
        "zone": "us-east-2b",
        "residency": "us",
        "phase": "active",
        "labels": {"tier": "production"},
        "capabilities": {"mig": "false", "rdma": "true"},
    },
    {
        "name": "lab-1",
        "display_name": "Research Lab",
        "region": "eu-central-1",
        "zone": "eu-central-1c",
        "residency": "eu",
        "phase": "active",
        "labels": {"tier": "research"},
        "capabilities": {},
    },
]

POOLS = [
    ("eu-west-1a", "h100-sxm-8x", "h100-80gb-sxm", 8, 2, 12),
    ("eu-west-1a", "l40s-4x", "l40s-48gb-pcie", 4, 1, 8),
    ("us-east-2b", "h200-sxm-8x", "h200-141gb-sxm", 8, 1, 6),
    ("lab-1", "a100-pcie-4x", "a100-40gb-pcie", 4, 1, 4),
]

NODES = {
    "eu-west-1a": [
        (f"gpu-h100-a-0{i}", "h100-sxm-8x", "h100-80gb-sxm", "NVIDIA H100 80GB HBM3", 81559, 8) for i in range(1, 4)
    ]
    + [("gpu-l40s-a-01", "l40s-4x", "l40s-48gb-pcie", "NVIDIA L40S", 46068, 4)],
    "us-east-2b": [
        (f"gpu-h200-b-0{i}", "h200-sxm-8x", "h200-141gb-sxm", "NVIDIA H200 141GB HBM3e", 143771, 8) for i in range(1, 3)
    ],
    "lab-1": [("gpu-a100-lab-01", "a100-pcie-4x", "a100-40gb-pcie", "NVIDIA A100-PCIE-40GB", 40536, 4)],
}


def seed_tenants() -> None:
    """Inserts tenants directly.

    `libs/packages/contract/API.md` §6 has no `/v1/tenants` collection, so a tenant cannot be
    created over the API and a fresh database can hold no projects at all.
    """
    import asyncio as _asyncio

    from sqlalchemy import select

    from app.core.ids import new_uid
    from app.db.engine import session_factory
    from app.db.models import Tenant

    async def _insert() -> None:
        async with session_factory()() as db:
            for slug, name in [("acme", "Acme"), ("globex", "Globex")]:
                existing = await db.scalar(select(Tenant).where(Tenant.slug == slug))
                if existing is not None:
                    print(f"  = tenant {slug} (exists)")
                    continue
                db.add(Tenant(uid=new_uid("ten_"), slug=slug, name=name, enabled=True))
                print(f"  + tenant {slug}")
            await db.commit()

    _asyncio.run(_insert())


def seed_declared() -> dict[str, str]:
    print("declared:")
    seed_tenants()
    for cluster in CLUSTERS:
        post("/clusters", cluster)

    for cluster, name, device_class, per_node, lo, hi in POOLS:
        post(
            "/pools",
            {
                "cluster": cluster,
                "name": name,
                "device_class": device_class,
                "devices_per_node": per_node,
                "min_nodes": lo,
                "max_nodes": hi,
                "labels": {"fabric": "nvlink" if per_node == 8 else "pcie"},
                "enabled": True,
            },
        )

    for name, value, preemptible, preempts, default in [
        ("interactive", 1000, False, True, False),
        ("standard", 500, False, False, True),
        ("batch", 100, True, False, False),
    ]:
        post(
            "/priority-classes",
            {
                "name": name,
                "value": value,
                "preemptible": preemptible,
                "preempts": preempts,
                "is_default": default,
                "description": f"{name} workloads",
            },
        )

    for tenant, slug, name, priority in [
        ("acme", "acme-research", "Acme Research", "interactive"),
        ("acme", "acme-serving", "Acme Serving", "standard"),
        ("acme", "acme-batch", "Acme Batch", "batch"),
    ]:
        post(
            "/projects",
            {"tenant": tenant, "slug": slug, "name": name, "default_priority_class": priority, "enabled": True},
        )

    for project, device_class, cluster, guaranteed, limit in [
        ("acme-research", "h100-80gb-sxm", "eu-west-1a", 8, 24),
        ("acme-serving", "h100-80gb-sxm", "eu-west-1a", 16, 32),
        ("acme-serving", "h200-141gb-sxm", "us-east-2b", 8, 16),
        ("acme-batch", "a100-40gb-pcie", None, 4, 8),
    ]:
        post(
            "/quotas",
            {
                "project": project,
                "device_class": device_class,
                "cluster": cluster,
                "guaranteed_devices": guaranteed,
                "limit_devices": limit,
                "over_quota_weight": 1.0,
                "over_quota_enabled": True,
            },
        )

    artifacts = [
        ("llama-3.1-70b-instruct", "safetensors", 141_000_000_000, 70_553_706_496, "llama-3.1"),
        ("mixtral-8x7b-instruct", "safetensors", 93_000_000_000, 46_702_792_704, "apache-2.0"),
        ("qwen2.5-7b-instruct", "safetensors", 15_200_000_000, 7_615_616_512, "apache-2.0"),
    ]
    for name, fmt, size, params, licence in artifacts:
        post(
            "/artifacts",
            {
                "name": name,
                "digest": "sha256:" + hashlib.sha256(name.encode()).hexdigest(),
                "kind": "weights",
                "format": fmt,
                "size_bytes": size,
                "parameter_count": params,
                "licence": licence,
                "metadata": {"source": "huggingface"},
            },
        )

    recipes = [
        ("llama-70b-h100-tp8", "llama-3.1-70b-instruct", "h100-80gb-sxm", 8, 131072, "fp8"),
        ("mixtral-h200-tp4", "mixtral-8x7b-instruct", "h200-141gb-sxm", 4, 32768, None),
        ("qwen-7b-l40s-tp1", "qwen2.5-7b-instruct", "l40s-48gb-pcie", 1, 32768, None),
    ]
    for name, artifact, device_class, tp, context, quant in recipes:
        post(
            "/recipes",
            {
                "name": name,
                "artifact": artifact,
                "engine": "vllm",
                "engine_runtime_version": "1.4.1",
                "device_class": device_class,
                "context_length": context,
                "tensor_parallel": tp,
                "device_count": tp,
                "quantization": quant,
                "dtype": "bfloat16",
                "prefix_cache_enabled": True,
                "requires_single_topology_domain": tp > 1,
                "topology_domain_kind": "nvlink" if tp > 1 else None,
                "labels": {"engine": "vllm"},
            },
        )

    post(
        "/policies",
        {
            "name": "eu-only",
            "description": "Keep regulated tenants inside the EU",
            "allowed_regions": ["eu-west-1", "eu-central-1"],
            "allowed_residencies": ["eu"],
            "allowed_device_classes": [],
            "allowed_clusters": [],
            "denied_clusters": [],
            "enabled": True,
        },
    )

    ids: dict[str, str] = {}
    with urllib.request.urlopen(f"{API}/v1/clusters?limit=100") as response:
        for item in json.loads(response.read())["items"]:
            ids[item["name"]] = item["id"]
    return ids


class Reporter:
    """A cluster telling the hub what it sees, over the real protocol."""

    def __init__(self, cluster_id: str) -> None:
        self.cluster_id = cluster_id
        self.seq = 0
        self.ack = 0

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

    def hello(self, operator_version: str = "0.0.0-seed") -> str:
        return json.dumps(
            {
                "v": 1,
                "type": "hello",
                "id": uid("")[:26],
                "ts": now(),
                "payload": {"cluster_id": self.cluster_id, "operator_version": operator_version, "lossy": True},
            }
        )

    async def run(self, socket: Any, name: str) -> None:
        await socket.send(self.hello())
        welcome = json.loads(await socket.recv())
        self.ack = welcome.get("seq") or 0

        for frame in self.supply(name):
            await socket.send(frame)
        await asyncio.sleep(2.0)
        print(f"  + {name}: reported")

    def supply(self, name: str) -> list[str]:
        """Every observed frame describing one cluster's supply, in stream order."""
        frames: list[str] = []
        for node_name, pool, device_class, model, memory, count in NODES[name]:
            node_id = uid("nod_", f"{name}/{node_name}")
            device_ids = [uid("dev_", f"{name}/{node_name}/{i}") for i in range(count)]
            frames.append(
                self.frame(
                    "node.observed",
                    {
                        "node_id": node_id,
                        "cluster_id": self.cluster_id,
                        "name": node_name,
                        "pool": pool,
                        "membership": "joined",
                        "ready": True,
                        "cordoned": node_name.endswith("03"),
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
                        "operator_version": "0.0.0-seed",
                        "device_ids": device_ids,
                        "labels": {"pool": pool},
                        "observed_at": now(),
                    },
                )
            )
            for index, device_id in enumerate(device_ids):
                unhealthy = node_name.endswith("02") and index == 3
                frames.append(
                    self.frame(
                        "device.observed",
                        {
                            "device_id": device_id,
                            "node_id": node_id,
                            "cluster_id": self.cluster_id,
                            "uuid": "GPU-" + hashlib.sha256(device_id.encode()).hexdigest()[:32],
                            "index": index,
                            "model": model,
                            "device_class": device_class,
                            "memory_total_mb": memory,
                            "health": "degraded" if unhealthy else "healthy",
                            "ecc": {
                                "mode_enabled": True,
                                "volatile_corrected": index * 3,
                                "volatile_uncorrected": 1 if unhealthy else 0,
                                "aggregate_uncorrected": 1 if unhealthy else 0,
                                "retired_pages": 0,
                                "remapped_rows_pending": 0,
                                "remapped_rows_failed": 0,
                            },
                            "throttled": unhealthy,
                            "throttle_reasons": ["hw_slowdown"] if unhealthy else [],
                            "mig": {"enabled": False, "profile": None, "instances": []},
                            "observed_at": now(),
                        },
                    )
                )
            frames.append(
                self.frame(
                    "node.event",
                    {
                        "node_id": node_id,
                        "cluster_id": self.cluster_id,
                        "event": "cordoned" if node_name.endswith("03") else "joined",
                        "reason": "maintenance" if node_name.endswith("03") else "registered",
                        "message": f"{node_name} reported by the seed",
                        "occurred_at": now(),
                    },
                )
            )
            if node_name.endswith("02"):
                frames.append(
                    self.frame(
                        "device.event",
                        {
                            "device_id": device_ids[3],
                            "node_id": node_id,
                            "cluster_id": self.cluster_id,
                            "event": "degraded",
                            "reason": "ecc_uncorrected",
                            "message": "uncorrected ECC error on device 3",
                            "occurred_at": now(),
                        },
                    )
                )
            if count == 8:
                frames.append(
                    self.frame(
                        "topology.observed",
                        {
                            "domain_id": uid("tpd_", f"{name}/{node_name}/nvlink"),
                            "cluster_id": self.cluster_id,
                            "kind": "nvlink",
                            "node_ids": [node_id],
                            "spans_nodes": False,
                            "device_ids": device_ids,
                            "observed_at": now(),
                        },
                    )
                )

        return frames


def seed_demand() -> None:
    """Workloads and the endpoint in front of them, created once supply is live.

    Separate from `seed_declared` because of an ordering the object model dictates: creating a
    workload expands it into deployments there and then, and expansion places against clusters
    that are reachable and hold free devices. Declared before any cluster has reported, every
    workload lands with zero shares and the seeded world has no deployments in it.
    """
    print("demand:")
    # Workloads, not deployments: a deployment is a workload's share of one cluster and is
    # created by placement expanding the workload, which is why `POST /deployments` is a 405.
    # `cluster` left out is the normal case — the plane picks. `qwen-dev` names one, so the
    # declared-cluster path is exercised too rather than only the derived one.
    workloads = [
        ("llama-70b-prod", None, "llama-70b-h100-tp8", "acme-serving", "standard", 2, 1, 4),
        ("mixtral-prod", None, "mixtral-h200-tp4", "acme-serving", "standard", 1, 1, 3),
        ("qwen-dev", "eu-west-1a", "qwen-7b-l40s-tp1", "acme-research", "interactive", 1, 0, 2),
    ]
    for name, cluster, recipe, project, priority, desired, lo, hi in workloads:
        post(
            "/workloads",
            {
                "name": name,
                "cluster": cluster,
                "recipe": recipe,
                "project": project,
                "priority_class": priority,
                "kind": "service",
                "desired_scale": desired,
                "min_scale": lo,
                "max_scale": hi,
                "enabled": True,
                "labels": {"env": "prod" if "prod" in name else "dev"},
            },
        )

    post(
        "/endpoints",
        {
            "name": "chat",
            "project": "acme-serving",
            "display_name": "Chat completions",
            "visibility": "public",
            "routing_strategy": "weighted",
            "enabled": True,
            "targets": [{"deployment": "llama-70b-prod", "weight": 80}, {"deployment": "mixtral-prod", "weight": 20}],
        },
    )


async def seed_observed(ids: dict[str, str]) -> None:
    print("observed:")
    async with contextlib.AsyncExitStack() as stack:
        for name, cluster_id in ids.items():
            if name not in NODES:
                continue
            socket = await stack.enter_async_context(websockets.connect(HUB, additional_headers=HUB_HEADERS))
            await Reporter(cluster_id).run(socket, name)
        # Held open on purpose: `reachable` is cleared when the socket drops, and placement
        # refuses an unreachable cluster before it looks at capacity.
        seed_demand()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="seed",
        description="structure only by default: what the product itself cannot create",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="also fill in a whole worked world — clusters, quotas, artifacts, recipes, workloads",
    )
    args = parser.parse_args()

    # Tenants and nothing else, because a tenant is the one object with no create route
    # (`contract/API.md` §6 has no `/v1/tenants`), so a fresh database can hold no projects and
    # every other flow is blocked behind it. Everything past this point is a flow worth walking
    # by hand: register the cluster, create the artifact, write the recipe, run the workload.
    print("structure:")
    seed_tenants()

    if args.demo:
        ids = seed_declared()
        asyncio.run(seed_observed(ids))

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
