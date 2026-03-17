import sys
import asyncio
import argparse

import uvicorn

from app.config import settings
from app.api.app import create_app
from app.api.app import on_startup
from app.api.app import on_shutdown


async def _cycle() -> None:
    """Builds the app and runs its lifecycle hooks without binding a socket."""
    create_app()
    await on_startup()
    await on_shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(prog="hangar-api")
    parser.add_argument("--host", default=settings.API_HOST)
    parser.add_argument("--port", type=int, default=settings.API_PORT)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--check", action="store_true", help="build the app, run its hooks, exit")
    args = parser.parse_args()

    if args.check:
        asyncio.run(_cycle())
        return 0

    if args.reload:
        uvicorn.run("app.api.asgi:app", host=args.host, port=args.port, reload=True)
        return 0

    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
