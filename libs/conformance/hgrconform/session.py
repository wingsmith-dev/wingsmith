"""One conversation with one endpoint, kept exactly as it happened.

Everything a probe does goes through here, for two reasons. The first is evidence:
every request and every response is written to the run directory before the probe
sees it, so a finding can be traced to the bytes that produced it. The second is
that the questions being asked are about the wire itself, and a convenience layer
would answer them for us: an SDK that returns a parsed object has already decided
that a repeated `content` key is one key, which is the exact defect one of the
requirements asks about.

So: httpx for transport, and nothing above it. Responses expose the raw text, the
parsed body, and the duplicate keys the parse would otherwise have hidden.
"""

from __future__ import annotations

import json
import time
import threading

from typing import Any
from pathlib import Path
from dataclasses import field
from dataclasses import dataclass

import httpx

from hgrconform.profile import Profile


CHAT_PATH = "/v1/chat/completions"


def dig(body: Any, path: str) -> Any:
    """Read a dotted path out of a response body, or `None` if it does not resolve.

    Integer segments index lists, so `choices.0.message.content` reads the way it is
    written. A path that does not resolve returns `None` and the caller reports a
    named miss; it never raises, because "this endpoint does not have that field" is
    an observation and not an error.
    """
    current = body
    for segment in path.split("."):
        if isinstance(current, list):
            if not segment.isdigit() or int(segment) >= len(current):
                return None
            current = current[int(segment)]
        elif isinstance(current, dict):
            if segment not in current:
                return None
            current = current[segment]
        else:
            return None
    return current


def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Parse a JSON object, and leave a marker when it repeated a key.

    `json.loads` silently keeps the last value for a repeated key, which makes the
    "response must not contain duplicate fields" requirement unobservable through
    any normal parse. The duplicates are recorded under a private key that the
    probes read and nothing else touches.
    """
    seen: dict[str, Any] = {}
    repeated: list[str] = []
    for key, value in pairs:
        if key in seen:
            repeated.append(key)
        seen[key] = value
    if repeated:
        seen[DUPLICATE_KEYS] = repeated
    return seen


DUPLICATE_KEYS = "__duplicate_keys__"


def duplicate_keys(body: Any, prefix: str = "") -> list[str]:
    """Every repeated key in a parsed body, as dotted paths."""
    found: list[str] = []
    if isinstance(body, dict):
        for key in body.get(DUPLICATE_KEYS, []):
            found.append(f"{prefix}{key}")
        for key, value in body.items():
            if key != DUPLICATE_KEYS:
                found.extend(duplicate_keys(value, f"{prefix}{key}."))
    elif isinstance(body, list):
        for index, value in enumerate(body):
            found.extend(duplicate_keys(value, f"{prefix}{index}."))
    return found


@dataclass
class Response:
    """One non-streaming exchange, from the status line down."""

    status: int
    headers: dict[str, str]
    text: str
    elapsed: float
    request: dict[str, Any]
    path: str

    @property
    def body(self) -> Any:
        try:
            return json.loads(self.text, object_pairs_hook=_pairs_hook)
        except json.JSONDecodeError:
            return None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def at(self, path: str) -> Any:
        return dig(self.body, path)


@dataclass
class Stream:
    """One streaming exchange, kept as the ordered list of chunks it arrived as.

    The order and the per-chunk contents are the point. Two of the requirements are
    about *where* a value appears in a stream, not whether it appears at all,
    and a client that folds the chunks into one object as they arrive cannot answer
    either.
    """

    status: int
    headers: dict[str, str]
    chunks: list[Any]
    raw_lines: list[str]
    elapsed: float
    request: dict[str, Any]
    path: str
    done: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self, key: str = "content") -> str:
        parts = [dig(chunk, f"choices.0.delta.{key}") or "" for chunk in self.chunks]
        return "".join(parts)

    def chunks_with(self, path: str) -> list[int]:
        """The indices of the chunks in which a dotted path resolves to a value."""
        return [index for index, chunk in enumerate(self.chunks) if dig(chunk, path) is not None]


@dataclass
class Session:
    """A client bound to one profile, and a place to put what it saw."""

    profile: Profile
    evidence_dir: Path
    exchanges: list[str] = field(default_factory=list)
    _client: httpx.Client | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(timeout=self.profile.timeout, follow_redirects=False)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.profile.token:
            headers["Authorization"] = f"Bearer {self.profile.token}"
        return headers

    def body(self, **overrides: Any) -> dict[str, Any]:
        """A minimal valid chat request for this profile, with overrides applied."""
        request: dict[str, Any] = {
            "model": self.profile.model,
            "messages": [{"role": "user", "content": "Say OK and nothing else."}],
        }
        request.update(overrides)
        return request

    def _route(self, path: str) -> str:
        """The path this vendor actually serves a canonical surface from."""
        return self.profile.routes.get(path, path)

    def _with_extra(self, body: dict[str, Any]) -> dict[str, Any]:
        """Merge the profile's `extra_body` underneath what the probe asked for.

        Applied here, not in `body()` because a probe is free to build its own
        request, the Anthropic surface and the malformed ones do, and a
        request that escaped unpinned would be answered by a different server than the
        rest of the run, which is the one thing `extra_body` exists to prevent. The
        probe's own keys win, so this can only add fields, never rewrite the question.
        """
        if not self.profile.extra_body:
            return dict(body)
        return self.profile.extra_body | dict(body)

    def post(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> Response:
        started = time.monotonic()
        assert self._client is not None
        body = self._with_extra(body)
        path = self._route(path)
        try:
            raw = self._client.post(
                f"{self.profile.base_url}{path}",
                json=body,
                headers=self.headers if headers is None else headers,
            )
            response = Response(
                status=raw.status_code,
                headers=dict(raw.headers),
                text=raw.text,
                elapsed=time.monotonic() - started,
                request=body,
                path=path,
            )
        except httpx.HTTPError as exc:
            response = Response(
                status=0,
                headers={},
                text=f"{type(exc).__name__}: {exc}",
                elapsed=time.monotonic() - started,
                request=body,
                path=path,
            )
        self._record(response.path, response.request, response.status, response.text)
        return response

    def get(self, path: str, headers: dict[str, str] | None = None) -> Response:
        started = time.monotonic()
        assert self._client is not None
        path = self._route(path)
        try:
            raw = self._client.get(
                f"{self.profile.base_url}{path}",
                headers=self.headers if headers is None else headers,
            )
            response = Response(
                status=raw.status_code,
                headers=dict(raw.headers),
                text=raw.text,
                elapsed=time.monotonic() - started,
                request={},
                path=path,
            )
        except httpx.HTTPError as exc:
            response = Response(0, {}, f"{type(exc).__name__}: {exc}", time.monotonic() - started, {}, path)
        self._record(path, {}, response.status, response.text)
        return response

    def stream(self, body: dict[str, Any], path: str = CHAT_PATH) -> Stream:
        """Send a streaming request and keep every chunk, in order, unmerged."""
        started = time.monotonic()
        assert self._client is not None
        request = self._with_extra(body) | {"stream": True}
        path = self._route(path)
        chunks: list[Any] = []
        lines: list[str] = []
        error = ""
        done = False
        try:
            with self._client.stream(
                "POST",
                f"{self.profile.base_url}{path}",
                json=request,
                headers=self.headers,
            ) as raw:
                status = raw.status_code
                headers = dict(raw.headers)
                if raw.status_code >= 400:
                    lines.append(raw.read().decode(errors="replace"))
                else:
                    for line in raw.iter_lines():
                        if not line:
                            continue
                        lines.append(line)
                        if not line.startswith("data:"):
                            continue
                        payload = line[len("data:") :].strip()
                        if payload == "[DONE]":
                            done = True
                            continue
                        try:
                            chunks.append(json.loads(payload, object_pairs_hook=_pairs_hook))
                        except json.JSONDecodeError:
                            error = f"chunk is not JSON: {payload[:120]}"
        except httpx.HTTPError as exc:
            status, headers = 0, {}
            error = f"{type(exc).__name__}: {exc}"

        stream = Stream(
            status=status,
            headers=headers,
            chunks=chunks,
            raw_lines=lines,
            elapsed=time.monotonic() - started,
            request=request,
            path=path,
            done=done,
            error=error,
        )
        self._record(path, request, status, "\n".join(lines), streaming=True)
        return stream

    def _record(self, path: str, request: Any, status: int, text: str, streaming: bool = False) -> None:
        """Write one exchange to disk before any probe reads it.

        The bearer token never reaches this file: it lives in the session's headers
        and the headers are not part of what is recorded.

        Locked, because two probes send their requests concurrently and the evidence
        file is named from the length of the list it is about to be appended to. Two
        unsynchronised threads there pick the same name and one silently overwrites
        the other's evidence: the quietest possible way for this tool to lie. The
        lock covers naming, writing and appending as one step; it does not cover the
        request itself, which is the part worth doing in parallel.
        """
        with self._lock:
            self._write(path, request, status, text, streaming)

    def _write(self, path: str, request: Any, status: int, text: str, streaming: bool) -> None:
        index = len(self.exchanges)
        name = f"{index:03d}-{path.strip('/').replace('/', '-')}{'-stream' if streaming else ''}.json"
        payload = {
            "path": path,
            "request": request,
            "status": status,
            "response": text,
        }
        (self.evidence_dir / name).write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        self.exchanges.append(name)
