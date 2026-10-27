#!/usr/bin/env python3
"""Snapshot the vLLM recipe corpus into the table the plane reads.

Run with `just vllm-recipes`, which supplies PyYAML. The plane itself parses
JSON and never speaks YAML, and it never reaches GitHub to register an
artifact: a cold network there would make model registration fail for a reason
that has nothing to do with the model. So the corpus is captured here, pinned
to one commit, and committed to the tree like `contract/devices.json`.

What is dropped is as deliberate as what is kept. `guide` is prose for a human
reading vllm.ai and is most of the bytes; the plane has no surface that shows
it. Everything kept is something the plane can either render into an engine
flag or weigh when it sizes a recipe.
"""

from __future__ import annotations

import io
import sys
import json
import tarfile
import urllib.request

from typing import Any
from pathlib import Path
from datetime import UTC
from datetime import datetime

import yaml


REPO = "vllm-project/recipes"
SOURCE = f"https://github.com/{REPO}"
OUT = Path(__file__).resolve().parents[1] / "libs/plane/app/integrations/data/vllm-recipes.json"

_MODEL_KEYS = (
    "model_id",
    "min_vllm_version",
    "nightly_required",
    "architecture",
    "parameter_count",
    "active_parameters",
    "context_length",
    "base_args",
    "base_env",
    "docker_image",
)
_VARIANT_KEYS = (
    "model_id",
    "label",
    "precision",
    "vram_minimum_gb",
    "tp",
    "extra_args",
    "extra_env",
    "min_vllm_version",
    "supported_hardware",
    "hardware_overrides",
)
_PROFILE_KEYS = ("brand", "generation", "display_name", "gpu_count", "vram_gb", "multi_node")


def _resolve_ref(ref: str) -> str:
    """The commit sha a branch or tag names right now.

    The snapshot records a sha and never a branch: `main` moves, and a table
    that cannot say which corpus it came from cannot be diffed against the next
    one.
    """
    url = f"https://api.github.com/repos/{REPO}/commits/{ref}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)["sha"]


def _fetch(sha: str) -> dict[str, Any]:
    """Every YAML file in the corpus at one commit, keyed by repo-relative path."""
    url = f"https://codeload.github.com/{REPO}/tar.gz/{sha}"
    with urllib.request.urlopen(url, timeout=120) as response:
        blob = response.read()
    files: dict[str, Any] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.endswith(".yaml"):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            path = member.name.split("/", 1)[1]
            files[path] = yaml.safe_load(handle.read())
    return files


def _pick(source: dict[str, Any] | None, keys: tuple[str, ...]) -> dict[str, Any]:
    source = source or {}
    return {key: source[key] for key in keys if source.get(key) is not None}


def _feature(spec: dict[str, Any]) -> dict[str, Any]:
    """One feature block, keeping the flags and the mode that selects them.

    `spec_decoding` and friends carry `modes`, each with its own args and its own
    default. Flattening them to the default's args would silently drop the
    alternatives, which are the interesting half: an Eagle3 draft head is a
    different deployment from MTP, not a different spelling of one.
    """
    kept = _pick(spec, ("description", "args", "default_mode"))
    modes = spec.get("modes")
    if isinstance(modes, dict):
        kept["modes"] = {name: _pick(mode, ("label", "description", "args")) for name, mode in modes.items()}
    return kept


def _recipe(path: str, doc: dict[str, Any]) -> dict[str, Any]:
    meta = doc.get("meta") or {}
    features = doc.get("features") or {}
    variants = doc.get("variants") or {}
    entry: dict[str, Any] = {
        "path": path,
        "title": meta.get("title"),
        "provider": meta.get("provider"),
        "tasks": meta.get("tasks") or [],
        "hardware": meta.get("hardware") or {},
        "model": _pick(doc.get("model"), _MODEL_KEYS),
        "dependencies": doc.get("dependencies") or [],
        "features": {name: _feature(spec or {}) for name, spec in features.items()},
        "opt_in_features": doc.get("opt_in_features") or [],
        "variants": {key: _pick(spec or {}, _VARIANT_KEYS) for key, spec in variants.items()},
        "compatible_strategies": doc.get("compatible_strategies") or [],
        "default_strategy": doc.get("default_strategy"),
        "strategy_overrides": doc.get("strategy_overrides") or {},
        "hardware_overrides": doc.get("hardware_overrides") or {},
        # `strategy_hardware` opts a strategy in or out per GPU and can name one that
        # `compatible_strategies` does not list at all; `strategy_min_gpus` is the GPU
        # floor a strategy needs, and its exact-GPU-id form is how the corpus says a model
        # fits two H200 nodes and needs four H100 ones. Both were dropped in the first
        # pass and both change the answer to "where can this run".
        "strategy_hardware": doc.get("strategy_hardware") or {},
        "strategy_min_gpus": doc.get("strategy_min_gpus"),
    }
    return {key: value for key, value in entry.items() if value not in (None, {}, [])}


def _strategy(doc: dict[str, Any]) -> dict[str, Any]:
    match = doc.get("hardware_match") or {}
    return {
        "deploy_type": doc.get("deploy_type"),
        "display_name": doc.get("display_name"),
        "orientation": doc.get("orientation"),
        "parallel_flag": doc.get("parallel_flag"),
        "vllm_args": doc.get("vllm_args") or [],
        "min_gpus": match.get("min_gpus"),
        "max_gpus": match.get("max_gpus"),
        "multi_node": match.get("multi_node"),
    }


def build(ref: str = "main") -> dict[str, Any]:
    sha = _resolve_ref(ref)
    files = _fetch(sha)

    recipes: dict[str, dict[str, Any]] = {}
    index: dict[str, dict[str, str]] = {}
    for path, doc in sorted(files.items()):
        if not path.startswith("models/") or not isinstance(doc, dict):
            continue
        slug = path[len("models/") : -len(".yaml")]
        entry = _recipe(path, doc)
        recipes[slug] = entry
        index[slug.lower()] = {"recipe": slug, "variant": "default"}
        for key, variant in entry.get("variants", {}).items():
            alias = variant.get("model_id")
            if isinstance(alias, str) and alias.lower() not in index:
                index[alias.lower()] = {"recipe": slug, "variant": key}

    taxonomy = files.get("taxonomy.yaml") or {}
    profiles = taxonomy.get("hardware_profiles") or {}
    strategies = {
        Path(path).stem: _strategy(doc)
        for path, doc in sorted(files.items())
        if path.startswith("strategies/") and isinstance(doc, dict)
    }

    return {
        "source": SOURCE,
        "ref": sha,
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "hardware_profiles": {name: _pick(spec, _PROFILE_KEYS) for name, spec in sorted(profiles.items())},
        "strategies": strategies,
        "recipes": recipes,
        "index": dict(sorted(index.items())),
    }


def main() -> int:
    ref = sys.argv[1] if len(sys.argv) > 1 else "main"
    snapshot = build(ref)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=2, sort_keys=False) + "\n")
    size = OUT.stat().st_size / 1024
    print(
        f"{len(snapshot['recipes'])} recipes, {len(snapshot['index'])} slugs, "
        f"{len(snapshot['hardware_profiles'])} hardware profiles, "
        f"{len(snapshot['strategies'])} strategies -> {OUT} ({size:.0f} KiB) at {snapshot['ref'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
