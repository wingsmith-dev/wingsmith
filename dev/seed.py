"""Fills a local Wingsmith with something to look at.

Everything goes in over the public API, and observed state comes from the fake fleet
(`dev/fleet.py`), each cluster connecting to the hub with its own enrolment token. Nothing here
reaches into the database, so what the console renders is what the real write paths produce.

    just seed            # the tenants every other flow starts from
    just seed --demo     # plus clusters, pools, quotas, weights, workloads and an endpoint

`--demo` registers the fleet's clusters and then waits for them to report, because creating a
workload places it there and then against the clusters that are reachable and hold free devices.
Run `just fleet` in another terminal, before or after; the workloads go in once every cluster is
connected. The fleet has to stay up afterwards for the clusters to stay reachable.
"""

from __future__ import annotations

import os
import sys
import time
import hashlib
import argparse
import urllib.error

from typing import Any
from collections import defaultdict

from fleet import API
from fleet import FLEET
from fleet import ApiError
from fleet import ClusterSpec
from fleet import call
from fleet import registration


WAIT_SECONDS = float(os.environ.get("WINGSMITH_SEED_WAIT", "300"))
"""How long `--demo` waits for the fleet to connect before giving up."""


def post(path: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """Creates one object. An existing one (409) is fine, so re-seeding converges.

    Anything else non-2xx stops the seed. Carrying on past a refused cluster or artifact only
    moves the failure to whatever depends on it, where it reads as something else.
    """
    label = body.get("name") or body.get("slug") or ""
    try:
        payload = call("POST", path, body)
    except ApiError as error:
        if error.status == 409:
            print(f"  = {path} {label} (exists)")
            return None
        raise
    print(f"  + {path} {label}")
    return payload


# ── What goes in ──────────────────────────────────────────────────────────────

TENANTS = [("acme", "Acme"), ("globex", "Globex")]

PRIORITY_CLASSES = [
    # name, value, preemptible, preempts, default
    ("interactive", 1000, False, True, False),
    ("standard", 500, False, False, True),
    ("batch", 100, True, False, False),
]

PROJECTS = [
    # tenant, slug, name, default priority class
    ("acme", "acme-research", "Acme Research", "interactive"),
    ("acme", "acme-serving", "Acme Serving", "standard"),
    ("acme", "acme-batch", "Acme Batch", "batch"),
]

QUOTAS = [
    # project, device class, cluster (None for any), guaranteed, limit
    ("acme-research", "h100-80gb-sxm", "eu-west-1a", 8, 16),
    ("acme-research", "l40s-48gb-pcie", "eu-west-1a", 4, 4),
    ("acme-serving", "h100-80gb-sxm", "eu-west-1a", 16, 16),
    ("acme-serving", "h200-141gb-sxm", "us-east-2b", 8, 16),
    ("acme-batch", "a100-40gb-pcie", None, 4, 4),
]

ARTIFACTS = [
    # name, Hugging Face repository, size in bytes, parameters, licence
    ("llama-3.1-70b-instruct", "meta-llama/Llama-3.1-70B-Instruct", 141_000_000_000, 70_553_706_496, "llama-3.1"),
    ("mixtral-8x7b-instruct", "mistralai/Mixtral-8x7B-Instruct-v0.1", 93_000_000_000, 46_702_792_704, "apache-2.0"),
    ("qwen2.5-7b-instruct", "Qwen/Qwen2.5-7B-Instruct", 15_200_000_000, 7_615_616_512, "apache-2.0"),
]

WORKLOADS = [
    # name, pinned cluster, artifact, device class, tensor parallel, context, project, priority,
    # desired, min, max
    #
    # `cluster` left out is the normal case: the plane picks. `qwen-dev` names one, so the
    # declared-cluster path is exercised too, not only the derived one.
    ("llama-70b-prod", None, "llama-3.1-70b-instruct", "h100-80gb-sxm", 8, 131072, "acme-serving", "standard", 2, 1, 2),
    ("mixtral-prod", None, "mixtral-8x7b-instruct", "h200-141gb-sxm", 4, 32768, "acme-serving", "standard", 1, 1, 3),
    (
        "qwen-dev",
        "eu-west-1a",
        "qwen2.5-7b-instruct",
        "l40s-48gb-pcie",
        1,
        32768,
        "acme-research",
        "interactive",
        1,
        0,
        2,
    ),
]

ENGINE_RUNTIME_VERSION = "1.4.1"


def pools(fleet: list[ClusterSpec]) -> list[dict[str, Any]]:
    """One pool per cluster and node pool the fleet reports, sized from its nodes."""
    nodes: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for spec in fleet:
        for node in spec.nodes:
            nodes[(spec.name, node.pool)].append(node)
    return [
        {
            "cluster": cluster,
            "name": pool,
            "device_class": members[0].device_class,
            "devices_per_node": members[0].devices,
            "min_nodes": len(members),
            "max_nodes": len(members) * 2,
            "labels": {"fabric": "nvlink" if members[0].nvlink else "pcie"},
            "enabled": True,
        }
        for (cluster, pool), members in nodes.items()
    ]


def artifact(name: str, repository: str, size: int, parameters: int, licence: str) -> dict[str, Any]:
    return {
        "name": name,
        "digest": "sha256:" + hashlib.sha256(name.encode()).hexdigest(),
        "kind": "weights",
        "format": "safetensors",
        "size_bytes": size,
        "parameter_count": parameters,
        "licence": licence,
        # The location is where the model id the endpoint answers to is read from.
        "locations": [{"scheme": "hf", "uri": f"hf://{repository}", "preferred": True}],
    }


def workload(
    name: str,
    cluster: str | None,
    weights: str,
    device_class: str,
    tensor: int,
    context: int,
    project: str,
    priority: str,
    desired: int,
    lo: int,
    hi: int,
) -> dict[str, Any]:
    return {
        "name": name,
        "cluster": cluster,
        "project": project,
        "priority_class": priority,
        "kind": "service",
        "desired_scale": desired,
        "min_scale": lo,
        "max_scale": hi,
        "enabled": True,
        "labels": {"env": "prod" if "prod" in name else "dev"},
        "serving": {
            "artifact": weights,
            "engine": "vllm",
            "engine_runtime_version": ENGINE_RUNTIME_VERSION,
            "shape": {
                "device_class": device_class,
                "device_count": tensor,
                "requires_single_topology_domain": tensor > 1,
                "topology_domain_kind": "nvlink" if tensor > 1 else None,
            },
            "parallelism": {"tensor": tensor},
            "context_length": context,
            "dtype": "bfloat16",
            "cache": {"prefix_cache_enabled": True},
        },
    }


# ── Steps ─────────────────────────────────────────────────────────────────────


def seed_tenants() -> None:
    for slug, name in TENANTS:
        post("/tenants", {"slug": slug, "name": name, "enabled": True})


def seed_declared(fleet: list[ClusterSpec]) -> None:
    print("declared:")
    for spec in fleet:
        post("/clusters", registration(spec))
    for body in pools(fleet):
        post("/pools", body)

    for name, value, preemptible, preempts, default in PRIORITY_CLASSES:
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

    for tenant, slug, name, priority in PROJECTS:
        post(
            "/projects",
            {"tenant": tenant, "slug": slug, "name": name, "default_priority_class": priority, "enabled": True},
        )

    for project, device_class, cluster, guaranteed, limit in QUOTAS:
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

    for row in ARTIFACTS:
        post("/artifacts", artifact(*row))

    post(
        "/policies",
        {
            "name": "eu-only",
            "description": "Keep regulated tenants inside the EU",
            "allowed_regions": ["eu-west-1", "eu-central-1"],
            "allowed_residencies": ["eu"],
            "enabled": True,
        },
    )


def wait_for_fleet(names: list[str], timeout: float = WAIT_SECONDS) -> None:
    """Blocks until every named cluster is reachable, or exits saying how to fix it."""
    deadline = time.monotonic() + timeout
    said = False
    while True:
        items = call("GET", "/clusters?limit=100")["items"]
        reachable = {item["name"] for item in items if item.get("reachable")}
        missing = [name for name in names if name not in reachable]
        if not missing:
            return
        if time.monotonic() >= deadline:
            raise SystemExit(f"the fleet did not connect within {timeout:.0f}s: {', '.join(missing)} unreachable")
        if not said:
            print(f"waiting for {', '.join(missing)} to connect; run `just fleet` in another terminal")
            said = True
        time.sleep(2)


def seed_demand() -> None:
    print("demand:")
    # Workloads, not deployments: a deployment is a workload's share of one cluster, created by
    # placement expanding the workload and named after it.
    for row in WORKLOADS:
        post("/workloads", workload(*row))

    post(
        "/endpoints",
        {
            "name": "chat",
            "project": "acme-serving",
            "visibility": "public",
            "routing_strategy": "weighted",
            "enabled": True,
            "targets": [{"deployment": "llama-70b-prod", "weight": 80}, {"deployment": "mixtral-prod", "weight": 20}],
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seed", description="fill a local Wingsmith over its API")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="also register the fake fleet's clusters and create pools, quotas, weights, workloads "
        "and an endpoint; needs `just fleet` running",
    )
    args = parser.parse_args(argv)

    # Tenants by default, because a project needs one and every other flow needs a project.
    # Everything past this is a flow worth walking by hand in the console.
    print("structure:")
    try:
        seed_tenants()
        if args.demo:
            seed_declared(FLEET)
            wait_for_fleet([spec.name for spec in FLEET])
            seed_demand()
    except ApiError as error:
        print(f"seed: {error}", file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"seed: cannot reach the API at {API} ({error.reason}); `just dev-up` starts it", file=sys.stderr)
        return 1

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
