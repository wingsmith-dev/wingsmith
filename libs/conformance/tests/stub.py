"""A small OpenAI-compatible server that can be told to misbehave in specific ways.

It exists so the probes can be tested without an endpoint, and more importantly so
the one rule that makes this tool worth running can be tested at all: every key a
probe compares must collapse variance that two honest endpoints would differ on.

Three modes:

    QUIRK=none    conforming, and deterministic
    QUIRK=leaky   three planted defects — it names its own infrastructure in an error
                  body and a header, puts `usage` in every stream chunk instead of the
                  last, and emits a field its declared JSON schema forbids
    QUIRK=noisy   conforming, but varies everything two honest endpoints serving one
                  model may vary: ids, completion token counts, chunk counts, trailing
                  whitespace, and the model's own decisions — whether it calls a tool
                  when the choice is left to it, how many, whether it uses a tool
                  result, whether it reads an image or a video correctly, whether
                  it recalls an earlier turn, and whether one reasoning request in
                  a run ends early with an empty answer

`noisy` is the important one. Two `noisy` instances with different seeds are two
honest endpoints that agree about everything a diff should care about and agree
about nothing else. Any difference the runner reports between them is a probe
comparing something it should not, and that includes a single sample of a model
decision, which differs between two runs of one model as readily as an id does.

Prompt token counts are a function of the request in every mode, because two
endpoints serving one model tokenize one prompt identically. Completion counts are
not, because where a model stops is its own decision.

The stub serves enough of each surface for every probe to observe something: image,
audio and video content parts (a real clip is accepted, four zero bytes are not),
thinking switched by `chat_template_kwargs.thinking`, streamed tool calls, and the
Anthropic Messages surface far enough to carry a tool conversation.
"""

import os
import sys
import json
import base64
import threading

from http.server import ThreadingHTTPServer
from http.server import BaseHTTPRequestHandler


QUIRK = os.environ.get("QUIRK", "none")
SEED = int(os.environ.get("SEED", "0"))
NOISY = QUIRK == "noisy"
LEAKY = QUIRK == "leaky"
MODEL = "gpt-oss-120b"
MAX_BODY = 32 * 1024 * 1024
N = [SEED]
# One stray EOS per run, on the noisy twin whose coin lands yes.
STRAY_EOS = [NOISY and SEED % 2 == 1]
STRAY_LOCK = threading.Lock()


def jitter(lo, hi):
    """Deterministic per process, varying per call, like a real endpoint."""
    N[0] = (N[0] * 1103515245 + 12345) & 0x7FFFFFFF
    return lo + N[0] % max(1, hi - lo)


def coin():
    """A model decision: always yes in the deterministic modes, the seed's parity when noisy.

    Parity rather than a random draw, so that two noisy twins with seeds of different
    parity decide every question differently. A compare key that depends on a decision
    then shows up on every run of the self-check instead of on the runs where a draw
    happened to differ.
    """
    return not NOISY or SEED % 2 == 1


def tool_id():
    """An opaque id that never repeats, which is what a conforming endpoint mints."""
    return "chatcmpl-tool-%012x" % jitter(1, 2**40)


def error(message, kind="invalid_request_error", code=None):
    return {"error": {"message": message, "type": kind, "code": code}}


def text_of(content):
    """The text of a message's content, whether it is a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def media_of(messages):
    """The media parts of the last user message, in order, as (kind, payload) pairs."""
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if not isinstance(content, list):
                return []
            found = []
            for part in content:
                kind = part.get("type") if isinstance(part, dict) else None
                if kind == "image_url":
                    found.append(("image", part["image_url"].get("url", "")))
                elif kind == "video_url":
                    found.append(("video", part["video_url"].get("url", "")))
                elif kind == "input_audio":
                    found.append(("audio", part["input_audio"].get("data", "")))
            return found
    return []


def decoded(url):
    if not url.startswith("data:") or "," not in url:
        return None
    try:
        return base64.b64decode(url.split(",", 1)[1])
    except ValueError:
        return None


def refuse_media(media):
    """The error a media part earns, or None when every part decodes."""
    for kind, payload in media:
        if kind == "audio":
            return error("invalid audio payload: failed to load")
        data = decoded(payload)
        if data is None:
            return error("fetching remote media is disabled")
        if kind == "image" and not data.startswith(b"\x89PNG"):
            return error("invalid image: failed to load")
        if kind == "video" and data[4:8] != b"ftyp":
            return error("invalid video: failed to load")
    return None


def prompt_tokens(messages, media):
    """The same count for the same request, whichever stub answers it."""
    text = sum(len(text_of(message.get("content"))) for message in messages)
    cost = 0
    for kind, payload in media:
        data = decoded(payload) or b""
        cost += 64 if kind == "image" else min(1024, 32 + len(data) // 4096)
    return 4 * len(messages) + text // 4 + cost


def answer(messages, media):
    """What the model says. Every reading of a picture or a clip is a decision when noisy."""
    prompt = text_of(messages[-1].get("content")).lower() if messages else ""
    if messages[-1].get("role") == "tool":
        used = coin()
        try:
            sentinel = json.loads(messages[-1].get("content") or "{}").get("temperature_c")
        except json.JSONDecodeError:
            sentinel = None
        return f"It is {sentinel} degrees there." if used and sentinel is not None else "It is warm there."
    if media:
        kinds = [kind for kind, _ in media]
        if "two media items" in prompt:
            colours = ["blue" if kind == "image" else "red" for kind in kinds]
            return ", ".join(colours if coin() else list(reversed(colours)))
        if "which way" in prompt:
            return "red, left-to-right" if coin() else "I cannot tell from these frames."
        if "what colour" in prompt:
            return "Red." if coin() else "It is too dark to tell."
        return "A red square moves across a black background."
    if any("remember" in text_of(message.get("content")).lower() for message in messages):
        return "4173" if coin() else "I don't recall a number."
    if "train leaves" in prompt:
        with STRAY_LOCK:
            stray, STRAY_EOS[0] = STRAY_EOS[0], False
        if stray:
            return ""
    return "OK"


def wanted_tools(body):
    """The tools this request is answered with: a contract where the request sets one, a decision where it does not."""
    tools = [tool["function"]["name"] for tool in body.get("tools") or [] if tool.get("function")]
    choice = body.get("tool_choice", "auto")
    messages = body.get("messages") or [{}]
    if not tools or choice == "none" or messages[-1].get("role") == "tool":
        return []
    if isinstance(choice, dict) and choice.get("type") == "function":
        named = choice.get("function", {}).get("name")
        return [named] if named in tools else []
    if isinstance(choice, dict) and choice.get("type") == "allowed_tools":
        mode = choice["allowed_tools"].get("mode", "auto")
        return [tools[0]] if mode == "required" or coin() else []
    if choice == "required":
        return [tools[0]]
    # `auto`: whether to call, how many, and which, are all the model's to decide.
    prompt = text_of(messages[-1].get("content")).lower()
    if "flight" in prompt:
        return ["book_flight"] if NOISY and coin() else []
    if "do not use any tool" in prompt:
        return [tools[0]] if NOISY and coin() else []
    if not coin():
        return []
    return tools[:2] if len(tools) > 1 and coin() else tools[:1]


def reasoning_wanted(body):
    kwargs = body.get("chat_template_kwargs") or {}
    return kwargs.get("thinking") is True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _headers(self, code, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if LEAKY:
            self.send_header("X-Served-By", "hangar-gateway-7f9d")

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self._headers(code, "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _event(self, obj):
        self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())

    def do_GET(self):
        if self.path == "/v1/models":
            owner = "hangar" if LEAKY else "stub"
            return self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": owner}]})
        return self._send(404, error("not found", code="404"))

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if len(raw) > MAX_BODY:
            return self._send(413, error("request body too large", code="payload_too_large"))
        body = json.loads(raw or b"{}")
        auth = self.headers.get("Authorization", "")

        if not auth.startswith("Bearer ") or "invalid" in auth:
            return self._send(401, error("invalid api key", "authentication_error", "invalid_api_key"))
        if self.path == "/v1/responses":
            return self._send(404, error("unsupported"))
        if self.path == "/v1/messages":
            return self._messages(body)
        if self.path != "/v1/chat/completions":
            return self._send(404, error("not found"))
        return self._chat(body)

    def _chat(self, body):
        if "messages" not in body:
            return self._send(400, error("messages is required", code="missing_field"))
        if body.get("model") != MODEL:
            message = "unknown model on hangar-gateway upstream vllm-0" if LEAKY else "unknown model"
            return self._send(404, error(message, code="model_not_found"))
        if isinstance(body.get("temperature"), (int, float)) and body["temperature"] > 2:
            return self._send(400, error("temperature out of range", code="bad_param"))

        messages = body["messages"]
        media = media_of(messages)
        refused = refuse_media(media)
        if refused:
            return self._send(400, refused)

        limit = body.get("max_tokens") or body.get("max_completion_tokens") or 64
        if body.get("max_output_tokens") and LEAKY:
            limit = 512
        pt = prompt_tokens(messages, media)
        reasoning = "Working it out step by step." if reasoning_wanted(body) else None
        tools = wanted_tools(body)
        if tools:
            ct = 9 if not NOISY else jitter(5, 15)
            usage = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
            calls = [
                {
                    "id": tool_id(),
                    "type": "function",
                    "index": index,
                    "function": {"name": name, "arguments": json.dumps({"city": "Lisbon"})},
                }
                for index, name in enumerate(tools)
            ]
            if body.get("stream"):
                return self._stream_calls(calls, usage, body)
            message = {"role": "assistant", "content": None, "tool_calls": calls}
            return self._complete(message, "tool_calls", usage)

        fmt = (body.get("response_format") or {}).get("type")
        if fmt == "json_object":
            text = json.dumps({"city": "Lisbon", "population": 548703})
        elif fmt == "json_schema":
            payload = {"city": "Lisbon", "population": 548703, "coastal": True, "districts": ["Alfama", "Belem"]}
            if LEAKY:
                payload["extra"] = "not in schema"
            text = json.dumps(payload)
        else:
            text = answer(messages, media)
        if NOISY:
            text = text + " " * jitter(0, 3)

        ct = min(limit, 8) if not NOISY else min(limit, jitter(3, 12))
        usage = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
        if body.get("stream"):
            return self._stream_text(text, reasoning, usage, body)
        message = {"role": "assistant", "content": text}
        if reasoning:
            message["reasoning_content"] = reasoning
        return self._complete(message, "stop", usage)

    def _complete(self, message, finish, usage):
        return self._send(
            200,
            {
                "id": "chatcmpl-%d" % jitter(1, 10**6),
                "object": "chat.completion",
                "created": 1,
                "model": MODEL,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": usage,
            },
        )

    def _chunk(self, delta, finish=None):
        return {
            "id": "c%d" % jitter(1, 10**6),
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def _stream(self, deltas, finish, usage, body):
        """Send deltas as chunks, then the final one. Where `usage` goes is the leaky defect."""
        include = (body.get("stream_options") or {}).get("include_usage")
        self._headers(200, "text/event-stream")
        self.end_headers()
        for delta in deltas:
            chunk = self._chunk(delta)
            if LEAKY and include:
                chunk["usage"] = usage
            self._event(chunk)
        final = self._chunk({}, finish)
        if include and not LEAKY:
            final["usage"] = usage
        self._event(final)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _stream_text(self, text, reasoning, usage, body):
        pieces = [text[i : i + 1] for i in range(len(text))] if NOISY else [text[:1], text[1:]]
        deltas = []
        if reasoning:
            deltas += [{"reasoning_content": word + " "} for word in reasoning.split()]
        deltas += [{"content": piece} for piece in pieces or [""]]
        deltas[0] = {"role": "assistant", **deltas[0]}
        return self._stream(deltas, "stop", usage, body)

    def _stream_calls(self, calls, usage, body):
        deltas = [{"role": "assistant", "content": None}]
        for call in calls:
            arguments = call["function"]["arguments"]
            half = len(arguments) // 2
            head = {"index": call["index"], "id": call["id"], "type": "function"}
            deltas.append({"tool_calls": [{**head, "function": {"name": call["function"]["name"], "arguments": ""}}]})
            for piece in (arguments[:half], arguments[half:]):
                deltas.append({"tool_calls": [{"index": call["index"], "function": {"arguments": piece}}]})
        return self._stream(deltas, "tool_calls", usage, body)

    def _messages(self, body):
        """The Anthropic Messages surface, far enough to carry a tool call and its result."""
        if body.get("model") != MODEL:
            return self._send(404, {"type": "error", "error": {"type": "not_found_error", "message": "unknown model"}})

        messages = body.get("messages") or [{}]
        last = messages[-1].get("content")
        answered = isinstance(last, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in last
        )
        tools = body.get("tools") or []
        choice = body.get("tool_choice") or {}
        pt = 4 * len(messages) + sum(len(text_of(m.get("content"))) for m in messages) // 4
        ct = 9 if not NOISY else jitter(5, 15)

        if tools and choice.get("type") != "none" and not answered:
            name = tools[0].get("name")
            content = [{"type": "tool_use", "id": tool_id(), "name": name, "input": {"city": "Lisbon"}}]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": "OK"}]
            stop = "end_turn"

        return self._send(
            200,
            {
                "id": "msg_%d" % jitter(1, 10**6),
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": content,
                "stop_reason": stop,
                "stop_sequence": None,
                "usage": {"input_tokens": pt, "output_tokens": ct},
            },
        )


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
