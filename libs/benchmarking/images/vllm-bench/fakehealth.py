"""A server that is always healthy, for `sweep serve` against a remote endpoint.

`sweep serve` requires `--serve-cmd` and probes `GET /health` on the host and
port it parses out of that string before every run. When the thing under test is
a gateway we did not start, point `--serve-cmd` at this and `--base-url` at the
real endpoint. Pass `--after-bench-cmd true` as well, or the sweep POSTs
`/reset_prefix_cache` here and refuses the 200 it gets back.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def _ok(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST = _ok

    def log_message(self, fmt: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
