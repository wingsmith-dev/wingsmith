"""Video input with real clips: whether the frames reach the model, and what they cost.

`multimodal.video` sends four zero bytes and asks only whether a video part is routed.
Once an endpoint does route video that stops being the question. What the standard
asks next needs a clip that decodes: whether the model saw it, whether a video beside
an image is read in the order it was sent, what the clip costs in prompt tokens and
how that cost moves with length, frame rate and size, what the endpoint does past its
limits, and whether all of it streams.

The clips are committed under `hgrconform/assets/video/` beside the `make.sh` that produced them,
rather than built here the way `multimodal.image` builds its PNG. An H.264 encoder is
not something to write by hand, and a recipe in a diff is the next best thing to a
clip built at run time. Every clip is one red square moving left to right across
black, varied along one axis at a time around a 320x240, 4 fps, 4 second base, so two
clips that cost a different number of tokens differ for one reason.

Prompt token counts are compared directly here, as in `tokens.prompt_accounting` and
for the same reason: the bytes are fixed, and two endpoints serving one model must turn
one clip into the same number of tokens. A difference is the defect, and needs no
threshold to interpret. Everything generated stays out of `compare` except the two
collapsed readings of the answer — was the colour named, which way was the motion.
"""

from __future__ import annotations

import re
import struct

from base64 import b64encode
from typing import Any
from pathlib import Path
from importlib import resources
from itertools import pairwise
from dataclasses import dataclass

from hgrconform.session import Session
from hgrconform.session import Response
from hgrconform.session import dig
from hgrconform.probes.registry import probe
from hgrconform.probes.multimodal import COLOUR_SYNONYMS
from hgrconform.probes.multimodal import _png
from hgrconform.probes.multimodal import _error_shape
from hgrconform.probes.multimodal import _refusal_kind


ASSETS = Path(resources.files("hgrconform") / "assets" / "video")
CHAT = "/v1/chat/completions"

ANSWER_TOKENS = 4096
BLUE = (20, 40, 220)
IMAGE_SIZE = 64

UNDERSTAND_PROMPT = (
    "What colour is the square in this video, and which way does it move? "
    "Answer with exactly: <colour>, <left-to-right or right-to-left>"
)
ORDER_PROMPT = (
    "Two media items follow, an image and a video. Name the main colour shown in each, "
    "ignoring black, in the order the items appear. Answer with exactly two colour words "
    "separated by a comma."
)
COUNT_PROMPT = "Describe this video."


def _words(words: tuple[str, ...]) -> re.Pattern[str]:
    """Whole words only: a bare substring test finds `red` inside `coloured`."""
    return re.compile(r"\b(?:" + "|".join(words) + r")\b")


RED = _words(COLOUR_SYNONYMS)
BLUE_WORDS = _words(("blue", "navy", "azure", "cobalt", "sapphire", "indigo"))
LEFT_TO_RIGHT = re.compile(r"\bleft[\s-]+to[\s-]+(?:the[\s-]+)?right\b|\brightwards?\b|\bto(?:wards?)? the right\b")
RIGHT_TO_LEFT = re.compile(r"\bright[\s-]+to[\s-]+(?:the[\s-]+)?left\b|\bleftwards?\b|\bto(?:wards?)? the left\b")


@dataclass(frozen=True)
class Clip:
    """One committed clip, named by the four values `make.sh` varies."""

    width: int
    height: int
    fps: int
    seconds: int

    @property
    def name(self) -> str:
        return f"motion-{self.width}x{self.height}-{self.fps}fps-{self.seconds}s"

    @property
    def frames(self) -> int:
        return self.fps * self.seconds

    def read(self) -> bytes:
        return (ASSETS / f"{self.name}.mp4").read_bytes()


BASE = Clip(320, 240, 4, 4)
DURATIONS = tuple(Clip(320, 240, 4, seconds) for seconds in (1, 2, 4, 8, 16))
RATES = tuple(Clip(320, 240, fps, 4) for fps in (1, 2, 4, 8, 16, 30))
SIZES = tuple(
    Clip(width, height, 4, 4) for width, height in ((160, 120), (320, 240), (640, 480), (1280, 720), (1920, 1080))
)
LONG = Clip(160, 120, 1, 1800)

# Whole MiB of clip, before base64. The second step is the one that matters: base64
# takes it past 32 MiB of request body, a common gateway ceiling, so an endpoint that
# accepts it and one that refuses it disagree about a limit a caller will actually hit.
PADDED_MIB = (8, 25)


def _video(data: bytes) -> dict[str, Any]:
    return {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + b64encode(data).decode()}}


def _image() -> dict[str, Any]:
    url = "data:image/png;base64," + b64encode(_png(IMAGE_SIZE, IMAGE_SIZE, BLUE)).decode()
    return {"type": "image_url", "image_url": {"url": url}}


def _ask(text: str, *parts: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"role": "user", "content": [{"type": "text", "text": text}, *parts]}]


def _count(session: Session, messages: list[dict[str, Any]]) -> Response:
    """A request whose only purpose is `usage.prompt_tokens`, so it generates one token."""
    return session.post(CHAT, session.body(messages=messages, max_tokens=1, stream=False))


def _answer(response: Response) -> str:
    content = response.at("choices.0.message.content")
    return content if isinstance(content, str) else ""


def _message(response: Response) -> str | None:
    """The error text, as evidence only: two endpoints never word a refusal the same way."""
    if response.ok:
        return None
    message = dig(response.body, "error.message")
    return (message if isinstance(message, str) else response.text)[:300]


def _direction(text: str) -> str:
    """Which way the answer says the square moved, as one of four words."""
    forward = bool(LEFT_TO_RIGHT.search(text.lower()))
    backward = bool(RIGHT_TO_LEFT.search(text.lower()))
    if forward and backward:
        return "both"
    if forward:
        return "left-to-right"
    if backward:
        return "right-to-left"
    return "neither"


def _read_order(text: str, first: re.Pattern[str], second: re.Pattern[str]) -> str:
    """Whether the colours were named in the order the items were sent.

    `one` and `none` are kept apart from `reversed` on purpose. An endpoint that drops
    one of two media items reads as `one`, which is a different defect from one that
    delivers both in the wrong order.
    """
    here, there = first.search(text.lower()), second.search(text.lower())
    if here and there:
        return "source" if here.start() < there.start() else "reversed"
    if here or there:
        return "one"
    return "none"


def _shape(counts: list[int | None]) -> str:
    """How a token count moves along one axis, in words the diff can compare.

    Two endpoints can disagree on every count and still sample the same way — a
    constant per-clip overhead does that — and they can agree on the first three
    counts of an axis and part ways where one of them hits a cap. The run of signs
    separates those: `grows-then-flat` on one side and `grows` on the other is a
    frame or resolution ceiling that only one of them has.
    """
    if any(count is None for count in counts):
        return "incomplete"
    words: list[str] = []
    for before, after in pairwise(counts):
        word = "grows" if after > before else "falls" if after < before else "flat"
        if not words or words[-1] != word:
            words.append(word)
    return "-then-".join(words) or "flat"


def _padded(data: bytes, mib: int) -> bytes:
    """A clip grown to exactly `mib` MiB by a trailing ISO BMFF `free` box.

    Decoders skip a `free` box by definition, so the video the model would see is the
    base clip unchanged and the only thing varied is the size of the request. That
    keeps a byte limit apart from a decode limit, and it keeps a 25 MiB file out of
    the repository.
    """
    filler = mib * 1024 * 1024 - len(data)
    return data + struct.pack(">I", filler) + b"free" + bytes(filler - 8)


@probe(
    id="video.understanding",
    title="A real clip is accepted, and the answer names its colour and direction of motion",
    requirement="V. Multimodal Capability Support 3 — video input, and correct video understanding",
    compare=(
        "status",
        "has_content",
        "colour_recognised",
        "direction",
        "finish_reason",
        "refusal_kind",
        "error_shape",
        "error_type",
    ),
    requires=("video",),
)
def understanding(session: Session) -> dict[str, Any]:
    """Acceptance and understanding of one clip, kept apart the way `multimodal.image` keeps them.

    Colour proves a frame reached the model. Direction proves more than one did, and in
    order: a single frame, or frames shuffled by a sampler, cannot answer it.
    """
    data = BASE.read()
    response = session.post(
        CHAT, session.body(messages=_ask(UNDERSTAND_PROMPT, _video(data)), max_tokens=ANSWER_TOKENS)
    )
    text = _answer(response)
    return {
        "status": response.status,
        "has_content": bool(text.strip()),
        "colour_recognised": bool(RED.search(text.lower())),
        "direction": _direction(text),
        "finish_reason": response.at("choices.0.finish_reason"),
        "answer": text[:200],
        "clip": BASE.name,
        "clip_bytes": len(data),
        "prompt_tokens": response.at("usage.prompt_tokens"),
        "completion_tokens": response.at("usage.completion_tokens"),
        "message": _message(response),
        "refusal_kind": _refusal_kind(response),
        **_error_shape(response),
    }


@probe(
    id="video.order",
    title="An image and a video in one prompt, in both orders, and whether each is read where it was sent",
    requirement="V. Multimodal Capability Support 1, 3 — image and video input in one request",
    compare=("statuses", "read_order", "refusal_kinds", "prompt_tokens"),
    requires=("image", "video"),
)
def order(session: Session) -> dict[str, Any]:
    """The same two items sent twice, swapped, with the colours as the readback.

    The image is solid blue and the square in the video is red, so the order of the
    two colour words in the answer is the order in which the model received the items.
    Prompt tokens are compared per order as well as the reading: an endpoint that
    lays the items out in a different place in the prompt than the provider does can
    still name both colours correctly, and only the count shows it.
    """
    video = _video(BASE.read())
    layouts = {
        "image_first": ((_image(), video), BLUE_WORDS, RED),
        "video_first": ((video, _image()), RED, BLUE_WORDS),
    }
    statuses: dict[str, int] = {}
    read: dict[str, str] = {}
    refusals: dict[str, str] = {}
    tokens: dict[str, Any] = {}
    answers: dict[str, str] = {}
    messages: dict[str, str | None] = {}
    for name, (parts, first, second) in layouts.items():
        response = session.post(CHAT, session.body(messages=_ask(ORDER_PROMPT, *parts), max_tokens=ANSWER_TOKENS))
        text = _answer(response)
        statuses[name] = response.status
        read[name] = _read_order(text, first, second)
        refusals[name] = _refusal_kind(response)
        tokens[name] = response.at("usage.prompt_tokens")
        answers[name] = text[:200]
        messages[name] = _message(response)
    return {
        "statuses": statuses,
        "read_order": read,
        "refusal_kinds": refusals,
        "prompt_tokens": tokens,
        "answers": answers,
        "messages": messages,
    }


@probe(
    id="video.tokens",
    title="Prompt tokens for one clip, beside the same text with no clip",
    requirement="II. Response Correctness 2 — prompt token accounting; V. Multimodal Capability Support 3",
    compare=("statuses", "prompt_tokens", "video_tokens"),
    requires=("video",),
)
def tokens(session: Session) -> dict[str, Any]:
    """What the base clip costs, as the difference between two otherwise identical prompts.

    The delta is the clip's own share — its pads, its per-chunk timestamps and media
    markers — with the chat template's overhead cancelled out, which is what makes it
    readable against a geometry prediction.
    """
    text = _count(session, _ask(COUNT_PROMPT))
    video = _count(session, _ask(COUNT_PROMPT, _video(BASE.read())))
    counts = {"text": text.at("usage.prompt_tokens"), "video": video.at("usage.prompt_tokens")}
    delta = counts["video"] - counts["text"] if all(isinstance(v, int) for v in counts.values()) else None
    return {
        "statuses": {"text": text.status, "video": video.status},
        "prompt_tokens": counts,
        "video_tokens": delta,
        "clip": BASE.name,
        "clip_frames": BASE.frames,
        "message": _message(video),
    }


@probe(
    id="video.sampling",
    title="How a clip's prompt tokens move with its duration, frame rate and resolution",
    requirement="V. Multimodal Capability Support 3 — video input; frame sampling and limits",
    compare=("statuses", "duration_tokens", "rate_tokens", "size_tokens", "shape"),
    requires=("video",),
)
def sampling(session: Session) -> dict[str, Any]:
    """Three sweeps through the base clip, one axis each, as tokens over a text baseline.

    Frame sampling is not observable through the API, but its cost is. Doubling the
    duration at a fixed rate doubles the frames; raising the rate past what the
    endpoint samples at stops adding any; raising the resolution past its per-frame
    budget stops adding pixels. Each of those is a shape along one axis, which is why
    `shape` is compared beside the counts.

    The base clip sits on all three axes and is sent once.
    """
    baseline = _count(session, _ask(COUNT_PROMPT))
    base_tokens = baseline.at("usage.prompt_tokens")
    measured: dict[Clip, Response] = {}
    sizes: dict[str, int] = {}
    for clip in dict.fromkeys((*DURATIONS, *RATES, *SIZES)):
        data = clip.read()
        sizes[clip.name] = len(data)
        measured[clip] = _count(session, _ask(COUNT_PROMPT, _video(data)))

    def cost(clip: Clip) -> int | None:
        total = measured[clip].at("usage.prompt_tokens")
        return total - base_tokens if isinstance(total, int) and isinstance(base_tokens, int) else None

    axes = {
        "duration": {f"{clip.seconds}s": cost(clip) for clip in DURATIONS},
        "rate": {f"{clip.fps}fps": cost(clip) for clip in RATES},
        "size": {f"{clip.width}x{clip.height}": cost(clip) for clip in SIZES},
    }
    return {
        "statuses": {"text": baseline.status} | {clip.name: response.status for clip, response in measured.items()},
        "duration_tokens": axes["duration"],
        "rate_tokens": axes["rate"],
        "size_tokens": axes["size"],
        "shape": {axis: _shape(list(values.values())) for axis, values in axes.items()},
        "baseline_prompt_tokens": base_tokens,
        "clip_frames": {clip.name: clip.frames for clip in measured},
        "clip_bytes": sizes,
        "messages": {clip.name: _message(response) for clip, response in measured.items() if not response.ok},
    }


@probe(
    id="video.two_videos",
    title="Two videos in one prompt: accepted, or refused and how",
    requirement="V. Multimodal Capability Support 3 — video input; limits on media items per request",
    compare=("status", "refusal_kind", "error_shape", "error_type", "prompt_tokens"),
    requires=("video",),
)
def two_videos(session: Session) -> dict[str, Any]:
    """The base clip twice. A per-prompt video limit is a deployment setting, so this is where it shows."""
    video = _video(BASE.read())
    response = _count(session, _ask(COUNT_PROMPT, video, video))
    return {
        "status": response.status,
        "prompt_tokens": response.at("usage.prompt_tokens"),
        "message": _message(response),
        "refusal_kind": _refusal_kind(response),
        **_error_shape(response),
    }


@probe(
    id="video.long_clip",
    title="A thirty-minute clip: accepted, refused, or silently capped",
    requirement="V. Multimodal Capability Support 3 — video input; limits on duration",
    compare=("status", "refusal_kind", "error_shape", "error_type", "prompt_tokens"),
    requires=("video",),
)
def long_clip(session: Session) -> dict[str, Any]:
    """1800 frames at 1 fps and 160x120, small on the wire and long in time.

    An endpoint with a frame cap accepts this and reports fewer prompt tokens than the
    duration would imply; one with a duration limit refuses it. The count is compared
    because a cap only shows up as a number.
    """
    data = LONG.read()
    response = _count(session, _ask(COUNT_PROMPT, _video(data)))
    return {
        "status": response.status,
        "prompt_tokens": response.at("usage.prompt_tokens"),
        "clip": LONG.name,
        "clip_frames": LONG.frames,
        "clip_bytes": len(data),
        "message": _message(response),
        "refusal_kind": _refusal_kind(response),
        **_error_shape(response),
    }


@probe(
    id="video.payload_size",
    title="The base clip padded to a large payload: where the request body is refused",
    requirement="V. Multimodal Capability Support 3 — video input; limits on size",
    compare=("statuses", "refusal_kinds", "error_shapes", "error_types"),
    requires=("video",),
)
def payload_size(session: Session) -> dict[str, Any]:
    """Two steps up in size, stopping at the first refusal.

    This observes the shape of a size limit and never searches for its value, for the
    same reason `limits.burst` does not search for a rate ceiling. A step that was
    not sent is absent on both sides only if both refused at the same step.
    """
    data = BASE.read()
    statuses: dict[str, int] = {}
    refusals: dict[str, str] = {}
    shapes: dict[str, Any] = {}
    types: dict[str, Any] = {}
    messages: dict[str, str | None] = {}
    body_mib: dict[str, float] = {}
    for mib in PADDED_MIB:
        step = f"{mib}MiB"
        padded = _padded(data, mib)
        body_mib[step] = round(len(padded) * 4 / 3 / (1024 * 1024), 1)
        response = _count(session, _ask(COUNT_PROMPT, _video(padded)))
        shape = _error_shape(response)
        statuses[step] = response.status
        refusals[step] = _refusal_kind(response)
        shapes[step] = shape["error_shape"]
        types[step] = shape["error_type"]
        messages[step] = _message(response)
        if not response.ok:
            break
    return {
        "statuses": statuses,
        "refusal_kinds": refusals,
        "error_shapes": shapes,
        "error_types": types,
        "base64_mib": body_mib,
        "messages": messages,
    }


def _usage_position(carrying: list[int], total: int) -> str:
    if not carrying:
        return "absent"
    if len(carrying) == total:
        return "every-chunk"
    if carrying == [total - 1]:
        return "final-chunk"
    return "some-chunks"


@probe(
    id="video.streaming",
    title="A video request streamed: whether it streams, and whether the final usage is there",
    requirement="I. Streaming Output 1, 3; V. Multimodal Capability Support 3 — video input over a stream",
    compare=(
        "status",
        "content_type",
        "terminated",
        "incremental",
        "has_content",
        "usage_position",
        "usage_prompt_tokens",
    ),
    requires=("video",),
)
def streaming(session: Session) -> dict[str, Any]:
    """`multimodal` and `streaming` each pass on their own; this is the two at once.

    A video request spends seconds in preprocessing before the first token, which is
    exactly where a proxy's idle timeout or a frontend that buffers until the encoder
    returns turns a stream into one late chunk. `incremental` asks whether more than
    one chunk carried generated text — reasoning or content — so a stream that arrived
    all at once is distinguishable from one that streamed.
    """
    body = session.body(
        messages=_ask(UNDERSTAND_PROMPT, _video(BASE.read())),
        max_tokens=ANSWER_TOKENS,
        stream_options={"include_usage": True},
    )
    stream = session.stream(body)
    reasoning_path = session.profile.paths.reasoning_delta
    generating = [
        index
        for index, chunk in enumerate(stream.chunks)
        if dig(chunk, "choices.0.delta.content") or dig(chunk, reasoning_path)
    ]
    usage = next((dig(chunk, "usage") for chunk in reversed(stream.chunks) if dig(chunk, "usage")), None)
    text = stream.text()
    return {
        "status": stream.status,
        "content_type": stream.headers.get("content-type", "").split(";")[0],
        "terminated": stream.done,
        "incremental": len(generating) > 1,
        "has_content": bool(text.strip()),
        "usage_position": _usage_position(stream.chunks_with("usage.prompt_tokens"), len(stream.chunks)),
        "usage_prompt_tokens": dig(usage, "prompt_tokens"),
        "chunk_count": len(stream.chunks),
        "colour_recognised": bool(RED.search(text.lower())),
        "direction": _direction(text),
        "answer": text[:200],
        "error": stream.error or (stream.raw_lines[0][:300] if not stream.ok and stream.raw_lines else ""),
    }
