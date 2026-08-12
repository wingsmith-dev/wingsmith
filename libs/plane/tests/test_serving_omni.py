"""The omni engine on the serving block: output modality, the diffusion block, and the wire.

Plan 05 (`docs/plans/05-image-serving.md`). The output modality is a serving field, not a
capability, because it renders — `--output-modalities` — and because the routing table carries it.
"""

import pytest
import msgspec

from app.core.ids import ARTIFACT
from app.core.ids import new_uid
from app.db.models import Artifact
from app.core.serving import serving_digest
from app.core.exceptions import Invalid
from app.ingestion.desired import _serving
from app.modules.workloads.schemas import RoleSchema
from app.modules.workloads.schemas import ShapeSchema
from app.modules.workloads.schemas import ServingSchema
from app.modules.workloads.schemas import TopologySchema
from app.modules.workloads.schemas import DiffusionSchema
from app.modules.workloads.schemas import WorkloadServingRequest
from app.modules.workloads.services import check_output
from app.modules.workloads.services import check_topology
from app.modules.workloads.services import declared_roles
from app.modules.workloads.services import stored_serving
from app.modules.workloads.services import check_serving_request
from app.modules.workloads.services import resolve_engine_runtime_image

from tests.test_serving import a_workload


def an_omni_request(**kwargs) -> WorkloadServingRequest:
    fields = {
        "artifact": new_uid(ARTIFACT),
        "engine": "omni",
        "engine_runtime_version": "1.4.1",
        "context_length": 1024,
        "shape": ShapeSchema(device_class="h100", device_count=1),
        "serving": ServingSchema(output_modality="image"),
    }
    return WorkloadServingRequest(**(fields | kwargs))


def test_an_omni_request_is_accepted():
    artifact = Artifact(uid=new_uid(ARTIFACT), name="z-image-turbo", digest="sha256:beef")
    assert check_serving_request(an_omni_request(), artifact) is None


@pytest.mark.parametrize("modality", [None, "text"])
def test_omni_needs_an_image_or_video_output(modality):
    with pytest.raises(Invalid, match="output_modality image or video"):
        check_output("omni", ServingSchema(output_modality=modality))


@pytest.mark.parametrize("modality", ["image", "video"])
def test_a_chat_engine_cannot_claim_an_image_or_video_output(modality):
    with pytest.raises(Invalid, match="emits text"):
        check_output("vllm", ServingSchema(output_modality=modality))


def test_a_chat_engine_may_say_text_or_nothing():
    check_output("vllm", ServingSchema())
    check_output("sglang", ServingSchema(output_modality="text"))


def test_an_unknown_output_modality_is_refused():
    with pytest.raises(Invalid, match="unknown output_modality"):
        check_output("omni", ServingSchema(output_modality="audio"))


def test_a_diffusion_block_is_the_omni_engines_alone():
    with pytest.raises(Invalid, match="diffusion block"):
        check_output("vllm", ServingSchema(diffusion=DiffusionSchema(model_class_name="FluxPipeline")))
    check_output(
        "vllm-omni",
        ServingSchema(output_modality="video", diffusion=DiffusionSchema(model_class_name="LTX2Pipeline")),
    )


def test_omni_is_never_disaggregated():
    roles = declared_roles(TopologySchema(roles=[RoleSchema(role="prefill"), RoleSchema(role="decode")]))
    for engine in ("omni", "OMNI", "vllm_omni"):
        with pytest.raises(Invalid, match="no prefill/decode split"):
            check_topology("disaggregated", roles, engine)


def test_omni_borrows_the_vllm_runtime_repository():
    image = resolve_engine_runtime_image("omni", "1.4.1", None)
    assert image is not None and image.endswith("/vllm-runtime:1.4.1")
    assert resolve_engine_runtime_image("omni", "1.5.0", "ghcr.io/x/omni:1") == "ghcr.io/x/omni:1"


def test_a_chat_workloads_stored_block_and_digest_do_not_move():
    """The column is hashed whole. A new key on every write would move every chat digest."""
    stored = stored_serving(ServingSchema(output_modality="text"))
    assert "output_modality" not in stored
    assert "diffusion" not in stored
    assert stored == {k: v for k, v in msgspec.to_builtins(ServingSchema()).items() if k in stored}

    artifact = Artifact(uid=new_uid(ARTIFACT))
    legacy = {
        k: v for k, v in msgspec.to_builtins(ServingSchema()).items() if k not in ("output_modality", "diffusion")
    }
    assert serving_digest(a_workload(serving=legacy), artifact, ()) == serving_digest(
        a_workload(serving=stored_serving(ServingSchema())), artifact, ()
    )


def test_the_diffusion_block_is_configuration_and_moves_the_digest():
    artifact = Artifact(uid=new_uid(ARTIFACT))
    plain = stored_serving(ServingSchema(output_modality="image"))
    tuned = stored_serving(
        ServingSchema(
            output_modality="image",
            diffusion=DiffusionSchema(default_sampling_params={"num_inference_steps": 8}),
        )
    )
    assert serving_digest(a_workload(engine="omni", serving=plain), artifact, ()) != serving_digest(
        a_workload(engine="omni", serving=tuned), artifact, ()
    )


def test_the_wire_carries_the_modality_and_the_stage_configuration():
    stored = stored_serving(
        ServingSchema(
            output_modality="video",
            diffusion=DiffusionSchema(
                model_class_name="LTX2Pipeline",
                default_sampling_params={"num_inference_steps": 30, "guidance_scale": 4.0},
            ),
        )
    )
    wire = _serving(a_workload(engine="omni", serving=stored), Artifact(uid=new_uid(ARTIFACT), slug="lightricks/ltx-2"))
    assert wire.output_modality == "video"
    assert wire.diffusion is not None
    assert wire.diffusion.model_class_name == "LTX2Pipeline"
    assert wire.diffusion.default_sampling_params == {"num_inference_steps": 30, "guidance_scale": 4.0}


def test_a_chat_frame_carries_neither():
    wire = _serving(a_workload(), Artifact(uid=new_uid(ARTIFACT), slug="meta-llama/llama-3.1-8b-instruct"))
    assert wire.output_modality is None
    assert wire.diffusion is None
