"""Reading and promoting a hand-applied vLLM-Omni worker.

The spec is the Z-Image-Turbo worker applied by hand on cluster-site-a (namespace
imgtest) on 2026-09-23, whose promotion was refused because a diffusion model has no
`--max-model-len` to read.
"""

import pytest

from app.core.ids import ARTIFACT
from app.core.ids import new_uid
from app.db.models import Artifact
from app.core.exceptions import Invalid
from app.modules.discovered.derive import derive
from app.modules.workloads.services import check_serving_request
from app.modules.discovered.services import serving_from_draft


Z_IMAGE = {
    "image": "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.1",
    "command": ["python3", "-m", "dynamo.vllm.omni"],
    "args": [
        "--model",
        "Tongyi-MAI/Z-Image-Turbo",
        "--served-model-name",
        "z-image-turbo",
        "--output-modalities",
        "image",
        "--media-output-fs-url",
        "file:///tmp/dynamo_media",
    ],
    "requests": {"nvidia.com/gpu": "1"},
    "limits": {"nvidia.com/gpu": "1"},
}


def draft_of(spec: dict):
    return derive(spec=spec, labels={}, name="z-image-turbo-omniworker", devices_per_replica=1, device_class="h100")


def test_an_omni_worker_reads_as_omni_with_its_output_modality():
    draft = draft_of(Z_IMAGE)
    assert draft.engine == "omni"
    assert draft.output_modality == "image"
    assert draft.context_length is None
    assert draft.confident
    assert not any("max-model-len" in note for note in draft.notes)


def test_an_omni_worker_promotes_without_a_context_length():
    serving = serving_from_draft(draft_of(Z_IMAGE), new_uid(ARTIFACT))
    assert serving.engine == "omni"
    assert serving.context_length == 0
    assert serving.serving.output_modality == "image"
    check_serving_request(serving, Artifact(uid=new_uid(ARTIFACT), name="z-image-turbo", digest="hf:x"))


def test_an_omni_worker_without_an_output_modality_is_refused():
    spec = dict(Z_IMAGE, args=["--model", "Tongyi-MAI/Z-Image-Turbo"])
    with pytest.raises(Invalid) as refused:
        serving_from_draft(draft_of(spec), new_uid(ARTIFACT))
    assert "output_modality" in refused.value.detail["unread"]
    assert "context_length" not in refused.value.detail["unread"]


def test_a_chat_worker_still_needs_its_context_length():
    spec = dict(Z_IMAGE, command=["python3", "-m", "dynamo.vllm"], args=["--model", "Qwen/Qwen3-8B"])
    with pytest.raises(Invalid) as refused:
        serving_from_draft(draft_of(spec), new_uid(ARTIFACT))
    assert refused.value.detail["unread"] == ["context_length"]
