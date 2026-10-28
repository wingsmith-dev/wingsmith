import sys
import asyncio
import argparse

from app.db import engine
from app.utils import logs
from app.hub.server import run


def main() -> int:
    parser = argparse.ArgumentParser(prog="wingsmith-hub")
    parser.add_argument("--check", action="store_true", help="start, tear down, exit")
    args = parser.parse_args()

    if args.check:
        logs.configure("hub")

        async def _cycle() -> None:
            loop = asyncio.get_running_loop()
            stop: asyncio.Future[None] = loop.create_future()
            loop.call_later(0.1, lambda: stop.done() or stop.set_result(None))
            from app.hub.server import serve_forever

            await serve_forever(stop)
            await engine.dispose()

        asyncio.run(_cycle())
        return 0

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
