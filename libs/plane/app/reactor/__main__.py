"""Runs the reactor on its own, for a deployment that would rather not fold it into the hub.

The hub starts one of these in-process by default (`app.hub.server`), which is the
right shape while the fleet is small: the lease makes co-tenancy safe, and a
separate Deployment for a loop that ticks once a minute is infrastructure nobody
asked for. This entrypoint exists so that stops being a decision baked into the
code: set `HANGAR_REACTOR_ENABLED=false` on the hub and run this instead.
"""

import asyncio

from app.utils import logs
from app.config import settings
from app.hub.identity import instance_id
from app.reactor.loop import reactor_loop
from app.config.settings import validate


def main() -> None:
    validate(settings)
    logs.configure("reactor")
    asyncio.run(reactor_loop(instance_id()))


if __name__ == "__main__":
    main()
