"""Multimodal input: whether an image content part is accepted, and whether it is understood.

The image is built here rather than read from a fixture, so that the probe carries no
state a run could lose and no file two endpoints could be given different versions of.
A solid-colour PNG is enough: the question the standard asks is whether the endpoint
looked at the pixels, and a colour is the smallest answer that cannot be produced by
guessing from the prompt alone.

The audio and video probes ask a smaller question on purpose. Transcription quality is
not comparable between two endpoints without a corpus; what is comparable is whether the
content part is routed at all, and what the endpoint says when it declines. So a single
malformed part goes in, and the status and the error shape come out.
"""

from __future__ import annotations

import zlib
import struct

from base64 import b64encode
from typing import Any

from hgrconform.session import Session
from hgrconform.session import dig
from hgrconform.probes.registry import Skip
from hgrconform.probes.registry import probe


STUB = b64encode(b"\x00\x00\x00\x00").decode()
COLOUR = (220, 20, 20)
COLOUR_WORD = "red"

# A vision model that sees the image can still name the colour a different way, and
# two endpoints serving one model will disagree on the word often enough that a bare
# substring test would report a difference on a run where both of them saw red.
# Matching a family of words keeps the observation about whether the pixels arrived.
COLOUR_SYNONYMS = ("red", "crimson", "scarlet", "vermilion", "maroon", "ruby")
IMAGE_SIZE = 16
PROMPT = "What colour is this image? Answer with one word."


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _png(width: int, height: int, colour: tuple[int, int, int]) -> bytes:
    """A solid-colour truecolour PNG, written by hand.

    Pillow is not a dependency of this package and must not become one for a sixteen
    pixel square. The format needs three chunks and one filter byte per scanline.
    """
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(colour) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(row * height, 9))
        + _chunk(b"IEND", b"")
    )


def _data_url() -> str:
    return "data:image/png;base64," + b64encode(_png(IMAGE_SIZE, IMAGE_SIZE, COLOUR)).decode()


def _error_shape(response: Any) -> dict[str, Any]:
    """The parts of an error the diff can explain, without the message text itself.

    Two endpoints that both decline will word it differently, and the wording is not
    the finding. The type, the code, and whether a message was given at all are.
    """
    error = dig(response.body, "error")
    if not isinstance(error, dict):
        return {"error_shape": "none" if response.ok else "unstructured", "error_type": None, "error_code": None}
    return {
        "error_shape": "object",
        "error_type": error.get("type"),
        "error_code": error.get("code"),
    }


@probe(
    id="multimodal.image",
    title="An image content part is accepted, and the answer depends on the pixels",
    requirement="V. Multimodal Capability Support 1 — image input, and correct image understanding",
    compare=("status", "has_content", "colour_recognised", "finish_reason", "error_shape", "error_type"),
    requires=("image",),
)
def image(session: Session) -> dict[str, Any]:
    """Acceptance and understanding, kept apart.

    `status` answers whether the content-part shape was accepted; `colour_recognised`
    answers whether the pixels reached the model. The answer text itself stays out of
    `compare`, because two endpoints that both see red will say so in different words
    and a diff on the sentence would report every run as a difference. For the same
    reason the recognition test matches a family of words rather than one: the
    question is whether the image arrived, not which synonym the model reached for.
    """
    body = session.body(
        max_tokens=16,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": _data_url()}},
                ],
            }
        ],
    )
    response = session.post("/v1/chat/completions", body)
    answer = response.at("choices.0.message.content")
    text = answer if isinstance(answer, str) else ""
    return {
        "status": response.status,
        "has_content": bool(text.strip()),
        "colour_recognised": any(word in text.lower() for word in COLOUR_SYNONYMS),
        "finish_reason": response.at("choices.0.finish_reason"),
        "answer": text[:200],
        "expected_colour": COLOUR_WORD,
        "accepted_words": list(COLOUR_SYNONYMS),
        "image_bytes": len(_png(IMAGE_SIZE, IMAGE_SIZE, COLOUR)),
        **_error_shape(response),
    }


@probe(
    id="multimodal.audio",
    title="How an audio content part is routed, or declined",
    requirement="V. Multimodal Capability Support 2 — audio input",
    compare=("status", "has_content", "error_shape", "error_type"),
    requires=("audio",),
)
def audio(session: Session) -> dict[str, Any]:
    """What the endpoint does with `input_audio`, not what it hears.

    The payload is deliberately not a real recording. An endpoint that has no audio
    route rejects the part before decoding it, and an endpoint that has one rejects the
    data instead — those are different statuses and different error types, which is the
    whole observation.
    """
    if not session.profile.supports("audio"):
        raise Skip("profile claims no audio modality")
    body = session.body(
        max_tokens=16,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is said in this audio? Answer with one word."},
                    {"type": "input_audio", "input_audio": {"data": STUB, "format": "wav"}},
                ],
            }
        ],
    )
    response = session.post("/v1/chat/completions", body)
    answer = response.at("choices.0.message.content")
    text = answer if isinstance(answer, str) else ""
    return {
        "status": response.status,
        "has_content": bool(text.strip()),
        "answer": text[:200],
        "response_excerpt": response.text[:400],
        **_error_shape(response),
    }


@probe(
    id="multimodal.video",
    title="How a video content part is routed, or declined",
    requirement="V. Multimodal Capability Support 3 — video input",
    compare=("status", "has_content", "error_shape", "error_type"),
    requires=("video",),
)
def video(session: Session) -> dict[str, Any]:
    """The same question as `multimodal.audio`, for the part name video endpoints use.

    There is no video content part in the OpenAI schema, so an endpoint that claims
    video has invented one. `video_url` is the spelling the compatible servers settled
    on; an endpoint that names it something else is a finding for the profile, not a
    branch in this probe.
    """
    if not session.profile.supports("video"):
        raise Skip("profile claims no video modality")
    body = session.body(
        max_tokens=16,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What happens in this video? Answer with one word."},
                    {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{STUB}"}},
                ],
            }
        ],
    )
    response = session.post("/v1/chat/completions", body)
    answer = response.at("choices.0.message.content")
    text = answer if isinstance(answer, str) else ""
    return {
        "status": response.status,
        "has_content": bool(text.strip()),
        "answer": text[:200],
        "response_excerpt": response.text[:400],
        **_error_shape(response),
    }
