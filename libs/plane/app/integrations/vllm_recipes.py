"""The vLLM recipe corpus, read as serving parameters somebody has already run.

`config.json` says what a model *is*. It does not say which tensor-parallel size
was tested, which tool-call parser the chat template needs, or that the FP8
checkpoint lives in a different repo. Those are somebody's serving experience,
and vLLM keeps 180 models' worth of it in a machine-readable corpus at
`vllm-project/recipes`: one YAML per HuggingFace slug, plus a hardware taxonomy
and a table of parallelism strategies.

The path in that repo *is* the slug (`models/<org>/<repo>.yaml`), so the lookup
key is `metadata.huggingface_id`, which the plane already stores.

Two boundaries hold here and are the reason this is an integration instead of a
module:

*Nothing here is a fact about our clusters.* `hardware: {h200: verified}` means
vLLM served it on their H200, and a `vram_minimum_gb` is their measurement. Every
value this module returns is an assertion with a `source_uri`, offered to a person
filling in a recipe form and never applied to one.

*Nothing here touches the network.* The corpus is snapshotted into
`data/vllm-recipes.json` by `just vllm-recipes` and read from disk, on the same
argument as `contract/devices.json`: registering an artifact must not fail because
GitHub is down, and a corpus that changes under a running plane makes two
identical resolves disagree.
"""

from __future__ import annotations

import os
import json
import logging

from typing import Any
from typing import Final
from pathlib import Path
from dataclasses import field
from dataclasses import dataclass


logger = logging.getLogger(__name__)

SNAPSHOT_ENV: Final = "HANGAR_VLLM_RECIPES"

SNAPSHOT_PATH: Final = Path(
    os.environ.get(SNAPSHOT_ENV) or Path(__file__).resolve().parent / "data" / "vllm-recipes.json"
)

VERIFIED: Final = "verified"
UNSUPPORTED: Final = "unsupported"
SUPPORTED: Final = "supported"

DEFAULT_GPUS_PER_NODE: Final = 8
"""What a node holds when a profile does not say. The corpus's own fallback."""

MAX_NODES: Final = 16
"""The largest cluster the corpus will size a strategy across. Theirs, not ours."""

DEFAULT_BRANCH: Final = "main"
"""The branch a reader should be sent to, which is the one the corpus develops on.

Not the branch the snapshot was taken from: that is a commit, and `snapshot_uri`
is where it is written. A permalink says what was stored and a branch link says what
upstream says now; a person following a link from a form wants the second.
"""

_MIB_PER_GB: Final = 1024

_SINGLE_NODE_TP: Final = "single_node_tp"


@dataclass(frozen=True)
class HardwareProfile:
    """One node shape the corpus reports results on.

    `vram_gb` is the whole node, not the card: `h200` is 8 devices and 1128 GB.
    Dividing is the caller's business, because a profile with `gpu_count` zero is
    a corpus bug and not something to hide behind a division here.
    """

    name: str
    brand: str = ""
    generation: str = ""
    display_name: str = ""
    gpu_count: int = 0
    vram_gb: int = 0
    multi_node: bool = False


@dataclass(frozen=True)
class Strategy:
    """A parallelism shape and the node count it implies.

    `multi_node` false is the interesting field: it is the corpus saying this
    strategy has to fit inside one interconnect, which is our
    `requires_single_topology_domain` under another name.
    """

    name: str
    deploy_type: str = ""
    display_name: str = ""
    orientation: str = ""
    parallel_flag: str = ""
    vllm_args: tuple[str, ...] = ()
    min_gpus: int | None = None
    max_gpus: int | None = None
    multi_node: bool = False


@dataclass(frozen=True)
class Variant:
    """One precision of one model, which may be a different repo entirely.

    `model_id` is set only when the variant is published separately: an FP8 or
    NVFP4 requantization by RedHat or NVIDIA. That makes those slugs registrable
    artifacts in their own right, which is why the snapshot indexes them back to
    the recipe that describes them.
    """

    key: str
    model_id: str | None = None
    label: str = ""
    precision: str = ""
    vram_minimum_gb: int | None = None
    tp: int | dict[str, int] | None = None
    """The tested parallelism: a number, or one number per hardware profile.

    Both forms are in the corpus and neither is a special case of the other. A
    Llama 3.3 70B is `{"xeon6": 8}` and nothing elsewhere, which says the tested
    size is a property of the node and not of the model.
    """
    extra_args: tuple[str, ...] = ()
    extra_env: dict[str, str] = field(default_factory=dict)
    min_vllm_version: str = ""
    supported_hardware: tuple[str, ...] = ()


@dataclass(frozen=True)
class Recipe:
    """One recipe file, with the guide prose dropped.

    `features` are keyed the way the corpus keys them (`tool_calling`,
    `reasoning`, `spec_decoding`, …) and hold the exact flags. A feature listed in
    `opt_in_features` is off in the tested baseline; every other feature is on,
    and that distinction is the difference between reproducing a working
    configuration and inventing a new one.
    """

    slug: str
    path: str = ""
    title: str = ""
    provider: str = ""
    tasks: tuple[str, ...] = ()
    hardware: dict[str, str] = field(default_factory=dict)
    model: dict[str, Any] = field(default_factory=dict)
    dependencies: tuple[dict[str, Any], ...] = ()
    features: dict[str, dict[str, Any]] = field(default_factory=dict)
    opt_in_features: tuple[str, ...] = ()
    variants: dict[str, Variant] = field(default_factory=dict)
    compatible_strategies: tuple[str, ...] = ()
    default_strategy: str = ""
    strategy_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    strategy_hardware: dict[str, dict[str, str]] = field(default_factory=dict)
    strategy_min_gpus: Any = None

    @property
    def min_vllm_version(self) -> str:
        return str(self.model.get("min_vllm_version") or "")

    @property
    def nightly_required(self) -> bool:
        return bool(self.model.get("nightly_required"))

    @property
    def docker_image(self) -> str:
        """The image the corpus serves this model from, on NVIDIA hardware.

        Empty on most recipes, which is the corpus saying the ordinary engine build
        serves them. Set on the few that need a build of their own: GLM-5.3-Flash
        is `vllm/vllm-openai:glm53-flash`, a vLLM with the GLA/KDA kernels and the
        `glm5_next` architecture that no released runtime has.

        Two shapes, because the corpus writes both: a string is one image for every
        accelerator, a mapping is keyed by vendor. Only `nvidia` is ours, and an
        AMD-only entry reads as absent, not as a fallback: a ROCm image on
        an H200 is not a degraded answer, it is a wrong one.

        This is evidence, never a value to copy into `engine_runtime_image`. The image it
        names is somebody else's runtime and has no `ai-dynamo` in it, so the
        operator's `python3 -m dynamo.vllm` does not exist there. What it supports
        is the only accurate claim: that the image the plane would derive will not
        load these weights.
        """
        value = self.model.get("docker_image")
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            return str(value.get("nvidia") or "").strip()
        return ""

    @property
    def architecture(self) -> str:
        return str(self.model.get("architecture") or "")

    @property
    def context_length(self) -> int | None:
        value = self.model.get("context_length")
        return int(value) if isinstance(value, int) else None

    @property
    def base_args(self) -> tuple[str, ...]:
        return tuple(str(arg) for arg in self.model.get("base_args") or ())

    @property
    def verified_hardware(self) -> tuple[str, ...]:
        """The profiles the corpus claims an end-to-end run on."""
        return tuple(sorted(name for name, status in self.hardware.items() if status == VERIFIED))

    @property
    def effective_strategies(self) -> tuple[str, ...]:
        """`compatible_strategies` plus anything `strategy_hardware` grants somewhere.

        Membership is the default and the gate overrides it in both directions: a
        listed strategy can be `unsupported` on one card, and an unlisted one can
        be `supported` on another, which is how a layout validated on a few GPUs
        is declared once instead of listed globally and opted out everywhere else.
        Reading the list alone understates where a model can run.
        """
        granted = tuple(
            name
            for name, gate in self.strategy_hardware.items()
            if name not in self.compatible_strategies and SUPPORTED in (gate or {}).values()
        )
        return self.compatible_strategies + granted

    @property
    def default_features(self) -> tuple[str, ...]:
        """The features on in the tested baseline: everything not opt-in."""
        return tuple(name for name in self.features if name not in self.opt_in_features)


@dataclass(frozen=True)
class RecipeMatch:
    """A recipe and the variant a slug landed on.

    A slug matching a variant's own repo is a *stronger* match than one matching
    the family, because the precision is then known, not assumed. Callers
    that only want the family can ignore `variant`; callers sizing a deployment
    should not.
    """

    recipe: Recipe
    variant: Variant
    source: str
    ref: str

    @property
    def source_uri(self) -> str:
        """The recipe on the corpus's own branch: what upstream says about this model now.

        The link a person follows, and therefore the branch instead of the commit.
        A reader clicking through from a form is asking what vLLM recommends, not
        what this plane happened to capture, and a 40-character sha lands them on
        a detached file view of a corpus that has moved on since. The snapshot is
        still recoverable: `snapshot_uri` names the exact bytes these values were
        read from, for the case where the two disagree.
        """
        return f"{self.source}/blob/{DEFAULT_BRANCH}/{self.recipe.path}"

    @property
    def snapshot_uri(self) -> str:
        """The same file at the commit this snapshot was captured from.

        The provenance link, pinned, so a claim can be read back against the bytes
        that produced it. `source_uri` is the one to follow; this is the one to
        cite when a value on a form and the file it came from no longer agree.
        """
        return f"{self.source}/blob/{self.ref}/{self.recipe.path}"

    def args(self) -> tuple[str, ...]:
        """The flags of the tested baseline: base, variant, then default-on features.

        Order is the corpus's own (base, variant, then features) and the result
        is deduplicated last-wins on the flag, which is what their renderer emits.
        A variant that lowers `--gpu-memory-utilization` from the base value means
        to replace it, and a command carrying both is not the one they publish.

        This is the single-node baseline only. Multi-node shapes add per-strategy
        and per-role flags that make sense only once a node count and a role are
        chosen, and inventing either here would put a command in front of somebody
        that nobody has run.
        """
        out = list(self.recipe.base_args)
        out.extend(self.variant.extra_args)
        for name in self.recipe.default_features:
            out.extend(str(arg) for arg in self.recipe.features[name].get("args") or ())
        return _dedupe(out)

    def flag(self, name: str) -> str | None:
        """The value of one flag in the baseline, or None.

        Last occurrence wins, matching how the engine reads its own argv. A flag
        present with no value after it is a valueless switch and returns None,
        which `has_flag` is for.
        """
        args = self.args()
        found: str | None = None
        for index, arg in enumerate(args):
            if arg == name and index + 1 < len(args) and not args[index + 1].startswith("--"):
                found = args[index + 1]
        return found

    def has_flag(self, name: str) -> bool:
        return name in self.args()

    def declared_tensor_parallel(self) -> int | None:
        """The one TP size the recipe writes down, independent of hardware.

        Rarely the whole answer, and never the answer for a large model. A recipe
        that writes nothing is not defaulting to one: the corpus resolves TP
        against the node it is about to run on, which is `tensor_parallel_for`.
        """
        for candidate in self._tp_candidates():
            if isinstance(candidate, int):
                return candidate
        return None

    def tensor_parallel_for(self, profile: HardwareProfile) -> int:
        """The TP size the corpus would render for this node shape.

        Their precedence, kept in their order: `variant.tp`, then
        `strategy_overrides.single_node_tp.tp` (either one a bare number or a map
        keyed by GPU id, generation, brand or `default`), and then an auto-fit from
        the published memory floor.

        The auto-fit is intentionally binary: one device when the weights fit on
        one, the whole node when they do not. Half a node is skipped because it
        leaves devices idle without buying the latency that would justify it. A
        declared size is clamped to the node, since a recipe naming eight cannot
        have meant eight on a four-card tray.
        """
        gpu_count = profile.gpu_count or 1
        for candidate in self._tp_candidates():
            declared = _hardware_keyed(candidate, profile)
            if isinstance(declared, int) and declared > 0:
                return declared if profile.generation == "cpu" else min(declared, gpu_count)
        floor_gb = self.variant.vram_minimum_gb or 0
        per_device_gb = profile.vram_gb / gpu_count if profile.vram_gb and gpu_count else 0
        if not floor_gb or per_device_gb <= 0:
            return gpu_count
        return 1 if floor_gb <= per_device_gb else gpu_count

    def min_gpus_for(self, strategy: str, profile: HardwareProfile) -> int:
        """The GPU floor a strategy needs here, or 0 when the recipe sets none.

        `strategy_min_gpus` is a scalar bar, a per-strategy map, or a per-GPU map
        of per-strategy bars, and the highest applicable one wins. Only exact GPU
        ids are read for the hardware layer, never generations: two cards of one
        generation differ in memory, which is the whole reason the floor moves.
        """
        config = self.recipe.strategy_min_gpus
        if config is None:
            return 0
        if isinstance(config, int):
            return config
        if not isinstance(config, dict):
            return 0
        return max(
            _min_gpus_in(config, strategy),
            _min_gpus_in(config.get(profile.name), strategy),
        )

    def nodes_for(self, strategy: str, profile: HardwareProfile) -> int:
        """Nodes needed to clear that floor at this node's device count."""
        per_node = profile.gpu_count or DEFAULT_GPUS_PER_NODE
        return max(1, -(-self.min_gpus_for(strategy, profile) // per_node))

    def supports(self, strategy: str, profile: HardwareProfile) -> bool:
        """Whether this strategy is offered on this node shape.

        The per-GPU gate outranks membership in either direction, so a strategy in
        `compatible_strategies` can still be `unsupported` on one card.
        """
        gate = _hardware_keyed(self.recipe.strategy_hardware.get(strategy), profile)
        if isinstance(gate, str):
            return gate != UNSUPPORTED
        return strategy in self.recipe.compatible_strategies

    def _tp_candidates(self) -> tuple[Any, ...]:
        """Where a tested TP size can be written, most specific first."""
        override = self.recipe.strategy_overrides.get(_SINGLE_NODE_TP) or {}
        return (self.variant.tp, override.get("tp"))

    def replica_memory_mib_min(self) -> int | None:
        """The variant's measured floor across the whole replica, in MiB.

        `vram_minimum_gb` is the total the model needs, not a per-device figure:
        Kimi K3 reports 1680 GB, which is twelve H200s and no single card ever
        made. Reading it as per-device is the arithmetic that turns a recipe into
        a placement nothing can satisfy, so the name says which it is.
        """
        if self.variant.vram_minimum_gb is None:
            return None
        return int(self.variant.vram_minimum_gb) * _MIB_PER_GB


class RecipeCorpus:
    """The snapshot, indexed by slug. Empty is a working state.

    Coverage is 180 models against a Hub of a million, so a miss is the ordinary
    case and never an error. Every caller must already have an answer without
    this: the corpus improves a suggestion, it does not make one possible.
    """

    def __init__(self, document: dict[str, Any] | None = None) -> None:
        document = document or {}
        self.source: str = str(document.get("source") or "")
        self.ref: str = str(document.get("ref") or "")
        self.captured_at: str = str(document.get("captured_at") or "")
        self.hardware_profiles: dict[str, HardwareProfile] = {
            name: HardwareProfile(name=name, **spec) for name, spec in (document.get("hardware_profiles") or {}).items()
        }
        self.strategies: dict[str, Strategy] = {
            name: Strategy(
                name=name,
                deploy_type=spec.get("deploy_type") or "",
                display_name=spec.get("display_name") or "",
                orientation=spec.get("orientation") or "",
                parallel_flag=spec.get("parallel_flag") or "",
                vllm_args=tuple(spec.get("vllm_args") or ()),
                min_gpus=spec.get("min_gpus"),
                max_gpus=spec.get("max_gpus"),
                multi_node=bool(spec.get("multi_node")),
            )
            for name, spec in (document.get("strategies") or {}).items()
        }
        self._recipes: dict[str, Recipe] = {
            slug: _recipe(slug, spec) for slug, spec in (document.get("recipes") or {}).items()
        }
        self._index: dict[str, tuple[str, str]] = {
            key: (entry["recipe"], entry["variant"]) for key, entry in (document.get("index") or {}).items()
        }

    def __len__(self) -> int:
        return len(self._recipes)

    def recipes(self) -> tuple[Recipe, ...]:
        """Every recipe in the snapshot, for callers auditing the corpus itself."""
        return tuple(self._recipes.values())

    def slugs(self) -> tuple[str, ...]:
        """Every slug that resolves, families and separately published variants alike."""
        return tuple(self._index)

    def lookup(self, slug: str) -> RecipeMatch | None:
        """The recipe for a HuggingFace slug, family or variant repo, or None.

        Case-insensitive, because the Hub is: `Qwen/Qwen3-32B` and `qwen/qwen3-32b`
        are one repo and the corpus preserves the author's capitalization.
        """
        if not slug:
            return None
        found = self._index.get(slug.strip().strip("/").lower())
        if found is None:
            return None
        recipe = self._recipes.get(found[0])
        if recipe is None:
            return None
        variant = recipe.variants.get(found[1]) or Variant(key=found[1])
        return RecipeMatch(recipe=recipe, variant=variant, source=self.source, ref=self.ref)

    def strategies_for(self, recipe: Recipe) -> tuple[Strategy, ...]:
        """The strategies a recipe declares, dropping any the taxonomy lost."""
        return tuple(self.strategies[name] for name in recipe.effective_strategies if name in self.strategies)


def _variant(key: str, spec: dict[str, Any]) -> Variant:
    return Variant(
        key=key,
        model_id=spec.get("model_id"),
        label=spec.get("label") or "",
        precision=spec.get("precision") or "",
        vram_minimum_gb=spec.get("vram_minimum_gb"),
        tp=spec.get("tp"),
        extra_args=tuple(str(arg) for arg in spec.get("extra_args") or ()),
        extra_env={str(k): str(v) for k, v in (spec.get("extra_env") or {}).items()},
        min_vllm_version=spec.get("min_vllm_version") or "",
        supported_hardware=tuple(spec.get("supported_hardware") or ()),
    )


def _recipe(slug: str, spec: dict[str, Any]) -> Recipe:
    return Recipe(
        slug=slug,
        path=spec.get("path") or "",
        title=spec.get("title") or "",
        provider=spec.get("provider") or "",
        tasks=tuple(spec.get("tasks") or ()),
        hardware=dict(spec.get("hardware") or {}),
        model=dict(spec.get("model") or {}),
        dependencies=tuple(spec.get("dependencies") or ()),
        features=dict(spec.get("features") or {}),
        opt_in_features=tuple(spec.get("opt_in_features") or ()),
        variants={key: _variant(key, value or {}) for key, value in (spec.get("variants") or {}).items()},
        compatible_strategies=tuple(spec.get("compatible_strategies") or ()),
        default_strategy=spec.get("default_strategy") or "",
        strategy_overrides=dict(spec.get("strategy_overrides") or {}),
        strategy_hardware=dict(spec.get("strategy_hardware") or {}),
        strategy_min_gpus=spec.get("strategy_min_gpus"),
    )


def load(path: Path | None = None) -> RecipeCorpus:
    """Reads the snapshot. A missing or broken file is not fatal."""
    source = path or SNAPSHOT_PATH
    try:
        document = json.loads(source.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("vllm_recipes.snapshot_unreadable path=%s error=%s", source, exc)
        return RecipeCorpus()
    return RecipeCorpus(document)


_corpus: RecipeCorpus | None = None


def corpus() -> RecipeCorpus:
    """The process-wide corpus, parsed once."""
    global _corpus
    if _corpus is None:
        _corpus = load()
    return _corpus


def lookup(slug: str) -> RecipeMatch | None:
    """Convenience for the common call: one slug against the process-wide corpus."""
    return corpus().lookup(slug)


def _hardware_keyed(value: Any, profile: HardwareProfile) -> Any:
    """Resolves a hardware-keyed map against one profile, or passes a scalar through.

    Their precedence, exactly: exact GPU id, then generation, then brand
    lowercased, then `default`. Merging the keyspace instead, which is what a
    reader assumes on seeing `{"xeon6": 2}`, silently applies a generation's
    value to a card it was never measured on.
    """
    if not isinstance(value, dict):
        return value
    generation = profile.generation
    if isinstance(generation, list):
        generation = generation[0] if generation else ""
    for key in (profile.name, generation, profile.brand.lower(), "default"):
        if key and key in value:
            return value[key]
    return None


def _min_gpus_in(block: Any, strategy: str) -> int:
    """One `strategy_min_gpus` block read for one strategy.

    A bare number is a bar for everything. A map is read under the strategy id
    and under the same id with either deploy prefix, because a recipe may write
    the floor as `tep` where the strategy is `multi_node_tep`.
    """
    if isinstance(block, int):
        return block
    if not isinstance(block, dict):
        return 0
    for key in (strategy, f"multi_node_{strategy}", f"single_node_{strategy}"):
        value = block.get(key)
        if isinstance(value, int):
            return value
    return 0


def _dedupe(args: list[str]) -> tuple[str, ...]:
    """Last occurrence of each flag wins, keeping the flag with its value.

    A flag and the token after it are one unit unless that token is itself a
    flag, which is how a valueless switch keeps its place. Positional arguments
    are never deduplicated.
    """
    units: list[tuple[str, ...]] = []
    index = 0
    while index < len(args):
        current = args[index]
        following = args[index + 1] if index + 1 < len(args) else None
        if current.startswith("-") and following is not None and not following.startswith("-"):
            units.append((current, following))
            index += 2
            continue
        units.append((current,))
        index += 1

    seen: set[str] = set()
    keep: list[bool] = [False] * len(units)
    for position in range(len(units) - 1, -1, -1):
        unit = units[position]
        if not unit[0].startswith("-"):
            keep[position] = True
        elif unit[0] not in seen:
            seen.add(unit[0])
            keep[position] = True
    return tuple(token for position, unit in enumerate(units) if keep[position] for token in unit)
