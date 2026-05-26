"""A small OpenAI-compatible server that can be told to misbehave in specific ways.

It exists so the probes can be tested without an endpoint, and more importantly so
the one rule that makes this tool worth running can be tested at all: every key a
probe compares must collapse variance that two honest endpoints would differ on.

Three modes:

    QUIRK=none    conforming, and deterministic
    QUIRK=leaky   three planted defects — it names its own infrastructure in an error
                  body, puts `usage` in every stream chunk instead of the last, and
                  emits a field its declared JSON schema forbids
    QUIRK=noisy   conforming, but varies token counts, ids, response ids, tool call
                  ids and chunk counts on every call, the way a real endpoint does

`noisy` is the important one. Two `noisy` instances with different seeds are two
honest endpoints that agree about everything a diff should care about and agree
about nothing else. Any difference the runner reports between them is a probe
comparing something it should not.
"""
import os, sys, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QUIRK = os.environ.get("QUIRK", "none")
SEED = int(os.environ.get("SEED", "0"))
N = [SEED]


def jitter(lo, hi):
    """Deterministic per-process but varying per request, like a real endpoint."""
    N[0] = (N[0] * 1103515245 + 12345) & 0x7FFFFFFF
    return lo + N[0] % max(1, hi - lo)


def tool_id(name):
    """An opaque id that never repeats, which is what a conforming endpoint mints.

    Two instances with different seeds mint different ids for the same call, so any
    probe that compares an id rather than its shape shows up in the twins check.
    """
    return "chatcmpl-tool-%012x" % jitter(1, 2**40)


def wanted_tool(body):
    """The tool this request should be answered with, or None if it should not be.

    Enough of `tool_choice` to be worth pointing a tool probe at: `none` suppresses
    the call, a named choice picks that tool, and anything else takes the first tool
    declared. A turn whose last message is a tool result answers in prose instead,
    the way a model does once it has what it asked for.
    """
    tools = body.get("tools") or []
    choice = body.get("tool_choice", "auto")
    if not tools or choice == "none":
        return None
    if body["messages"] and body["messages"][-1].get("role") == "tool":
        return None
    named = (choice or {}).get("function", {}).get("name") if isinstance(choice, dict) else None
    for tool in tools:
        if tool.get("function", {}).get("name") == named:
            return tool["function"]["name"]
    return tools[0].get("function", {}).get("name")


def returned_result(messages):
    """The sentinel a tool result carried, so a round trip can be seen to have used it."""
    for message in reversed(messages):
        if message.get("role") == "tool":
            try:
                return json.loads(message.get("content") or "{}").get("temperature_c")
            except json.JSONDecodeError:
                return None
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, obj, ctype="application/json"):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        if QUIRK == "leaky":
            self.send_header("X-Served-By", "hangar-gateway-7f9d")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/v1/models":
            return self._send(200, {"object": "list", "data": [{"id": "gpt-oss-120b", "object": "model", "owned_by": "hangar"}]})
        return self._send(404, {"error": {"message": "not found", "type": "invalid_request_error", "code": "404"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        auth = self.headers.get("Authorization", "")

        if not auth.startswith("Bearer ") or "invalid" in auth:
            return self._send(401, {"error": {"message": "invalid api key", "type": "authentication_error", "code": "invalid_api_key"}})

        if self.path == "/v1/responses":
            return self._send(404, {"error": {"message": "unsupported", "type": "invalid_request_error"}})
        if self.path == "/v1/messages":
            return self._messages(body)
        if self.path != "/v1/chat/completions":
            return self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

        if "messages" not in body:
            return self._send(400, {"error": {"message": "messages is required", "type": "invalid_request_error", "code": "missing_field"}})
        if body.get("model") != "gpt-oss-120b":
            msg = "unknown model" if QUIRK != "leaky" else "unknown model on hangar-gateway upstream vllm-0"
            return self._send(404, {"error": {"message": msg, "type": "invalid_request_error", "code": "model_not_found"}})
        if isinstance(body.get("temperature"), (int, float)) and body["temperature"] > 2:
            return self._send(400, {"error": {"message": "temperature out of range", "type": "invalid_request_error", "code": "bad_param"}})

        limit = body.get("max_tokens") or body.get("max_completion_tokens") or 64
        if body.get("max_output_tokens") and QUIRK == "leaky":
            limit = 512

        tool = wanted_tool(body)
        if tool:
            pt = 11 if QUIRK != "noisy" else jitter(9, 14)
            ct = 9 if QUIRK != "noisy" else jitter(5, 15)
            return self._send(200, {
                "id": "chatcmpl-%d" % jitter(1, 10**6), "object": "chat.completion", "created": 1,
                "model": "gpt-oss-120b",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": tool_id(tool), "type": "function", "index": 0,
                     "function": {"name": tool, "arguments": json.dumps({"city": "Lisbon"})}},
                ]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
            })

        fmt = (body.get("response_format") or {}).get("type")
        sentinel = returned_result(body["messages"])
        if fmt == "json_object":
            text = json.dumps({"city": "Lisbon", "population": 548703})
        elif fmt == "json_schema":
            payload = {"city": "Lisbon", "population": 548703, "coastal": True, "districts": ["Alfama", "Belem"]}
            if QUIRK == "leaky":
                payload["extra"] = "not in schema"
            text = json.dumps(payload)
        elif sentinel is not None:
            text = "It is %s degrees there." % sentinel
        elif any("remember" in (m.get("content") or "").lower() for m in body["messages"]):
            text = "4173"
        else:
            text = "OK"

        pt = 11 if QUIRK != "noisy" else jitter(9, 14)
        ct = min(limit, 8) if QUIRK != "noisy" else min(limit, jitter(3, 12))
        usage = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
        if QUIRK == "noisy":
            text = text + " " * jitter(0, 3)

        if body.get("stream"):
            include = (body.get("stream_options") or {}).get("include_usage")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            pieces = [text[:1], text[1:]] if QUIRK != "noisy" else [text[i:i+1] for i in range(len(text))] or [""]
            for i, piece in enumerate(pieces):
                chunk = {"id": "c%d" % jitter(1, 10**6), "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": ({"role": "assistant", "content": piece} if i == 0 else {"content": piece}), "finish_reason": None}]}
                if QUIRK == "leaky" and include:
                    chunk["usage"] = usage
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            final = {"id": "c%d" % jitter(1, 10**6), "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            if include and QUIRK != "leaky":
                final["usage"] = usage
            self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        return self._send(200, {
            "id": "chatcmpl-%d" % jitter(1, 10**6), "object": "chat.completion", "created": 1,
            "model": "gpt-oss-120b",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage,
        })

    def _messages(self, body):
        """The Anthropic Messages surface, far enough to carry a tool call and its result.

        Only the block shapes a tool conversation needs: `tool_use` out, `tool_result`
        in, and prose once a result has come back. Enough for a probe that asks what
        this surface does with the identity of a call to have something to observe.
        """
        if body.get("model") != "gpt-oss-120b":
            return self._send(404, {"type": "error", "error": {"type": "not_found_error", "message": "unknown model"}})

        messages = body.get("messages") or [{}]
        last = messages[-1].get("content")
        answered = isinstance(last, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in last
        )
        tools = body.get("tools") or []
        choice = body.get("tool_choice") or {}
        pt = 11 if QUIRK != "noisy" else jitter(9, 14)
        ct = 9 if QUIRK != "noisy" else jitter(5, 15)

        if tools and choice.get("type") != "none" and not answered:
            name = tools[0].get("name")
            content = [{"type": "tool_use", "id": tool_id(name), "name": name, "input": {"city": "Lisbon"}}]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": "OK"}]
            stop = "end_turn"

        return self._send(200, {
            "id": "msg_%d" % jitter(1, 10**6), "type": "message", "role": "assistant",
            "model": "gpt-oss-120b", "content": content, "stop_reason": stop,
            "stop_sequence": None, "usage": {"input_tokens": pt, "output_tokens": ct},
        })


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
