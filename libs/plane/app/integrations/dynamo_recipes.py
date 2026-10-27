"""NVIDIA's Dynamo deployment recipes, read as the command line we actually run.

`vllm_recipes` is the flags of `vllm serve`. This is the flags of
`python3 -m dynamo.vllm`, and those are two different programs. Dynamo publishes
one `deploy.yaml` per model per node shape in `ai-dynamo/dynamo` under
`recipes/<model>/<backend>/<shape>/`, and the worker container in that file holds
the exact argument list the vendor runs against the exact binary the operator
starts. When the two corpora disagree about the same flag, this one is closer to
the process, and they do disagree: for `zai-org/GLM-5.3-Flash` at 1,048,576
context, vLLM's recipe writes `--max-num-seqs 512` (inside its `pd_cluster`
decode role) and Dynamo's aggregated H200 recipe writes `--max-num-seqs 32`.

Three boundaries hold here, and they are why this is an integration instead of a
module:

*Nothing here is a fact about our clusters.* A GB200 recipe is evidence about
GB200 racks. `engine_defaults` therefore takes the device class it is being asked
about and never answers from a shape it was not measured on unless it has to.

*Nothing here touches the network.* The corpus is snapshotted into
`data/dynamo-recipes.json` by `just dynamo-recipes` and read from disk, on the
same argument as `vllm-recipes.json` and `contract/devices.json`.

*Almost nothing here is adopted.* `ADOPTED_FLAGS` is one flag long and
`DECLINED_FLAGS` records, per argument, why the rest are not. The rule that
licenses adoption is narrow and is written out on both constants; a flag that
does not meet it is evidence for a person to read, not a default to apply.
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

SNAPSHOT_ENV: Final = "HANGAR_DYNAMO_RECIPES"

SNAPSHOT_PATH: Final = Path(
    os.environ.get(SNAPSHOT_ENV) or Path(__file__).resolve().parent / "data" / "dynamo-recipes.json"
)

DEFAULT_BRANCH: Final = "main"

AGGREGATED: Final = "agg"
"""The deployment topology the operator renders, and the only one read for defaults.

`render/dynamo.go` emits a Frontend and one `VllmDecodeWorker`. It has no prefill
component and no KV transfer between roles, so a disaggregated recipe's numbers
were measured on a machine layout we do not build. Kimi K3's disaggregated GB200
recipe runs its prefill worker at `--max-num-seqs 4` and its decode worker at 256;
neither is the number for a single worker doing both, and picking either would be
inventing a measurement.
"""

ADOPTED_FLAGS: Final = ("max-num-seqs",)
"""The engine arguments a Dynamo recipe is allowed to default for us.

Three conditions, all of them, and a flag that fails any one stays out:

1. **It is a property of the model, not of a cluster.** The value follows the
   weights wherever they are served.
2. **It is ceiling-shaped.** Below the published value the engine starts and runs
   slower; above it the engine refuses to start. That asymmetry is what makes it
   safe to carry a number measured on one node shape to another, and it is the
   only reason `engine_defaults` may fall back to the lowest published value when
   it has nothing for our card.
3. **The plane does not already derive it.** Everything in
   `app.core.serving.DERIVED_FLAGS` reaches the cluster from a validated, typed
   column that placement was computed against, and a recipe default landing in
   `engine_args` would beat it on the operator's own command line.

`max-num-seqs` is the whole list. Each decode sequence in a hybrid model holds one
Mamba state block, those blocks come out of what is left after weights and KV, and
at long context there are fewer of them than vLLM's default 1024 concurrent
sequences. The engine answers that with a `ValueError` at CUDA graph capture and a
crash loop, which is not an out-of-memory condition and cannot be placed around.
"""

DECLINED_FLAGS: Final[dict[str, str]] = {
    "trust-remote-code": (
        "a security decision, not a serving parameter. It makes the worker execute Python "
        "published in the model repository, and adopting it because a third-party YAML says so "
        "is the plane deciding to run somebody else's code without being asked"
    ),
    "attention-backend": "names a kernel, which is a property of the card and of the build in the image",
    "moe-backend": "as attention-backend",
    "mamba-backend": "as attention-backend",
    "linear-backend": "as attention-backend",
    "kda-prefill-backend": "as attention-backend",
    "block-size": (
        "paging granularity, chosen against the attention backend and the card. Not ceiling-shaped: "
        "a wrong value degrades throughput in either direction"
    ),
    "mamba-ssm-cache-dtype": "a memory-against-accuracy trade made per card, and the engine starts without it",
    "compilation-config": (
        "cudagraph capture sizes tuned to one node's free memory. Not ceiling-shaped, and a JSON blob "
        "the plane cannot validate"
    ),
    "max-cudagraph-capture-size": "as compilation-config",
    "speculative-config": (
        "opt-in. It names a draft method and sometimes a second model, changes the output "
        "distribution, and the model serves correctly without it"
    ),
    "tokenizer-mode": (
        "a property of the checkpoint and a candidate, held back only because getting it wrong "
        "produces wrong text instead of a refusal to start: the failure is silent, which is the "
        "opposite of ceiling-shaped"
    ),
    "hf-overrides": "patches the repository's own config; a JSON blob with no bound on what it changes",
    "limit-mm-per-prompt": (
        "already declared by the typed `serving.max_images_per_request` and "
        "`serving.max_videos_per_request` fields, which render as one map"
    ),
    "tool-call-parser": "not an argument of `dynamo.vllm`; see SERVING_FLAGS in the workloads service",
    "reasoning-parser": "declared by the typed `serving.reasoning.parser` field",
    "dyn-tool-call-parser": "a frontend concern under Dynamo, and not a flag this plane renders today",
    "dyn-reasoning-parser": "as dyn-tool-call-parser",
    "kv-transfer-config": "a disaggregation mechanism; the operator renders one worker and no transfer",
    "disaggregation-mode": "as kv-transfer-config",
    "async-scheduling": "a throughput knob measured per node, and not ceiling-shaped",
    "enable-multimodal": (
        "implied by either typed per-modality cap; see SERVING_IMPLIED_FLAGS in the workloads service"
    ),
    "enable-prefix-caching": (
        "already the engine's default and already declared: the workload's `prefix_cache_enabled` "
        "column renders the negative case and nothing else needs saying"
    ),
    "kv-events-config": "a routing mechanism between Dynamo components, not a property of the weights",
    "data-parallel-size": "a shape, and the plane's own parallelism columns are what placement ran against",
    "enable-expert-parallel": "as data-parallel-size",
    "decode-context-parallel-size": "as data-parallel-size",
    "no-enable-flashinfer-autotune": (
        "a startup-time kernel search whose cost and benefit depend on the card and the build. "
        "Recipes set it both ways for the same model"
    ),
    "mamba-cache-mode": (
        "the strongest remaining candidate. It is a property of a hybrid model's state layout, and "
        "it moves how much memory a decode sequence costs, which is the same quantity max-num-seqs "
        "is bounded by. Held back because it is not ceiling-shaped: a wrong mode is a slower or "
        "less accurate engine, not one that refuses to start, so a bad carry would be invisible"
    ),
    "no-disable-hybrid-kv-cache-manager": (
        "as mamba-cache-mode, and a double negative on top: the flag turns off a disable, and "
        "getting the polarity wrong changes memory silently"
    ),
    "load-format": "how the weights are read off disk, which depends on the volume they are read from",
    "safetensors-load-strategy": "as load-format",
    "distributed-executor-backend": "how ranks are launched, which is the operator's business and not the model's",
    "enforce-eager": "disables graph capture wholesale; a debugging setting, and a large performance loss",
    "scheduler-cls": "names a Python class inside one engine build",
}
"""Arguments the corpus publishes that this module does not adopt, and why.

Data, not prose, so that the next person to widen `ADOPTED_FLAGS` has to
read the reason the flag was left out and disagree with it in writing. Nothing
reads this map at run time; the tests read it, and so does anybody wondering why
their recipe's flag did not arrive.
"""


@dataclass(frozen=True)
class Deployment:
    """One worker launch from one `deploy.yaml`.

    `hardware` is the card the recipe names itself after, `mode` is `agg` or
    `disagg`, and `role` is what the component is called in the graph. `args` is
    the whole command line keyed without dashes: evidence, most of which is never
    adopted.
    """

    path: str
    backend: str = ""
    mode: str = ""
    role: str = ""
    hardware: str = ""
    args: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Recipe:
    """Every Dynamo launch published for one HuggingFace slug."""

    slug: str
    model_id: str = ""
    deployments: tuple[Deployment, ...] = ()


@dataclass(frozen=True)
class RecipeMatch:
    """A recipe and the corpus it was read from."""

    recipe: Recipe
    source: str
    ref: str

    def source_uri(self, deployment: Deployment) -> str:
        """One `deploy.yaml` on the corpus's own branch: what NVIDIA publishes now."""
        return f"{self.source}/blob/{DEFAULT_BRANCH}/{deployment.path}"

    def snapshot_uri(self, deployment: Deployment) -> str:
        """The same file at the commit this snapshot was captured from."""
        return f"{self.source}/blob/{self.ref}/{deployment.path}"

    def aggregated(self, engine: str) -> tuple[Deployment, ...]:
        """The single-worker launches published for one engine."""
        wanted = (engine or "").strip().lower()
        return tuple(d for d in self.recipe.deployments if d.mode == AGGREGATED and d.backend == wanted)

    def flag(self, name: str, engine: str, hardware: str = "") -> str | None:
        """The value one aggregated recipe publishes for a flag, or None.

        A recipe written for this card wins outright, because it is the only one
        that measured the machine the workload will run on. With nothing for this
        card the answer is the *lowest* published value across the model's other
        aggregated recipes, which is only defensible for the ceiling-shaped flags
        `ADOPTED_FLAGS` admits: too low costs throughput and too high is a crash
        loop, so the direction to be wrong in is down. A value that is not an
        integer is skipped, not compared as text.
        """
        published: list[tuple[int, str]] = []
        for deployment in self.aggregated(engine):
            raw = (deployment.args.get(name) or "").strip()
            if not raw:
                continue
            try:
                number = int(raw)
            except ValueError:
                continue
            if hardware and deployment.hardware == hardware:
                return raw
            published.append((number, raw))
        if not published:
            return None
        return min(published)[1]

    def engine_defaults(self, engine: str, device_class: str) -> dict[str, str]:
        """The adopted flags this recipe speaks for, keyed the way `engine_args` is.

        Empty for a model with no aggregated recipe for this engine, for a recipe
        that names none of the adopted flags, and for an engine the corpus does not
        cover: all three of which leave the workload exactly as it was before this
        existed.
        """
        hardware = hardware_key(device_class)
        defaults: dict[str, str] = {}
        for name in ADOPTED_FLAGS:
            value = self.flag(name, engine, hardware)
            if value is not None:
                defaults[name] = value
        return defaults


def hardware_key(device_class: str) -> str:
    """A device class as the token a Dynamo recipe directory names itself after.

    `h200-141gb-sxm` is `h200` and `gb200-186gb-nvl` is `gb200`. The two dictionaries
    were written by different people for different purposes and agree only on the
    leading token, so that is the whole of the match: a class whose first segment
    names no recipe simply has no hardware-specific answer, which is a miss and not
    an error.
    """
    return (device_class or "").strip().lower().split("-", 1)[0]


class DynamoCorpus:
    """The snapshot, indexed by slug. Empty is a working state.

    Coverage is a couple of dozen models, the ones NVIDIA benchmarks, so a miss
    is the ordinary case and never an error.
    """

    def __init__(self, document: dict[str, Any] | None = None) -> None:
        document = document or {}
        self.source: str = str(document.get("source") or "")
        self.ref: str = str(document.get("ref") or "")
        self.captured_at: str = str(document.get("captured_at") or "")
        self._recipes: dict[str, Recipe] = {
            slug: _recipe(slug, spec) for slug, spec in (document.get("recipes") or {}).items()
        }
        self._index: dict[str, str] = {key: value for key, value in (document.get("index") or {}).items()}

    def __len__(self) -> int:
        return len(self._recipes)

    def recipes(self) -> tuple[Recipe, ...]:
        """Every recipe in the snapshot, for callers auditing the corpus itself."""
        return tuple(self._recipes.values())

    def lookup(self, slug: str) -> RecipeMatch | None:
        """The recipe for a HuggingFace slug, or None.

        Case-insensitive, because the Hub is.
        """
        if not slug:
            return None
        found = self._index.get(slug.strip().strip("/").lower())
        recipe = self._recipes.get(found or "")
        if recipe is None:
            return None
        return RecipeMatch(recipe=recipe, source=self.source, ref=self.ref)


def _recipe(slug: str, spec: dict[str, Any]) -> Recipe:
    return Recipe(
        slug=slug,
        model_id=spec.get("model_id") or slug,
        deployments=tuple(
            Deployment(
                path=entry.get("path") or "",
                backend=entry.get("backend") or "",
                mode=entry.get("mode") or "",
                role=entry.get("role") or "",
                hardware=entry.get("hardware") or "",
                args={str(key): str(value) for key, value in (entry.get("args") or {}).items()},
            )
            for entry in spec.get("deployments") or ()
        ),
    )


def load(path: Path | None = None) -> DynamoCorpus:
    """Reads the snapshot. A missing or broken file is not fatal."""
    source = path or SNAPSHOT_PATH
    try:
        document = json.loads(source.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("dynamo_recipes.snapshot_unreadable path=%s error=%s", source, exc)
        return DynamoCorpus()
    return DynamoCorpus(document)


_corpus: DynamoCorpus | None = None


def corpus() -> DynamoCorpus:
    """The process-wide corpus, parsed once."""
    global _corpus
    if _corpus is None:
        _corpus = load()
    return _corpus


def lookup(slug: str) -> RecipeMatch | None:
    """Convenience for the common call: one slug against the process-wide corpus."""
    return corpus().lookup(slug)


def engine_defaults(slug: str, engine: str, device_class: str) -> dict[str, str]:
    """The adopted engine arguments for one model on one card, or an empty map.

    The one call the serving path makes. Empty is the answer for every model the
    corpus does not cover, which is nearly all of them.
    """
    match = lookup(slug)
    if match is None:
        return {}
    return match.engine_defaults(engine, device_class)
