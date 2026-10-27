"""The Dynamo recipe corpus, and the engine arguments it is allowed to default.

The bug these exist for: `zai-org/GLM-5.3-Flash` at 1,048,576 context is a hybrid
model, every decode sequence holds one Mamba state block, and at that context only
512 blocks fit. vLLM's default `max_num_seqs` is 1024, so the worker raises a
`ValueError` at CUDA graph capture and crash-loops: a hard refusal, not an
out-of-memory, and a property of the model, not of our cluster. NVIDIA
publishes the answer in `ai-dynamo/dynamo` `recipes/glm-5.3-flash/vllm/`, against
the exact program the operator starts, and the plane used to read none of it.

Two corpora and they are not interchangeable. `vllm_recipes` describes
`vllm serve`; this one describes `python3 -m dynamo.vllm`. For this very model the
vLLM recipe writes 512 and the Dynamo aggregated H200 recipe writes 32, and the
32 is the one measured on the machine the workload runs on.
"""

import json
import datetime as dt

import pytest

from app.core.ids import ARTIFACT
from app.core.ids import WORKLOAD
from app.core.ids import new_uid
from app.db.models import Artifact
from app.db.models import Workload
from app.core.serving import DERIVED_FLAGS
from app.integrations import dynamo_recipes
from app.ingestion.desired import _serving
from app.modules.workloads.services import serving_view
from app.modules.workloads.services import recipe_engine_args
from app.modules.workloads.services import resolved_engine_args
from app.integrations.dynamo_recipes import AGGREGATED
from app.integrations.dynamo_recipes import ADOPTED_FLAGS
from app.integrations.dynamo_recipes import DECLINED_FLAGS


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

GLM = "zai-org/GLM-5.3-Flash"

H200 = "h200-141gb-sxm"
GB200 = "gb200-186gb-nvl"
B200 = "b200-180gb-sxm"

ONE_MILLION = 1_048_576

MAMBA_CACHE_BLOCKS = 512
"""The ceiling the engine itself names when it refuses to capture graphs.

    max_num_seqs (1024) exceeds available Mamba cache blocks (512)

Pinned as the bar instead of the published 32, because the published number is
allowed to move and the failure is not: anything above this is a crash loop.
"""


@pytest.fixture(scope="module")
def corpus():
    return dynamo_recipes.corpus()


def a_workload(**kwargs) -> Workload:
    base = {
        "uid": new_uid(WORKLOAD),
        "name": "glm-53-flash",
        "engine": "vllm",
        "engine_runtime_version": "1.4.1",
        "engine_runtime_image": None,
        "frontend_image": None,
        "engine_args": {},
        "imported_args": {},
        "quantization": None,
        "dtype": None,
        "tensor_parallel": 4,
        "pipeline_parallel": 1,
        "expert_parallel": 1,
        "context_length": ONE_MILLION,
        "max_batch_tokens": None,
        "kv_cache_dtype": None,
        "kv_cache_fraction": None,
        "prefix_cache_enabled": True,
        "prefix_cache_config": {},
        "device_class": H200,
        "device_count": 4,
        "device_memory_mib_min": 0,
        "requires_single_topology_domain": True,
        "topology_domain_kind": None,
        "cpu_cores": None,
        "memory_mib": None,
        "disk_gib": None,
        "labels": {},
        "overlay": {},
        "serving": {},
        "capabilities": {},
        "serving_digest": "svg_" + "0" * 64,
        "created_at": EPOCH,
        "updated_at": None,
    }
    return Workload(**(base | kwargs))


def an_artifact(slug: str = GLM) -> Artifact:
    return Artifact(uid=new_uid(ARTIFACT), slug=slug)


# --------------------------------------------------------------------------- the regression


def test_glm_53_flash_at_1m_tokens_gets_an_accepted_max_num_seqs():
    """The whole reason this exists: a cluster crash-looped at CUDA graph capture.

    The engine refuses when `max_num_seqs` exceeds the Mamba cache blocks that fit,
    which at 1 M context is 512. Its default is 1024, the plane sent nothing, and
    the decode worker never bound a port.
    """
    resolved = resolved_engine_args(a_workload(), an_artifact())
    assert "max-num-seqs" in resolved
    assert int(resolved["max-num-seqs"]) <= MAMBA_CACHE_BLOCKS
    assert resolved["max-num-seqs"] == "32"


def test_the_number_reaches_the_cluster_and_not_only_the_console():
    """The wire and the read are one function, so they cannot answer differently.

    A resolved view that showed a flag the deployment frame did not carry is the
    failure this plane has already had once, with `--reasoning-parser`.
    """
    workload, artifact = a_workload(), an_artifact()
    assert _serving(workload, artifact).engine_args == serving_view(workload, artifact).resolved_engine_args


# --------------------------------------------------------------------------- precedence


def test_an_explicit_engine_arg_beats_the_recipe():
    """An escape hatch that loses to a default is not one.

    Both spellings, because `engine_args` is keyed as a person types it and the
    operator trims the dashes before it renders.
    """
    for key in ("max-num-seqs", "--max-num-seqs"):
        resolved = resolved_engine_args(a_workload(engine_args={key: "8"}), an_artifact())
        assert resolved["max-num-seqs"] == "8"


def test_a_recipe_may_not_name_a_flag_the_plane_derives():
    """A recipe's context length or parallel size must never overrule a placed shape.

    `engine_args` is laid over the operator's derived flags last, so anything the
    plane puts there wins on the command line. A workload's own columns were
    validated by `check_shape` and `check_capacity` and were what placement chose
    devices against; a suggestion that silently replaced one of them would leave
    the running process disagreeing with the fit that admitted it.
    """
    assert not set(ADOPTED_FLAGS) & DERIVED_FLAGS
    assert not set(recipe_engine_args(a_workload(), an_artifact())) & DERIVED_FLAGS


def test_a_declined_flag_is_declined_with_a_reason():
    """The list of what is not carried is data, so widening it means disagreeing in writing."""
    assert not set(DECLINED_FLAGS) & set(ADOPTED_FLAGS)
    assert all(reason.strip() for reason in DECLINED_FLAGS.values())


# --------------------------------------------------------------------------- absence


def test_a_model_the_corpus_does_not_cover_is_left_exactly_as_it_was():
    """Coverage is a couple of dozen models, so a miss is the ordinary case."""
    workload = a_workload(engine_args={"--dtype": "bfloat16"})
    assert recipe_engine_args(workload, an_artifact("meta-llama/llama-3.1-8b-instruct")) == {}
    assert resolved_engine_args(workload, an_artifact("meta-llama/llama-3.1-8b-instruct")) == {
        "dtype": "bfloat16",
    }


def test_an_artifact_with_no_slug_asks_the_corpus_nothing():
    assert recipe_engine_args(a_workload(), Artifact(uid=new_uid(ARTIFACT), slug=None)) == {}


def test_an_engine_the_corpus_has_no_recipe_for_gets_no_defaults():
    """A TensorRT-LLM deployment must not inherit a vLLM worker's arguments."""
    assert recipe_engine_args(a_workload(engine="trtllm"), an_artifact()) == {}


def test_a_missing_or_broken_snapshot_is_not_fatal(tmp_path):
    """Registering and rendering a workload must not depend on a file being parseable."""
    assert len(dynamo_recipes.load(tmp_path / "absent.json")) == 0
    broken = tmp_path / "broken.json"
    broken.write_text("{oh no")
    assert len(dynamo_recipes.load(broken)) == 0
    assert dynamo_recipes.load(broken).lookup(GLM) is None


# --------------------------------------------------------------------------- reading the corpus


def test_the_card_the_workload_runs_on_picks_the_recipe(corpus):
    """GLM-5.3-Flash is 32 sequences on an H200 and 256 on a GB200 rack.

    One model, one context length, two published numbers, and the difference is the
    card. Answering with either one regardless would be right by coincidence half
    the time.
    """
    assert recipe_engine_args(a_workload(device_class=H200), an_artifact())["max-num-seqs"] == "32"
    assert recipe_engine_args(a_workload(device_class=GB200), an_artifact())["max-num-seqs"] == "256"


def test_a_card_with_no_recipe_of_its_own_gets_the_lowest_published_value():
    """The only direction it is safe to be wrong in.

    `max_num_seqs` is ceiling-shaped: under the true limit the engine starts and
    runs slower, over it the engine refuses. So a model with no recipe for this card
    takes the lowest number anybody published for it: GLM-5.3-Flash has no
    aggregated B200 recipe, and 32 is the floor of its H200 and GB200 ones.
    """
    assert recipe_engine_args(a_workload(device_class=B200), an_artifact())["max-num-seqs"] == "32"


def test_a_device_class_is_matched_on_the_card_and_not_the_whole_string():
    assert dynamo_recipes.hardware_key("h200-141gb-sxm") == "h200"
    assert dynamo_recipes.hardware_key("gb200-186gb-nvl") == "gb200"
    assert dynamo_recipes.hardware_key("") == ""


def test_a_disaggregated_recipe_is_never_read(corpus):
    """The operator renders one worker, so a prefill worker's numbers are not ours.

    Kimi K3's disaggregated GB200 recipe runs prefill at four concurrent sequences
    and decode at 256. Neither is the answer for a single worker doing both, and
    four would throttle the model to nothing.
    """
    match = corpus.lookup("moonshotai/Kimi-K3")
    assert any(d.mode == "disagg" and d.args.get("max-num-seqs") == "4" for d in match.recipe.deployments)
    assert all(d.mode == AGGREGATED for d in match.aggregated("vllm"))
    assert match.engine_defaults("vllm", GB200)["max-num-seqs"] == "256"


def test_every_adopted_flag_is_one_the_corpus_passes_to_a_dynamo_worker(corpus):
    """The rule that licenses carrying an argument across at all.

    An argument is adoptable only where NVIDIA is observed handing that exact
    spelling to `python3 -m dynamo.vllm`. It is what separates this from lifting a
    flag out of `vllm serve`'s help text and hoping the other program takes it: the
    mistake that put `--tool-call-parser` on a worker that exits on unrecognised
    arguments.
    """
    seen = {flag for recipe in corpus.recipes() for d in recipe.deployments if d.backend == "vllm" for flag in d.args}
    assert set(ADOPTED_FLAGS) <= seen


def test_the_snapshot_records_which_commit_it_came_from(corpus):
    """A table that cannot say which corpus it came from cannot be diffed against the next."""
    assert corpus.source == "https://github.com/ai-dynamo/dynamo"
    assert len(corpus.ref) == 40
    assert len(corpus) > 0


def test_snapshot_holds_the_whole_argument_list(corpus):
    """Widening the allow-list has to be a code change, never a re-capture.

    Keeping the evidence means the decision about a flag is made where the reasons
    are written down, and a person can read what a recipe actually says without a
    network round trip.
    """
    match = corpus.lookup(GLM)
    worker = next(d for d in match.aggregated("vllm") if d.hardware == "h200")
    assert worker.args["max-model-len"] == str(ONE_MILLION)
    assert worker.args["tensor-parallel-size"] == "8"
    assert "attention-backend" in worker.args
    assert json.loads(worker.args["compilation-config"])["cudagraph_mode"] == "FULL_AND_PIECEWISE"
    assert match.source_uri(worker).endswith("recipes/glm-5.3-flash/vllm/agg-h200-agentic/deploy.yaml")
    assert corpus.ref in match.snapshot_uri(worker)
