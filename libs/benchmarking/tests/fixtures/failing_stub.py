"""The endpoint the captured harness exports in this directory were recorded against.

A streaming chat-completions server that answers every request with `max_tokens`
words and fails every Nth request with a 500. It is not imported by the tests; it is
here so a fixture can be captured again when a harness pin moves:

    python tests/fixtures/failing_stub.py 8000 4 &
    aiperf profile --model stub-model --url http://127.0.0.1:8000 --endpoint-type chat \\
        --streaming --tokenizer gpt2 --concurrency 4 --request-count 20 \\
        --synthetic-input-tokens-mean 64 --output-tokens-mean 16 \\
        --extra-inputs ignore_eos:true --ui simple --artifact-dir /tmp/aiperf

and then replace the local artifact directory in the export with `/artifacts`. With
N = 4 that is 15 completed requests and 5 failed ones; with N = 0 nothing fails.
"""

import sys
import json
import time
import threading

from http.server import ThreadingHTTPServer
from http.server import BaseHTTPRequestHandler


FAIL_EVERY = int(sys.argv[2]) if len(sys.argv) > 2 else 4
COUNT = [0]
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json(200, {"object": "list", "data": [{"id": "stub-model", "object": "model"}]})

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        with LOCK:
            COUNT[0] += 1
            index = COUNT[0]
        if FAIL_EVERY and index % FAIL_EVERY == 0:
            return self._json(500, {"error": {"message": "stub failure", "type": "server_error", "code": 500}})

        words = int(request.get("max_completion_tokens") or request.get("max_tokens") or 16)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(payload):
            data = ("data: " + (payload if isinstance(payload, str) else json.dumps(payload)) + "\n\n").encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

        def delta(content, finish=None):
            return {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": request.get("model"),
                "choices": [{"index": 0, "delta": content, "finish_reason": finish}],
            }

        for _ in range(words):
            time.sleep(0.002)
            chunk(delta({"content": " word"}))
        last = delta({}, "length")
        last["usage"] = {"prompt_tokens": 64, "completion_tokens": words, "total_tokens": 64 + words}
        chunk(last)
        chunk("[DONE]")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
