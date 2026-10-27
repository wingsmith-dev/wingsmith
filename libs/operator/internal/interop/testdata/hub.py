"""The real hub, on a real socket, with a control surface the interop harness drives it through.

This is not a second implementation of the protocol: every frame is produced and consumed by
`app.hub.session.OperatorSession`, the same object the plane's hub runs. What lives here is
process wiring: an ephemeral port, a `MemorySink` instead of the database sink, a `DesiredSource`
fed from the golden fixture corpus, and a handful of HTTP control routes that let a test provoke
the situations the contract specifies but a healthy hub never produces on its own: a dropped
connection, a forgotten session, a deliberate sequence gap, a duplicate, a sink that refuses a
frame, a peer that goes silent.

Authentication is the hub's own: one cluster is enrolled at startup with a freshly minted
per-cluster enrolment token, which is announced on stdout beside the port, and only that token
opens a session, bound to that cluster. There is no database, so the enrolment lives in memory.

The control routes are served by the same `process_request` hook that answers `/health`, so the
harness needs one port and no second server.

Run from libs/plane: `uv run python ../operator/internal/interop/testdata/hub.py --cluster-id ...`.
"""

import sys
import http
import json
import asyncio
import argparse

from typing import Any
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

from msgspec import Struct
from msgspec import json as msgjson
from websockets.asyncio.server import Request
from websockets.asyncio.server import Response
from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve

from app.auth import Principal
from app.utils import logs
from app.hub.sink import MemorySink
from app.hub.state import SessionRegistry
from app.hub.session import SessionConfig
from app.hub.session import OperatorSession
from app.auth.manager import AuthManager
from app.hub.messages import MESSAGE_TYPES
from app.hub.messages import RawEnvelope


FIXTURES = Path(__file__).resolve().parents[5] / "libs" / "packages" / "contract" / "fixtures"


def fixture_payload(type_name: str) -> Struct:
    """Decodes the canonical example of `type_name` into its registered Struct."""
    document = json.loads((FIXTURES / f"{type_name}.json").read_text())
    struct = MESSAGE_TYPES[type_name]
    return msgjson.decode(json.dumps(document["payload"]).encode(), type=struct)


class ListDesiredSource:
    """A `DesiredSource` that yields a fixed list, so a full resync has something to stream."""

    def __init__(self, items: list[tuple[str, Struct]]) -> None:
        self.items = items

    async def iter_desired(self, cluster_id: str, scope: str):
        for type_name, payload in self.items:
            yield type_name, payload


class TapConnection:
    """A `Connection` that records what the session sent and can stop sending it.

    Recording gives `/dup` a real frame to re-send instead of a re-encoded imitation. Muting is
    how the harness makes the hub go silent without killing the socket, which is the only way to
    test that the operator's liveness deadline, not TCP, is what tears the session down.
    """

    def __init__(self, inner: ServerConnection, harness: "Harness") -> None:
        self.inner = inner
        self.harness = harness

    async def send(self, message: Any) -> None:
        self.harness.sent.append(message)
        if self.harness.muted:
            return
        await self.inner.send(message)

    async def recv(self) -> Any:
        return await self.inner.recv()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self.inner.close(code, reason)


class Harness:
    """Process-wide state the control routes read and write."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.registry = SessionRegistry(args.replay_capacity)
        self.sink = MemorySink()
        self.desired = ListDesiredSource([(args.desired_type, fixture_payload(args.desired_type))] * args.desired_count)
        self.config = SessionConfig(
            handshake_timeout=args.handshake_timeout,
            ping_interval=args.ping_interval,
            liveness_timeout=args.liveness_timeout,
            send_queue_size=args.send_queue,
            replay_capacity=args.replay_capacity,
        )
        self.session: OperatorSession | None = None
        self.tap: TapConnection | None = None
        self.sent: list[str] = []
        self.muted = False
        self.connections = 0
        self.auth_headers: list[str] = []
        self.closed_by_hub = 0
        self.cluster_id: str = args.cluster_id
        self.token, self.token_hash = AuthManager.mint_enrolment_token()

    def resolve(self, header: str | None) -> Principal | None:
        """The enrolled cluster's principal for its own token, and nothing for any other."""
        token = AuthManager.bearer_token(header)
        if not AuthManager.looks_like_enrolment(token):
            return None
        assert token is not None
        if AuthManager.hash_enrolment_token(token) != self.token_hash:
            return None
        return AuthManager.operator_principal(cluster_uid=self.cluster_id, cluster_name="interop")

    def state(self) -> dict[str, Any]:
        session = self.session
        state = session.state if session is not None else None
        return {
            "connections": self.connections,
            "auth_headers": self.auth_headers,
            "closed_by_hub": self.closed_by_hub,
            "session": None
            if state is None
            else {
                "session_id": state.session_id,
                "cluster_id": state.cluster_id,
                "next_seq": state.next_seq,
                "expected_seq": state.expected_seq,
                "ack_cursor": state.ack_cursor,
                "connection_count": state.connection_count,
                "replay_pending": len(state.replay),
            },
            "sessions": len(self.registry),
            "sent": [_summarise(raw) for raw in self.sent],
            "received": {
                "observed": [{"type": f.type, "seq": f.seq, "reply_to": f.reply_to} for f in self.sink.observed],
                "events": [{"type": f.type, "seq": f.seq} for f in self.sink.events],
                "operations": [{"type": f.type, "seq": f.seq} for f in self.sink.operations],
                "resyncs": [
                    {"phase": r[0], "request_id": r[2], "count": r[3], "complete": r[4]} for r in self.sink.resyncs
                ],
            },
        }


def _summarise(raw: str) -> dict[str, Any]:
    envelope = msgjson.decode(raw.encode(), type=RawEnvelope)
    return {
        "type": envelope.type,
        "id": envelope.id,
        "seq": envelope.seq,
        "ack": envelope.ack,
        "reply_to": envelope.reply_to,
    }


HUB_PATH = "/v1/hub/sync"

HARNESS: Harness


async def _control(path: str, query: dict[str, list[str]]) -> tuple[http.HTTPStatus, Any]:
    harness = HARNESS
    session = harness.session
    match path:
        case "/control/state":
            return http.HTTPStatus.OK, harness.state()

        case "/control/push":
            if session is None or session.state is None:
                return http.HTTPStatus.CONFLICT, {"error": "no live session"}
            type_name = query.get("type", ["deployment.desired"])[0]
            count = int(query.get("n", ["1"])[0])
            if query.get("gap"):
                session.state.take_seq()
            seqs = []
            for _ in range(count):
                seqs.append(session.state.next_seq)
                await session.send_desired(type_name, fixture_payload(type_name))
            return http.HTTPStatus.OK, {"seqs": seqs}

        case "/control/duplicate":
            if session is None:
                return http.HTTPStatus.CONFLICT, {"error": "no live session"}
            for raw in reversed(harness.sent):
                if msgjson.decode(raw.encode(), type=RawEnvelope).seq is not None:
                    await session.connection.send(raw)
                    return http.HTTPStatus.OK, {"resent": _summarise(raw)}
            return http.HTTPStatus.CONFLICT, {"error": "nothing sequenced has been sent"}

        case "/control/kill":
            if harness.tap is None:
                return http.HTTPStatus.CONFLICT, {"error": "no live connection"}
            harness.closed_by_hub += 1
            transport = getattr(harness.tap.inner, "transport", None)
            if transport is not None:
                transport.abort()
            else:  # pragma: no cover - every asyncio connection has a transport
                await harness.tap.inner.close(1011, "killed by harness")
            return http.HTTPStatus.OK, {"killed": True}

        case "/control/forget":
            if session is None or session.state is None:
                return http.HTTPStatus.CONFLICT, {"error": "no live session"}
            harness.registry.drop(session.state.session_id)
            return http.HTTPStatus.OK, {"forgot": session.state.session_id}

        case "/control/fail":
            types = [t for t in query.get("types", [""])[0].split(",") if t]
            harness.sink.fail_types = set(types)
            return http.HTTPStatus.OK, {"fail_types": sorted(harness.sink.fail_types)}

        case "/control/mute":
            harness.muted = query.get("on", ["1"])[0] not in ("0", "false")
            return http.HTTPStatus.OK, {"muted": harness.muted}

        case "/control/reset":
            harness.sent.clear()
            harness.sink.observed.clear()
            harness.sink.events.clear()
            harness.sink.operations.clear()
            harness.sink.resyncs.clear()
            return http.HTTPStatus.OK, {"reset": True}

    return http.HTTPStatus.NOT_FOUND, {"error": f"no control route {path}"}


async def _process_request(connection: ServerConnection, request: Request) -> Response | None:
    parsed = urlparse(request.path)
    if parsed.path == "/health":
        return connection.respond(http.HTTPStatus.OK, "ok\n")
    if parsed.path.startswith("/control/"):
        status, body = await _control(parsed.path, parse_qs(parsed.query))
        return connection.respond(status, json.dumps(body) + "\n")
    if parsed.path != HUB_PATH:
        return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")
    header = request.headers.get("Authorization")
    HARNESS.auth_headers.append(header or "")
    if HARNESS.resolve(header) is None:
        return connection.respond(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")
    return None


async def _handler(connection: ServerConnection) -> None:
    harness = HARNESS
    harness.connections += 1
    principal = harness.resolve(connection.request.headers.get("Authorization"))
    if principal is None:
        await connection.close(code=4401, reason="unauthorized")
        return
    tap = TapConnection(connection, harness)
    session = OperatorSession(
        tap,
        principal,
        registry=harness.registry,
        sink=harness.sink,
        desired=harness.desired,
        config=harness.config,
    )
    harness.tap = tap
    harness.session = session
    await session.run()


async def main() -> int:
    global HARNESS, HUB_PATH

    parser = argparse.ArgumentParser(prog="interop-hub")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--path", default=HUB_PATH)
    parser.add_argument("--ping-interval", type=float, default=10.0)
    parser.add_argument("--liveness-timeout", type=float, default=30.0)
    parser.add_argument("--handshake-timeout", type=float, default=10.0)
    parser.add_argument("--send-queue", type=int, default=1024)
    parser.add_argument("--replay-capacity", type=int, default=4096)
    parser.add_argument("--desired-type", default="deployment.desired")
    parser.add_argument("--desired-count", type=int, default=3)
    parser.add_argument("--cluster-id", required=True)
    args = parser.parse_args()

    logs.configure("interop-hub")
    HUB_PATH = args.path
    HARNESS = Harness(args)

    async with serve(
        _handler,
        args.host,
        args.port,
        process_request=_process_request,
        ping_interval=None,
    ) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        # The token first: the harness reads it before it starts waiting on the port.
        print(f"HUB_TOKEN {HARNESS.token}", flush=True)
        print(f"HUB_PORT {port}", flush=True)
        await asyncio.get_running_loop().create_future()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
