from __future__ import annotations

import hashlib

from typing import Any
from logging import getLogger
from dataclasses import field
from dataclasses import dataclass

import httpx

from app.config import settings


logger = getLogger(__name__)

_BASE_URL = "https://huggingface.co"

_TIMEOUT = 20.0

WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".gguf")
"""Extensions whose bytes are the model.

The digest covers these and nothing else: `config.json`, tokenizer files and the
README are edited routinely: a typo fix in the card would otherwise mint a new
artifact identity for weights that never changed.
"""

_FORMAT_BY_SUFFIX = {
    ".safetensors": "safetensors",
    ".bin": "bin",
    ".pt": "bin",
    ".gguf": "gguf",
}


class HuggingFaceError(Exception):
    """Base for every failure this module reports.

    Typed instead of letting `httpx.HTTPStatusError` escape: the caller turns
    these into API errors, and a transport exception carries a request URL and a
    bearer header into a response body that should only say what went wrong.
    """


class HuggingFaceNotFound(HuggingFaceError):
    """No such model on the Hub: a 404. Distinct from gated, which also hides repos."""


class HuggingFaceGated(HuggingFaceError):
    """The repo exists but this token cannot read it: 401 or 403.

    Separate from `HuggingFaceNotFound` because the remedy is different: one is a
    typo in the slug, the other is a licence to accept or a token to configure.
    """


class HuggingFaceUnavailable(HuggingFaceError):
    """The Hub failed or did not answer: 5xx, timeout, connection error. Retryable."""


@dataclass(frozen=True)
class HuggingFaceWeightFile:
    """One weight file in the resolved tree.

    `oid` is `None` when the file is committed to git, not LFS-tracked, in
    which case the Hub exposes no content hash for it at all and the tree alone
    cannot address the bytes.
    """

    path: str
    size: int
    oid: str | None


@dataclass(frozen=True)
class HuggingFaceModel:
    """Everything the plane needs to mint an artifact from a Hub slug.

    `digest` is derived here, not asserted by the client, so two callers
    registering the same weights land on the same identity. When
    `content_addressable` is false the Hub gave no oid for some weight file and
    `digest` is a name of the form `hf:SLUG@SHA`; the caller must record it with
    `verified` false, because nothing has hashed any bytes.
    """

    slug: str
    revision: str
    digest: str
    content_addressable: bool
    size_bytes: int
    parameter_count: int | None
    format: str | None
    licence: str | None
    gated: bool
    config: dict[str, Any]
    weight_files: tuple[HuggingFaceWeightFile, ...] = ()
    unaddressable_files: tuple[str, ...] = ()
    sidecar_files: tuple[str, ...] = ()
    tokenizer_config: dict[str, Any] = field(default_factory=dict)
    files: tuple[str, ...] = ()
    """Every file at the repo root, and `tokenizer_config.json` decoded.

    Both are here for the same reason `config.json` is: a frontend cannot serve a
    model whose tokenizer and chat template it cannot assemble, and the only
    moment that can be established without a cluster is registration. Read once,
    stored, and never fetched again -- the same rule the recipe corpora follow.
    """


class HuggingFaceClient:
    """Async wrapper around the public HuggingFace Hub API.

    Every call after the first pins the resolved commit sha instead of `main`:
    a slug inspected twice seconds apart must not disagree because a push landed
    between the tree read and the config read.
    """

    def __init__(
        self,
        token: str | None = None,
        base_url: str = _BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`token` falls back to `HF_TOKEN` in settings.

        TODO: gated repos (Llama and friends) need a decision about whose token is
        used: a single fleet-wide plane token accepts the licence on behalf of
        every tenant, which is a legal position and not just a config choice.
        """
        self._token = token if token is not None else (settings.HF_TOKEN or None)
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._headers = {"Accept": "application/json"}
        if self._token:
            self._headers["Authorization"] = f"Bearer {self._token}"

    async def inspect(self, slug: str) -> HuggingFaceModel:
        """Resolve a slug to a pinned revision, a derived digest, and its metadata.

        Raises:
            HuggingFaceNotFound: The slug does not exist.
            HuggingFaceGated: The repo needs an authorization this client lacks.
            HuggingFaceUnavailable: The Hub errored or did not answer in time.
        """
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            info = await self._json(client, f"{self._base_url}/api/models/{slug}", slug)
            sha = info.get("sha") or "main"
            entries = await self._tree(client, slug, sha)
            config = await self._config(client, slug, sha)
            tokenizer_config = await self._json_file(client, slug, sha, "tokenizer_config.json")

        return self._build(slug, sha, info, config, entries, tokenizer_config)

    async def resolve_revision(self, slug: str) -> str:
        """The commit sha `main` currently points at. Exposed for callers pinning by hand."""
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            info = await self._json(client, f"{self._base_url}/api/models/{slug}", slug)
        return str(info.get("sha") or "main")

    async def _tree(self, client: httpx.AsyncClient, slug: str, sha: str) -> list[dict[str, Any]]:
        url = f"{self._base_url}/api/models/{slug}/tree/{sha}"
        params: dict[str, Any] | None = {"expand": 1, "recursive": 1}
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        while url:
            response = await self._get(client, url, slug, params=params)
            page = response.json()
            if isinstance(page, list):
                entries.extend(item for item in page if isinstance(item, dict))
            nxt = _next_link(response.headers.get("link") or response.headers.get("Link"))
            if not nxt or nxt in seen:
                break
            seen.add(nxt)
            url, params = nxt, None
        return entries

    async def _config(self, client: httpx.AsyncClient, slug: str, sha: str) -> dict[str, Any]:
        """A missing `config.json` is not a failure: GGUF repos carry no such file."""
        return await self._json_file(client, slug, sha, "config.json")

    async def _json_file(self, client: httpx.AsyncClient, slug: str, sha: str, path: str) -> dict[str, Any]:
        """One small JSON file from the repo, or `{}` when the repo does not carry it.

        Absence is an answer here and not an error. A repo with no
        `tokenizer_config.json` is a repo no frontend can build a prompt
        formatter from, which is a note on the draft instead of a failure to
        register -- the artifact may never be served as a chat model at all.
        """
        try:
            response = await self._get(client, f"{self._base_url}/{slug}/resolve/{sha}/{path}", slug)
        except HuggingFaceNotFound:
            return {}
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    async def _json(self, client: httpx.AsyncClient, url: str, slug: str) -> dict[str, Any]:
        response = await self._get(client, url, slug)
        body = response.json()
        return body if isinstance(body, dict) else {}

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        slug: str,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await client.get(url, headers=self._headers, params=params)
        except httpx.HTTPError as exc:
            raise HuggingFaceUnavailable(f"huggingface did not answer for {slug}") from exc

        status = response.status_code
        if status == 404:
            raise HuggingFaceNotFound(f"no such model on huggingface: {slug}")
        if status in (401, 403):
            raise HuggingFaceGated(f"huggingface repo {slug} requires authorization")
        if status >= 500:
            raise HuggingFaceUnavailable(f"huggingface returned {status} for {slug}")
        if status >= 400:
            raise HuggingFaceError(f"huggingface returned {status} for {slug}")
        return response

    def _build(
        self,
        slug: str,
        sha: str,
        info: dict[str, Any],
        config: dict[str, Any],
        entries: list[dict[str, Any]],
        tokenizer_config: dict[str, Any] | None = None,
    ) -> HuggingFaceModel:
        names = tuple(
            entry["path"]
            for entry in entries
            if isinstance(entry.get("path"), str) and entry.get("type") != "directory"
        )
        weights: list[HuggingFaceWeightFile] = []
        for entry in entries:
            path = entry.get("path")
            if not isinstance(path, str) or entry.get("type") == "directory":
                continue
            if not _is_weight(path):
                continue
            lfs = entry.get("lfs") or {}
            oid = lfs.get("oid") if isinstance(lfs, dict) else None
            size = lfs.get("size") if isinstance(lfs, dict) else None
            if size is None:
                size = entry.get("size") or 0
            weights.append(HuggingFaceWeightFile(path=path, size=int(size), oid=oid or None))

        weights, sidecars = _served_weights(weights)
        unaddressable = tuple(w.path for w in weights if w.oid is None)
        content_addressable = bool(weights) and not unaddressable

        if content_addressable:
            digest = content_digest(weights)
        else:
            digest = f"hf:{slug}@{sha}"

        safetensors = info.get("safetensors") or {}
        total = safetensors.get("total") if isinstance(safetensors, dict) else None
        gated_raw = info.get("gated", False)

        return HuggingFaceModel(
            slug=slug,
            revision=sha,
            digest=digest,
            content_addressable=content_addressable,
            size_bytes=sum(w.size for w in weights),
            parameter_count=int(total) if total else None,
            format=_format_of(weights),
            licence=_licence_of(info),
            gated=gated_raw not in (False, None),
            config=config,
            weight_files=tuple(weights),
            unaddressable_files=unaddressable,
            sidecar_files=sidecars,
            tokenizer_config=dict(tokenizer_config or {}),
            files=names,
        )


def _served_weights(
    weights: list[HuggingFaceWeightFile],
) -> tuple[list[HuggingFaceWeightFile], tuple[str, ...]]:
    """Splits the weights the engine loads from the extra copies a repo ships beside them.

    `openai/gpt-oss-20b` carries its served safetensors shards at the root and two more
    full copies of the same parameters under `original/` and `metal/`. Counting all three
    trebles `size_bytes`, and, the reason this is not merely cosmetic, puts them in the
    digest, so re-uploading the Metal build would change the identity of weights that did
    not change. Sidecars are a packaging detail of the repository, not of the model.

    Root-level files win when there are any, because that is where a transformers-style
    repo puts what it serves. A repo that keeps its only weights in a subdirectory, a
    GGUF conversion, most often, has no root-level files to prefer, so everything counts
    and nothing is dropped.
    """
    weights.sort(key=lambda w: w.path)
    root = [w for w in weights if "/" not in w.path]
    if not root:
        return weights, ()
    sidecars = tuple(w.path for w in weights if "/" in w.path)
    return root, sidecars


def content_digest(weights: list[HuggingFaceWeightFile]) -> str:
    """`sha256` over sorted `path:oid` lines, so the tree's order cannot change identity.

    The oids are already sha256 of the file bytes, which is what lets the plane
    address the content without pulling a single weight through it.
    """
    lines = sorted(f"{w.path}:{w.oid}" for w in weights)
    body = "\n".join(lines).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _is_weight(path: str) -> bool:
    return path.lower().endswith(WEIGHT_SUFFIXES)


def _format_of(weights: list[HuggingFaceWeightFile]) -> str | None:
    """First match in suffix order, not first file: a repo shipping both `.safetensors`
    and a legacy `.bin` of the same weights is a safetensors repo."""
    for suffix, name in _FORMAT_BY_SUFFIX.items():
        if any(w.path.lower().endswith(suffix) for w in weights):
            return name
    return None


def _licence_of(info: dict[str, Any]) -> str | None:
    card = info.get("cardData") or {}
    if isinstance(card, dict):
        licence = card.get("license")
        if isinstance(licence, str) and licence:
            return licence
    for tag in info.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("license:"):
            return tag.split(":", 1)[1] or None
    return None


def _next_link(header: str | None) -> str | None:
    """Hub pagination is an RFC 5988 `Link` header, not a cursor in the body."""
    if not header:
        return None
    for part in header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip()
        if not url.startswith("<") or not url.endswith(">"):
            continue
        if any(seg.strip().replace('"', "").replace(" ", "") == "rel=next" for seg in segments[1:]):
            return url[1:-1]
    return None
