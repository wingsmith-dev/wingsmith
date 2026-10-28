"""dev/fleet.py: every frame the fake operator sends is one the hub can decode.

The fake is not the operator and nothing compiles it against the contract, so when a message
changes shape it keeps sending the old one. Decoding its frames with the plane's own structs turns
that into a failure here, not a cluster that reconnects forever in somebody's `just fleet`.
"""

from __future__ import annotations

import json
import asyncio

from typing import Any
from pathlib import Path

import fleet
import pytest

from app.hub.messages import decode


FIXTURES = Path(__file__).resolve().parents[2] / "libs" / "packages" / "contract" / "fixtures"


class Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)


def desired(spec: fleet.ClusterSpec) -> dict[str, Any]:
    """The contract's own deployment.desired, placed on one of this cluster's device classes."""
    payload = json.loads((FIXTURES / "deployment.desired.json").read_text())["payload"]
    node = spec.nodes[0]
    payload["placement"]["device_class"] = node.device_class
    payload["placement"]["devices_per_replica"] = 1
    payload["min_replicas"] = 1
    return payload


@pytest.mark.parametrize("spec", fleet.FLEET, ids=lambda spec: spec.name)
def test_every_frame_decodes_as_the_message_the_hub_expects(spec: fleet.ClusterSpec, monkeypatch) -> None:
    monkeypatch.setattr(fleet, "STARTUP_SECONDS", 0)
    agent = fleet.Agent(spec)
    agent.cluster_id = "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"
    agent.socket = Socket()

    frames = agent.supply()

    async def lifecycle() -> None:
        body = desired(spec)
        await agent.apply(body)
        deployment = agent.deployments[body["deployment_id"]]
        await agent.ready(deployment)
        await agent.remove(deployment.id)

    asyncio.run(lifecycle())
    frames += agent.socket.sent

    types = set()
    for raw in frames:
        envelope = decode(raw.encode())
        types.add(envelope.type)
    assert {"node.observed", "device.observed", "deployment.observed", "replica.observed"} <= types
    assert "allocation.observed" in types
