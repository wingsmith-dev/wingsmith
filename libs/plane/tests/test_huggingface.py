"""The HuggingFace client, with every response mocked.

No test here touches the network: the digest is the artifact's identity, so the
rule it has to obey — same files, same digest, regardless of what the Hub happened
to return first — is a property of our code and must be provable offline.
"""

import json
import hashlib

import httpx
import pytest

from app.integrations.huggingface import HuggingFaceGated
from app.integrations.huggingface import HuggingFaceClient
from app.integrations.huggingface import HuggingFaceNotFound
from app.integrations.huggingface import HuggingFaceUnavailable


SHA = "b5c1a2d3e4f50617283940a1b2c3d4e5f6071829"

CONFIG = {"architectures": ["GptOssForCausalLM"], "hidden_size": 2880}

INFO = {
    "id": "openai/gpt-oss-20b",
    "sha": SHA,
    "gated": False,
    "safetensors": {"total": 20914757632},
    "cardData": {"license": "apache-2.0"},
    "tags": ["transformers", "license:apache-2.0"],
}


def _lfs(path: str, oid: str, size: int) -> dict:
    return {"type": "file", "path": path, "size": size, "lfs": {"oid": oid, "size": size}}


def _plain(path: str, size: int) -> dict:
    return {"type": "file", "path": path, "size": size}


TREE = [
    _plain("config.json", 1421),
    _plain("tokenizer.json", 27868174),
    _plain("README.md", 8123),
    _lfs("model-00000-of-00002.safetensors", "a" * 64, 4504575256),
    _lfs("model-00001-of-00002.safetensors", "b" * 64, 4504575256),
]


def _transport(info=None, tree=None, config=None, status=200, tree_pages=None):
    info = INFO if info is None else info
    tree = TREE if tree is None else tree
    config = CONFIG if config is None else config

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": "nope"})
        path = request.url.path
        if path.endswith("/config.json"):
            return httpx.Response(200, json=config)
        if "/tree/" in path:
            if tree_pages is not None:
                page = int(request.url.params.get("p", 0))
                headers = {}
                if page + 1 < len(tree_pages):
                    nxt = str(request.url.copy_set_param("p", page + 1))
                    headers["Link"] = f'<{nxt}>; rel="next"'
                return httpx.Response(200, json=tree_pages[page], headers=headers)
            return httpx.Response(200, json=tree)
        return httpx.Response(200, json=info)

    return httpx.MockTransport(handler)


def _client(**kwargs) -> HuggingFaceClient:
    return HuggingFaceClient(token="", transport=_transport(**kwargs))


async def test_inspect_resolves_a_stable_digest():
    model = await _client().inspect("openai/gpt-oss-20b")

    expected = (
        "sha256:"
        + hashlib.sha256(
            "\n".join(
                [
                    f"model-00000-of-00002.safetensors:{'a' * 64}",
                    f"model-00001-of-00002.safetensors:{'b' * 64}",
                ]
            ).encode()
        ).hexdigest()
    )

    assert model.digest == expected
    assert model.content_addressable is True
    assert model.revision == SHA
    assert model.size_bytes == 4504575256 * 2
    assert model.parameter_count == 20914757632
    assert model.format == "safetensors"
    assert model.licence == "apache-2.0"
    assert model.gated is False
    assert model.config == CONFIG
    assert [f.path for f in model.weight_files] == [
        "model-00000-of-00002.safetensors",
        "model-00001-of-00002.safetensors",
    ]


async def test_digest_ignores_tree_order():
    forward = await _client().inspect("openai/gpt-oss-20b")
    reversed_tree = await _client(tree=list(reversed(TREE))).inspect("openai/gpt-oss-20b")

    assert forward.digest == reversed_tree.digest


async def test_digest_ignores_config_and_tokenizer_files():
    stripped = [entry for entry in TREE if entry["path"].endswith(".safetensors")]
    noisy = stripped + [
        _plain("config.json", 99999),
        _plain("tokenizer_config.json", 42),
        _plain("README.md", 1),
        _lfs("tokenizer.model", "c" * 64, 500),
    ]

    assert (await _client(tree=stripped).inspect("m/x")).digest == (await _client(tree=noisy).inspect("m/x")).digest


async def test_a_non_lfs_weight_file_is_not_content_addressable():
    tree = TREE + [_plain("pytorch_model.bin", 12345)]
    model = await _client(tree=tree).inspect("openai/gpt-oss-20b")

    assert model.content_addressable is False
    assert model.digest == f"hf:openai/gpt-oss-20b@{SHA}"
    assert model.unaddressable_files == ("pytorch_model.bin",)
    assert model.size_bytes == 4504575256 * 2 + 12345


async def test_pagination_follows_the_link_header():
    pages = [TREE[:3], TREE[3:]]
    model = await _client(tree_pages=pages).inspect("openai/gpt-oss-20b")

    assert len(model.weight_files) == 2
    assert model.content_addressable is True


async def test_licence_falls_back_to_a_tag():
    info = {**INFO, "cardData": {}}
    assert (await _client(info=info).inspect("m/x")).licence == "apache-2.0"


async def test_gated_flag_is_normalized():
    info = {**INFO, "gated": "auto"}
    assert (await _client(info=info).inspect("m/x")).gated is True


async def test_gguf_repo_without_config_still_resolves():
    tree = [_lfs("model-q4.gguf", "d" * 64, 900)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/config.json"):
            return httpx.Response(404, json={})
        if "/tree/" in request.url.path:
            return httpx.Response(200, json=tree)
        return httpx.Response(200, json=INFO)

    model = await HuggingFaceClient(token="", transport=httpx.MockTransport(handler)).inspect("m/x")

    assert model.format == "gguf"
    assert model.config == {}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, HuggingFaceNotFound),
        (401, HuggingFaceGated),
        (403, HuggingFaceGated),
        (500, HuggingFaceUnavailable),
        (503, HuggingFaceUnavailable),
    ],
)
async def test_status_codes_map_to_typed_exceptions(status: int, expected: type[Exception]):
    with pytest.raises(expected):
        await _client(status=status).inspect("openai/gpt-oss-20b")


async def test_transport_failure_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    with pytest.raises(HuggingFaceUnavailable):
        await HuggingFaceClient(token="", transport=httpx.MockTransport(handler)).inspect("m/x")


async def test_token_is_sent_as_a_bearer_header():
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        if request.url.path.endswith("/config.json"):
            return httpx.Response(200, content=json.dumps(CONFIG))
        if "/tree/" in request.url.path:
            return httpx.Response(200, json=TREE)
        return httpx.Response(200, json=INFO)

    await HuggingFaceClient(token="hf_secret", transport=httpx.MockTransport(handler)).inspect("m/x")

    assert seen and all(value == "Bearer hf_secret" for value in seen)


async def test_sidecar_copies_count_towards_neither_size_nor_digest():
    """`openai/gpt-oss-20b` ships its served shards at the root and two more full copies
    of the same parameters under `original/` and `metal/`. Counting them trebles the size
    and, worse, lets a re-upload of the Metal build change the identity of weights that
    did not change."""
    served = await _client().inspect("m/x")

    with_sidecars = await _client(
        tree=[
            *TREE,
            _lfs("original/model.safetensors", "c" * 64, 13763575256),
            _lfs("metal/model.bin", "d" * 64, 13753575256),
        ]
    ).inspect("m/x")

    assert with_sidecars.digest == served.digest
    assert with_sidecars.size_bytes == served.size_bytes
    assert with_sidecars.sidecar_files == ("metal/model.bin", "original/model.safetensors")
    assert with_sidecars.format == "safetensors"


async def test_weights_only_in_a_subdirectory_are_still_the_weights():
    """A GGUF conversion keeps its only weights in a subdirectory, so there is no
    root-level file to prefer and nothing may be dropped."""
    model = await _client(
        tree=[
            _plain("README.md", 8123),
            _lfs("gguf/model-q4.gguf", "e" * 64, 4004575256),
        ]
    ).inspect("m/x")

    assert model.sidecar_files == ()
    assert model.size_bytes == 4004575256
    assert model.content_addressable is True
    assert model.format == "gguf"
