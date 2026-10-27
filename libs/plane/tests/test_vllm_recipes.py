"""The vLLM recipe corpus and what the artifact draft does with it.

Two kinds of test here. The first read the committed snapshot and assert its
internal consistency — a corpus that names a strategy nothing defines, or indexes
a slug onto a recipe that is not there, is a broken refresh and should fail here
rather than as a `None` three layers up. The second cover the reading: a miss is
ordinary, a hit is attributed, and neither one may move the plane's own sizing.
"""

import json

import pytest

from app.integrations import vllm_recipes
from app.integrations.vllm_recipes import RecipeCorpus
from app.integrations.vllm_recipes import HardwareProfile
from app.modules.artifacts.resolve import resolved

from .test_artifacts import FakeHub
from .test_artifacts import service
from .test_artifacts import hf_model


@pytest.fixture
def corpus() -> RecipeCorpus:
    return vllm_recipes.corpus()


def test_the_snapshot_is_present_and_names_the_commit_it_came_from(corpus):
    """A corpus that cannot say which commit it is cannot be diffed against the next one."""
    assert len(corpus) > 100
    assert len(corpus.ref) == 40
    assert corpus.source == "https://github.com/vllm-project/recipes"
    assert corpus.captured_at


def test_every_indexed_slug_resolves_and_every_accessor_answers(corpus):
    """Every reader, over every slug. The corpus is not as uniform as one file suggests.

    Reading a handful of recipes by hand and generalising is how `tp` was taken for
    an integer: it is a per-hardware map on eleven of them, and the accessor raised
    on all eleven while every test passed. A loop over the whole corpus is the only
    thing that finds the shape nobody wrote down.
    """
    for slug in corpus.slugs():
        match = corpus.lookup(slug)
        assert match is not None, slug
        assert match.recipe.path == f"models/{match.recipe.slug}.yaml"
        assert match.source_uri.endswith(match.recipe.path)

        declared = match.declared_tensor_parallel()
        assert declared is None or declared >= 1, slug
        for profile in corpus.hardware_profiles.values():
            tp = match.tensor_parallel_for(profile)
            assert tp >= 1, f"{slug} on {profile.name}"
            assert profile.generation == "cpu" or tp <= (profile.gpu_count or 1), f"{slug} on {profile.name}"
            for name in match.recipe.effective_strategies:
                assert match.nodes_for(name, profile) >= 1, f"{slug}/{name} on {profile.name}"
                match.supports(name, profile)

        assert isinstance(match.recipe.docker_image, str), slug

        floor = match.replica_memory_mib_min()
        assert floor is None or floor > 0, slug
        assert all(isinstance(arg, str) for arg in match.args()), slug
        assert set(match.recipe.default_features).isdisjoint(match.recipe.opt_in_features), slug


def test_every_named_strategy_and_hardware_profile_is_defined(corpus):
    """The corpus keeps a controlled vocabulary; a refresh that loses half of it is a bug."""
    for recipe in corpus.recipes():
        for name in recipe.compatible_strategies:
            assert name in corpus.strategies, f"{recipe.slug} names strategy {name}"
        for name in recipe.hardware:
            assert name in corpus.hardware_profiles, f"{recipe.slug} names hardware {name}"


def test_a_family_slug_and_a_requantized_variant_both_find_the_recipe(corpus):
    """The FP8 repo is somebody else's, and is still described by the family's recipe."""
    family = corpus.lookup("Google/gemma-4-26B-A4B-it")
    variant = corpus.lookup("RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic")

    assert family is not None and variant is not None
    assert variant.recipe.slug == family.recipe.slug
    assert family.variant.key == "default"
    assert variant.variant.key == "fp8"
    assert variant.variant.precision == "fp8"
    assert variant.replica_memory_mib_min() < family.replica_memory_mib_min()


def test_a_lookup_is_case_insensitive_and_a_miss_is_none(corpus):
    """The Hub treats a slug case-insensitively and the corpus preserves the author's spelling."""
    assert corpus.lookup("qwen/QWEN3-32b") is not None
    assert corpus.lookup("nobody/not-a-model") is None
    assert corpus.lookup("") is None


def test_the_link_to_follow_is_the_branch_and_the_permalink_is_kept_beside_it(corpus):
    """Two URIs answering two questions: what upstream says now, and what we read.

    A person clicking through from the deploy form is asking what vLLM recommends
    for these weights, and a 40-character sha answers a different question — it
    lands them on a detached view of a corpus that has moved since. The permalink
    stays, because a claim nobody can read back against its own bytes is not one.
    """
    match = corpus.lookup("openai/gpt-oss-20b")
    assert match.source_uri == ("https://github.com/vllm-project/recipes/blob/main/models/openai/gpt-oss-20b.yaml")
    assert match.snapshot_uri == (
        f"https://github.com/vllm-project/recipes/blob/{corpus.ref}/models/openai/gpt-oss-20b.yaml"
    )


def test_a_missing_or_broken_snapshot_is_an_empty_corpus_and_not_a_crash(tmp_path):
    """Registering an artifact must not fail because a table on disk went missing."""
    assert len(vllm_recipes.load(tmp_path / "absent.json")) == 0

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert len(vllm_recipes.load(broken)) == 0
    assert vllm_recipes.load(broken).lookup("openai/gpt-oss-20b") is None


def _profile(name: str, generation: str = "", gpu_count: int = 8, vram_gb: int = 0, brand: str = "NVIDIA"):
    return HardwareProfile(name=name, generation=generation, brand=brand, gpu_count=gpu_count, vram_gb=vram_gb)


def _corpus(**recipe) -> RecipeCorpus:
    document = {
        "source": "https://example.invalid/recipes",
        "ref": "f" * 40,
        "strategies": {
            "single_node_tp": {"multi_node": False, "min_gpus": 1, "max_gpus": 8},
            "multi_node_tp_pp": {"multi_node": True, "min_gpus": 16},
        },
        "recipes": {"acme/model": {"path": "models/acme/model.yaml", **recipe}},
        "index": {"acme/model": {"recipe": "acme/model", "variant": "default"}},
    }
    return RecipeCorpus(json.loads(json.dumps(document)))


def test_the_baseline_command_is_base_then_variant_then_default_on_features():
    """Opt-in features are off in the tested baseline, which is the point of the list."""
    corpus = _corpus(
        model={"base_args": ["--base"]},
        variants={"default": {"extra_args": ["--variant"]}},
        features={
            "tool_calling": {"args": ["--tool-call-parser", "hermes"]},
            "spec_decoding": {"args": ["--speculative-config", "{}"]},
        },
        opt_in_features=["spec_decoding"],
    )
    match = corpus.lookup("acme/model")

    assert match.args() == ("--base", "--variant", "--tool-call-parser", "hermes")
    assert match.flag("--tool-call-parser") == "hermes"
    assert match.recipe.default_features == ("tool_calling",)
    assert not match.has_flag("--speculative-config")


def test_a_flag_reads_the_last_value_and_a_bare_switch_has_none():
    """The engine reads its own argv the same way, so a variant may override a base flag."""
    corpus = _corpus(
        model={"base_args": ["--gpu-memory-utilization", "0.9", "--enable-prefix-caching"]},
        variants={"default": {"extra_args": ["--gpu-memory-utilization", "0.95"]}},
    )
    match = corpus.lookup("acme/model")

    assert match.flag("--gpu-memory-utilization") == "0.95"
    assert match.flag("--enable-prefix-caching") is None
    assert match.has_flag("--enable-prefix-caching")
    assert match.flag("--absent") is None


def test_a_declared_tp_comes_from_the_variant_first_then_the_strategy_override():
    """A recipe that names neither has not said one — it has said "ask the hardware"."""
    assert _corpus(variants={"default": {"tp": 2}}).lookup("acme/model").declared_tensor_parallel() == 2
    assert (
        _corpus(strategy_overrides={"single_node_tp": {"tp": 8}}).lookup("acme/model").declared_tensor_parallel() == 8
    )
    assert (
        _corpus(variants={"default": {"tp": 2}}, strategy_overrides={"single_node_tp": {"tp": 8}})
        .lookup("acme/model")
        .declared_tensor_parallel()
        == 2
    )
    assert _corpus().lookup("acme/model").declared_tensor_parallel() is None


def test_a_recipe_that_can_only_run_inside_one_interconnect_says_so():
    single = _corpus(compatible_strategies=["single_node_tp"])
    spread = _corpus(compatible_strategies=["single_node_tp", "multi_node_tp_pp"])

    assert all(not s.multi_node for s in single.strategies_for(single.lookup("acme/model").recipe))
    assert any(s.multi_node for s in spread.strategies_for(spread.lookup("acme/model").recipe))


async def test_the_draft_carries_the_upstream_recipe_attributed_to_its_source():
    draft = await service().resolve("openai/gpt-oss-20b")

    assert draft.upstream is not None
    assert draft.upstream.source == "vllm-recipes"
    assert draft.upstream.source_uri.startswith("https://github.com/vllm-project/recipes/blob/main/")
    assert draft.upstream.snapshot_uri.startswith("https://github.com/vllm-project/recipes/blob/")
    assert draft.upstream.engine == "vllm"
    assert draft.upstream.engine_version_min
    assert draft.upstream.tool_call_parser == "openai"
    assert "h200" in draft.upstream.verified_hardware


async def test_uncovered_weights_resolve_without_corpus_or_remark():
    """A miss is the ordinary case — the corpus covers a few hundred repos, the Hub a million."""
    model = hf_model(slug="acme/not-in-the-corpus")

    draft = await service(hub=FakeHub(model)).resolve(model.slug)

    assert draft.upstream is None
    assert draft.serving.device_count >= 1
    assert not any("vLLM recipe" in note.message for note in draft.notes)


def test_the_upstream_recipe_never_moves_the_plane_s_own_sizing():
    """Two answers to two different questions. Merging them loses which is which."""
    model = hf_model()
    draft = resolved(model)

    assert draft.upstream.replica_memory_mib_min == 16 * 1024
    assert draft.serving.device_memory_mib_min != draft.upstream.replica_memory_mib_min
    assert draft.serving.device_count == 1


def test_sizing_off_by_more_than_double_is_warned():
    """Our arithmetic and their measurement agreeing is the evidence that either is right."""
    ours = resolved(hf_model())
    assert not any("disagree" in note.message for note in ours.notes)

    inflated = resolved(hf_model(parameter_count=hf_model().parameter_count * 8))
    warning = [note for note in inflated.notes if "disagree" in note.message]
    assert len(warning) == 1
    assert warning[0].level == "warn"


def test_a_tp_written_per_hardware_resolves_by_id_then_generation_then_brand():
    """Their precedence, and eleven recipes depend on it. Merging the keyspace is wrong.

    A map is not a lookup table of equals: `blackwell` must not answer for an H200,
    and `default` must lose to anything more specific.
    """
    corpus = _corpus(
        strategy_overrides={"single_node_tp": {"tp": {"h200": 2, "blackwell": 4, "default": 1}}},
    )
    match = corpus.lookup("acme/model")

    assert match.declared_tensor_parallel() is None
    assert match.tensor_parallel_for(_profile("h200", generation="hopper", gpu_count=8)) == 2
    assert match.tensor_parallel_for(_profile("b200", generation="blackwell", gpu_count=8)) == 4
    assert match.tensor_parallel_for(_profile("mi355x", generation="cdna4", gpu_count=8)) == 1


def test_a_declared_tp_is_clamped_to_the_node_and_an_undeclared_one_auto_fits():
    """Their rule: fit on one device, or take the whole node. Never half of it."""
    declared = _corpus(variants={"default": {"tp": 8}}).lookup("acme/model")
    assert declared.tensor_parallel_for(_profile("gb200", gpu_count=4)) == 4

    fits = _corpus(variants={"default": {"vram_minimum_gb": 60}}).lookup("acme/model")
    assert fits.tensor_parallel_for(_profile("h200", gpu_count=8, vram_gb=1128)) == 1

    spills = _corpus(variants={"default": {"vram_minimum_gb": 400}}).lookup("acme/model")
    assert spills.tensor_parallel_for(_profile("h200", gpu_count=8, vram_gb=1128)) == 8


def test_a_trillion_parameter_recipe_reads_as_a_replica_total_and_not_a_card(corpus):
    """Kimi K3: 2.8T parameters, 1.6 TB, and no single card that could hold it.

    The published floor is the whole replica. Multiplying it by the tested
    parallelism — or handing it to placement as a per-device minimum — asks for
    hardware that does not exist, and every smaller model in the corpus hides the
    error because its `tp` is one.
    """
    match = corpus.lookup("moonshotai/Kimi-K3")

    assert match.replica_memory_mib_min() > 1_000_000
    assert match.declared_tensor_parallel() is None
    assert match.recipe.context_length == 1_048_576
    assert match.flag("--tool-call-parser") == "kimi_k3"
    assert match.flag("--reasoning-parser") == "kimi_k3"
    assert match.has_flag("--trust-remote-code")

    strategies = corpus.strategies_for(match.recipe)
    assert any(strategy.multi_node for strategy in strategies)


def test_a_per_gpu_gate_opts_a_strategy_out_and_opts_an_unlisted_one_in():
    """`strategy_hardware` overrides membership in both directions. Kimi K3 uses both."""
    corpus = _corpus(
        compatible_strategies=["single_node_tp", "multi_node_dep"],
        strategy_hardware={
            "multi_node_dep": {"h200": "unsupported"},
            "multi_node_tp_dp": {"h200": "supported"},
        },
    )
    match = corpus.lookup("acme/model")
    h200 = _profile("h200", gpu_count=8)
    b200 = _profile("b200", gpu_count=8)

    assert "multi_node_tp_dp" in match.recipe.effective_strategies
    assert "multi_node_tp_dp" not in match.recipe.compatible_strategies
    assert match.supports("multi_node_tp_dp", h200)
    assert not match.supports("multi_node_tp_dp", b200)
    assert not match.supports("multi_node_dep", h200)
    assert match.supports("multi_node_dep", b200)


def test_a_gpu_floor_written_per_card_raises_the_node_count_for_that_card():
    """The corpus's own worked example: K3 clears its floor on H200 and needs four H100 nodes."""
    corpus = _corpus(strategy_min_gpus={"single_node_tp": 8, "h100": 32})
    match = corpus.lookup("acme/model")

    assert match.nodes_for("single_node_tp", _profile("h200", gpu_count=8)) == 1
    assert match.nodes_for("single_node_tp", _profile("h100", gpu_count=8)) == 4
    assert _corpus().lookup("acme/model").nodes_for("single_node_tp", _profile("h100", gpu_count=8)) == 1


def test_the_baseline_command_is_deduplicated_last_wins_the_way_they_render_it():
    """A variant lowering a base value means to replace it, not to be appended after it."""
    match = _corpus(
        model={"base_args": ["--trust-remote-code", "--gpu-memory-utilization", "0.95"]},
        variants={"default": {"extra_args": ["--gpu-memory-utilization", "0.9"]}},
    ).lookup("acme/model")

    assert match.args() == ("--trust-remote-code", "--gpu-memory-utilization", "0.9")
    assert match.args().count("--gpu-memory-utilization") == 1


def test_a_model_that_needs_many_nodes_is_not_claimed_to_fit_one():
    """`requires_single_topology_domain` is a constraint, and a wrong one strands a replica."""
    draft = resolved(hf_model(slug="moonshotai/Kimi-K3", parameter_count=2_800_000_000_000))

    assert draft.upstream.requires_single_topology_domain is False
    assert draft.upstream.replica_memory_mib_min == 1680 * 1024
    assert draft.upstream.tensor_parallel is None
    assert draft.upstream.engine_version_min == "0.27.1"

    on = {d.hardware: d for d in draft.upstream.deployments}
    assert on["h200"].tensor_parallel == 8
    assert on["gb200"].tensor_parallel == 4
    assert on["h200"].nodes_by_memory == 2
    assert on["ascend_910c"].nodes_declared == 4
    assert "multi_node_dep" not in on["h200"].strategies
    assert "multi_node_dep" in on["b200"].strategies
    assert "multi_node_tp_dp" in on["h200"].strategies
    assert "gb300" in draft.upstream.verified_hardware
    assert "spec_decoding" in draft.upstream.features_opt_in
    assert "--speculative-config" not in draft.upstream.args


def test_the_published_floor_is_compared_as_a_replica_total_not_per_device(corpus):
    """2.8T parameters derive 1456 GB against a published 1680 GB — the same sizing.

    Multiplying the published figure by the tested parallelism would have called
    these two an order of magnitude apart and warned on a draft that is right.
    """
    draft = resolved(hf_model(slug="moonshotai/Kimi-K3", parameter_count=2_800_000_000_000))
    ours = draft.serving.device_count * draft.serving.device_memory_mib_min

    assert draft.serving.device_count > 1
    assert 0.5 < ours / draft.upstream.replica_memory_mib_min < 2.0
    assert not any("disagree" in note.message for note in draft.notes)


def test_a_variant_that_caps_the_context_below_what_the_model_declares_says_so(corpus):
    """The Ascend build of Kimi K3 was served at 128k against a declared 1M window.

    A tested `--max-model-len` under the config's own context is the corpus
    disagreeing with the model card, and it is the number somebody actually ran.
    """
    match = corpus.lookup("eco-tech/Kimi-K3-w4a8")

    assert match.recipe.context_length == 1_048_576
    assert match.flag("--max-model-len") == "131072"
    assert match.flag("--gpu-memory-utilization") == "0.9"


def test_a_recipe_naming_its_own_image_reports_it_and_the_rest_report_nothing(corpus):
    """Three shapes in the corpus and only one of them is a plain string.

    `docker_image` is a string on some recipes and a vendor-keyed map on others,
    and reading either one as the other is how a model that needs its own build
    looks like a model that does not.
    """
    assert corpus.lookup("zai-org/GLM-5.3-Flash").recipe.docker_image == "vllm/vllm-openai:glm53-flash"
    assert corpus.lookup("MiniMaxAI/MiniMax-H3").recipe.docker_image == "vllm/vllm-omni:minimax-h3"
    assert corpus.lookup("Google/diffusiongemma-26B-A4B-it").recipe.docker_image == "vllm/vllm-openai:gemma"
    assert corpus.lookup("Qwen/Qwen3-32B").recipe.docker_image == ""


def test_an_image_published_only_for_amd_is_not_offered_as_ours(corpus):
    """A ROCm build on an H200 is a wrong answer, not a degraded one.

    Reading the map with a fallback would hand a CUDA cluster an image that cannot
    start, and it would do it for the recipes that say most clearly they need a
    build of their own.
    """
    amd_only = [
        recipe
        for recipe in corpus.recipes()
        if isinstance(recipe.model.get("docker_image"), dict) and not recipe.model["docker_image"].get("nvidia")
    ]

    assert amd_only, "the corpus no longer has an AMD-only image to check"
    for recipe in amd_only:
        assert recipe.model["docker_image"].get("amd"), recipe.slug
        assert recipe.docker_image == "", recipe.slug


def test_a_draft_carries_the_image_the_corpus_names_without_proposing_it(corpus):
    """Evidence that the derived image will not serve, not a value to copy.

    The image named here has no `ai-dynamo` in it, so a draft that seeded
    `engine_runtime_image` from it would trade a Transformers error for a missing module
    and be no closer to serving.
    """
    draft = resolved(hf_model(slug="zai-org/GLM-5.3-Flash", parameter_count=321_000_000_000))

    assert draft.upstream.docker_image == "vllm/vllm-openai:glm53-flash"
    assert draft.upstream.nightly_required
    assert draft.upstream.engine_version_min == "0.29.0"


def test_a_model_the_ordinary_build_serves_names_no_image(corpus):
    """Absent is the common case and must not read as an empty string on the wire."""
    draft = resolved(hf_model(slug="Qwen/Qwen3-32B", parameter_count=32_000_000_000))

    assert draft.upstream.docker_image is None
