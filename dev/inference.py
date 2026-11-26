"""The request path for the fake fleet: a real gateway, and a real agent per cluster.

`fleet.py` fakes the operator half of a cluster. This is the other half, and almost none of it is
fake: the gateway and the agent are this checkout's own binaries, the gateway reads its routing
table from the local plane the way a deployed one does, and each agent dials it with the cluster's
enrolment token. Only the engine at the end is a stand-in, `wingstransit upstream --demo`, which
answers in words that name the model and the cluster that served it.

    just fleet --serve          # the fleet, plus the gateway and an agent per cluster

Requests go to the gateway's internal listener, which authenticates nobody. That is what the
internal listener is for, and it is bound to loopback here for the same reason the plane's ports
are.
"""

from __future__ import annotations

import os
import asyncio
import contextlib

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PREBUILT = "WINGSMITH_DEMO_BIN" in os.environ
"""Set in the demo image, which ships the three binaries built; `build()` then has nothing to do."""
BIN = Path(os.environ["WINGSMITH_DEMO_BIN"]) if PREBUILT else ROOT / "dev" / ".bin"
LOGS = BIN / "logs"
LOG_TO_STDOUT = os.environ.get("WINGSMITH_DEMO_LOGS") == "stdout"
"""Children inherit this process's stdout instead of writing files under `LOGS`, so `kubectl logs` sees them."""

BIND = os.environ.get("WINGSMITH_DEMO_BIND", "127.0.0.1")
"""The address the per-cluster engines and agent health endpoints listen on. They are only dialled from
inside this process tree, so the default is right except where something else must probe them."""
DIAL = "127.0.0.1" if BIND in ("", "0.0.0.0", "::") else BIND

PLANE = os.environ.get("WINGSMITH_API", "http://localhost:8000").removesuffix("/v1")

GATEWAY = os.environ.get("WINGSMITH_DEMO_GATEWAY", "127.0.0.1:8080")
"""Where callers reach the gateway: its internal listener."""

TUNNEL = os.environ.get("WINGSMITH_DEMO_TUNNEL", "127.0.0.1:8443")
"""Where the gateway's tunnel listener binds."""

TUNNEL_DIAL = os.environ.get("WINGSMITH_DEMO_TUNNEL_DIAL", TUNNEL)
"""Where the agents dial it. Differs from `TUNNEL` when that binds `0.0.0.0`."""
GATEWAY_METRICS = os.environ.get("WINGSMITH_DEMO_GATEWAY_METRICS", "127.0.0.1:9090")

FRONTEND_PORTS = 9901
AGENT_HEALTH_PORTS = 9801

LOCAL_GATEWAY_SERVICE_TOKEN = "wingsmith-local-gateway-token"
"""What the plane accepts from a gateway when `APP_ENV=local` and no token is set
(`app.auth.const`). Nothing else accepts it."""

BINARIES = {
    # name: (module, package)
    "gateway": ("libs/gateway", "./cmd/gateway"),
    "agent": ("libs/agent", "./cmd/agent"),
    "wingstransit": ("libs/transit", "./cmd/wingstransit"),
}


async def build() -> None:
    """Builds the three binaries from this checkout, so the path is the code being looked at."""
    if PREBUILT:
        return
    BIN.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    for name, (module, package) in BINARIES.items():
        process = await asyncio.create_subprocess_exec("go", "build", "-o", str(BIN / name), package, cwd=ROOT / module)
        if await process.wait() != 0:
            raise SystemExit(f"inference: building {name} from {module} failed")


async def spawn(name: str, argv: list[str], env: dict[str, str] | None = None) -> asyncio.subprocess.Process:
    if LOG_TO_STDOUT:
        return await asyncio.create_subprocess_exec(*argv, env={**os.environ, **(env or {})})
    LOGS.mkdir(parents=True, exist_ok=True)
    log = (LOGS / f"{name}.log").open("ab")
    return await asyncio.create_subprocess_exec(
        *argv, env={**os.environ, **(env or {})}, stdout=log, stderr=asyncio.subprocess.STDOUT
    )


async def stop(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=5)
        return
    process.kill()
    await process.wait()


class Gateway:
    """The one gateway replica every agent dials."""

    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self.process = await spawn(
            "gateway",
            [
                str(BIN / "gateway"),
                "-listen",
                GATEWAY,
                "-plane-url",
                PLANE,
                "-tunnel-listen",
                TUNNEL,
                "-metrics-listen",
                GATEWAY_METRICS,
                "-gateway-id",
                "gateway-0",
                "-gateways",
                "1",
                # Short, so an endpoint created in the console routes within seconds.
                "-resync",
                "5s",
            ],
            # Overridable, because a plane outside `APP_ENV=local` does not accept the local default.
            {"WINGSMITH_GATEWAY_SERVICE_TOKEN": os.environ.get("WINGSMITH_GATEWAY_SERVICE_TOKEN", LOCAL_GATEWAY_SERVICE_TOKEN)},
        )
        print(f"inference: gateway on http://{GATEWAY}, tunnel on {TUNNEL}, logs in {LOGS}")

    async def close(self) -> None:
        await stop(self.process)


class ClusterPath:
    """One cluster's agent and the stand-in engine behind it.

    The engine runs for as long as the fleet does. The agent is restarted on every enrolment,
    because each one rotates the token it dials with.
    """

    def __init__(self, name: str, index: int) -> None:
        self.name = name
        self.index = index
        self.frontend = f"{BIND}:{FRONTEND_PORTS + index}"
        self.health = f"{BIND}:{AGENT_HEALTH_PORTS + index}"
        self.engine: asyncio.subprocess.Process | None = None
        self.agent: asyncio.subprocess.Process | None = None

    async def attach(self, cluster_id: str, token: str) -> None:
        if self.engine is None or self.engine.returncode is not None:
            self.engine = await spawn(
                f"engine-{self.name}",
                [
                    str(BIN / "wingstransit"),
                    "upstream",
                    "-listen",
                    self.frontend,
                    "-demo",
                    self.name,
                    # Slow enough to read as it streams, quick enough not to be a wait.
                    "-ttft",
                    "350ms",
                    "-itl",
                    "35ms",
                ],
            )
        await stop(self.agent)
        self.agent = await spawn(
            f"agent-{self.name}",
            [str(BIN / "agent")],
            {
                "WINGSMITH_CLUSTER_ID": cluster_id,
                "WINGSMITH_TUNNEL_GATEWAY": TUNNEL_DIAL,
                "WINGSMITH_ENROLMENT_TOKEN": token,
                # Loopback to loopback; there is nothing between them to terminate TLS for.
                "WINGSMITH_TUNNEL_TLS": "false",
                "WINGSMITH_INFERENCE_ENDPOINT": f"http://{DIAL}:{FRONTEND_PORTS + self.index}",
                "WINGSMITH_HEALTH_ADDR": self.health,
                "WINGSMITH_LOG_FORMAT": "text",
            },
        )

    async def close(self) -> None:
        await stop(self.agent)
        await stop(self.engine)
