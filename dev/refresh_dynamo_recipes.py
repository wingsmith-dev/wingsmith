#!/usr/bin/env python3
"""Snapshot NVIDIA's Dynamo deployment recipes into the table the plane reads.

Run with `just dynamo-recipes`, which supplies PyYAML, on the same argument as
`refresh_vllm_recipes.py`: the plane parses JSON, never speaks YAML, and never
reaches GitHub on a request path.

**Why a second corpus.** `vllm-project/recipes` publishes the flags of
`vllm serve`. Our clusters do not run `vllm serve`: Dynamo runs
`python3 -m dynamo.vllm` for the worker and `python3 -m dynamo.frontend` for the
OpenAI surface, and those are two other programs with two other CLIs. A flag
lifted from the vLLM corpus is a guess about what our worker accepts;
`ai-dynamo/dynamo` `recipes/<model>/<backend>/<shape>/deploy.yaml` is the same
vendor publishing the exact command line they run against the exact program we
run. Where the two disagree, this one is closer to the process.

What is captured is the whole argument map of every `python3 -m dynamo.*`
worker launch, plus enough of the deployment's shape to say which one applies:
the backend, whether the recipe is aggregated or disaggregated, which role the
component plays, and which card it was written for. The plane filters that down
to the handful of arguments it is willing to adopt; keeping the rest means
widening the allow-list is a code change and not a re-capture.

Nothing here is a fact about our clusters. A recipe for a GB200 rack is evidence
about GB200 racks.
"""

from __future__ import annotations

import io
import re
import sys
import json
import shlex
import tarfile
import urllib.request

from typing import Any
from pathlib import Path
from datetime import UTC
from datetime import datetime

import yaml


REPO = "ai-dynamo/dynamo"
SOURCE = f"https://github.com/{REPO}"
OUT = Path(__file__).resolve().parents[1] / "libs/plane/app/integrations/data/dynamo-recipes.json"

_ROOT = "recipes/"

_BACKENDS = ("vllm", "sglang", "trtllm")

_HARDWARE_TOKENS = (
    "gb300",
    "gb200",
    "b300",
    "b200",
    "h200",
    "h100",
    "a100",
    "l40s",
    "mi355x",
    "mi325x",
    "mi300x",
)
"""Card tokens a recipe directory names itself after, longest first.

Longest first because `gb200` contains `b200`: matching the short one first would
file every GB200 rack recipe under the wrong card.
"""

_LAUNCH = re.compile(r"dynamo\.(" + "|".join(_BACKENDS) + r")\b")

_SHELL = re.compile(r"(?:^|/)(?:ba|z|da)?sh$")

_PLACEHOLDER = re.compile(r"^\$\{?(\w+)\}?$")


def _resolve_ref(ref: str) -> str:
    """The commit sha a branch or tag names right now.

    The snapshot records a sha and never a branch: `main` moves, and a table that
    cannot say which corpus it came from cannot be diffed against the next one.
    """
    url = f"https://api.github.com/repos/{REPO}/commits/{ref}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)["sha"]


def _fetch(sha: str) -> dict[str, str]:
    """Every `recipes/**/deploy.yaml` at one commit, as text keyed by repo path.

    Text, not parsed YAML, because the interesting part of these files is a
    shell script inside a block scalar and the parse happens per document below.
    """
    url = f"https://codeload.github.com/{REPO}/tar.gz/{sha}"
    with urllib.request.urlopen(url, timeout=300) as response:
        blob = response.read()
    files: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            path = member.name.split("/", 1)[1]
            if not path.startswith(_ROOT) or not path.endswith("deploy.yaml"):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            files[path] = handle.read().decode("utf-8", "replace")
    return files


def _tokens(container: dict[str, Any]) -> list[str] | None:
    """One container's `dynamo.<backend>` invocation, as argv.

    Two shapes, because the recipes write both. A container whose `command` is a
    shell runs a script in `args`, where the launch is the last line and carries
    backslash continuations; a container that execs `python3` directly has the
    whole invocation already tokenised across `command` and `args`, and running
    that through a shell lexer would strip the quotes off the JSON blobs several
    of these flags take.

    `None` when the container starts something that is not a Dynamo worker.
    """
    command = [str(part) for part in container.get("command") or []]
    args = [str(part) for part in container.get("args") or []]
    if command and _SHELL.search(command[0]):
        script = "\n".join(args)
        match = _LAUNCH.search(script)
        if match is None:
            return None
        lines: list[str] = []
        for line in script[match.start() :].split("\n"):
            lines.append(line.rstrip().rstrip("\\"))
            if not line.rstrip().endswith("\\"):
                break
        joined = " ".join(part.strip() for part in lines)
        try:
            return shlex.split(joined)
        except ValueError:
            return joined.split()
    argv = command + args
    if _LAUNCH.search(" ".join(argv)) is None:
        return None
    return argv


def _expand(value: str, env: dict[str, str]) -> str:
    """`${VAR}` resolved against the pod's environment, or left exactly as written.

    Left as written when nothing defines it: an unresolved placeholder shows plainly
    that it is unresolved, where dropping it would make a recipe look as though
    it never set the flag.
    """
    placeholder = _PLACEHOLDER.match(value)
    return env.get(placeholder.group(1), value) if placeholder else value


def _args(tokens: list[str], env: dict[str, str]) -> dict[str, str]:
    """An argv as `{flag without dashes: value}`.

    Last occurrence wins, which is how the engine reads its own argv. A flag with
    no value after it is a valueless switch and maps to the empty string, which is
    also how `engine_args` spells one.

    `--flag=value` and `--flag value` are one form here. Several recipes write the
    first, and reading only the second files the whole argument under a key nobody
    looks up: a silent miss instead of a parse error.
    """
    out: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            index += 1
            continue
        key = token[2:]
        if "=" in key:
            key, _, value = key.partition("=")
            out[key] = _expand(value, env)
            index += 1
            continue
        if index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
            out[key] = _expand(tokens[index + 1], env)
            index += 2
            continue
        out[key] = ""
        index += 1
    return out


def _containers(document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any], Any]]:
    """`(name, type, container, component environment)` for every container in a graph.

    Two CRD shapes, both current in the corpus. `spec.components` is a list of
    components each holding a `podTemplate`, and `spec.services` is the older map
    of one component per key, holding `extraPodSpec.mainContainer` and its
    environment under `envs`. Reading only the first loses a third of the corpus
    without erroring: the file parses, matches nothing, and the model silently has
    no recipe.
    """
    spec = document.get("spec") or {}
    out: list[tuple[str, str, dict[str, Any], Any]] = []

    components = spec.get("components") or []
    if isinstance(components, dict):
        components = [{"name": name, **(value or {})} for name, value in components.items()]
    for component in components:
        if not isinstance(component, dict):
            continue
        pod = ((component.get("podTemplate") or {}).get("spec")) or {}
        for container in pod.get("containers") or []:
            if isinstance(container, dict):
                out.append((str(component.get("name") or ""), str(component.get("type") or ""), container, None))

    for name, service in (spec.get("services") or {}).items():
        if not isinstance(service, dict):
            continue
        container = ((service.get("extraPodSpec") or {}).get("mainContainer")) or {}
        if container:
            out.append((str(name), str(service.get("componentType") or ""), container, service.get("envs")))

    return out


def _env(*blocks: Any) -> dict[str, str]:
    """The literal environment a container sees, graph-level `spec.env` included.

    Later blocks win, so a container's own entry overrides the graph's. Anything
    behind a `valueFrom` is skipped, not guessed at: it resolves out of a
    ConfigMap or a Secret this snapshot does not read, and inventing a value for it
    would put a made-up model id on a recipe.
    """
    out: dict[str, str] = {}
    for block in blocks:
        for entry in block or []:
            if isinstance(entry, dict) and entry.get("value") is not None:
                out[str(entry.get("name"))] = str(entry.get("value"))
    return out


def _hardware(path: str, document: dict[str, Any]) -> str:
    """The card a recipe was written for, from its directory name or its node selector.

    The directory is the primary source because it is what the recipe calls itself,
    and the node selector is the fallback for the few that do not say. Empty when
    neither does, which reads as "any card" and is the least specific evidence there
    is.
    """
    lowered = path.lower()
    for token in _HARDWARE_TOKENS:
        if token in lowered:
            return token
    products = " ".join(re.findall(r"nvidia\.com/gpu\.product[^\n]*\n?(?:[^\n]*\n)?", yaml.safe_dump(document)))
    normalized = products.lower().replace("-", "").replace(" ", "")
    for token in _HARDWARE_TOKENS:
        if token in normalized:
            return token
    return ""


def _mode(path: str) -> str:
    """`disagg` when the recipe splits prefill from decode, `agg` otherwise.

    Read off the path, which is where these recipes state it. It matters because
    the operator renders one worker and no prefill/decode split, so a disaggregated
    recipe's numbers were measured on a topology we do not run.
    """
    return "disagg" if "disagg" in path.lower() else "agg"


def _slug(args: dict[str, str], env: dict[str, str]) -> str:
    """The HuggingFace repo a component serves.

    `--served-model-name` first, because that is the id a client sends and the id
    the plane keys an artifact by. `--model` is the fallback, and both are dropped
    when they name a filesystem path: several recipes mount a pre-populated cache
    and pass a snapshot directory, which names no repo.
    """
    candidates = (
        args.get("served-model-name"),
        env.get("SERVED_MODEL_NAME"),
        env.get("MODEL_NAME"),
        args.get("model"),
    )
    for candidate in candidates:
        text = (candidate or "").strip()
        if text and "/" in text and not text.startswith(("/", "$", ".")):
            return text
    return ""


def _deployments(path: str, text: str) -> list[dict[str, Any]]:
    """Every Dynamo worker launch in one `deploy.yaml`, with the shape it runs in.

    Frontends are dropped. `dynamo.frontend` is a different program with a
    different CLI, and its flags are not engine arguments by any reading.
    """
    out: list[dict[str, Any]] = []
    try:
        documents = [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]
    except yaml.YAMLError:
        return out
    for document in documents:
        if document.get("kind") != "DynamoGraphDeployment":
            continue
        hardware = _hardware(path, document)
        for name, kind, container, component_env in _containers(document):
            if kind == "frontend" or name.lower() == "frontend":
                continue
            tokens = _tokens(container)
            if tokens is None:
                continue
            env = _env((document.get("spec") or {}).get("env"), component_env, container.get("env"))
            args = _args(tokens, env)
            out.append(
                {
                    "path": path,
                    "backend": _LAUNCH.search(" ".join(tokens)).group(1),
                    "mode": _mode(path),
                    "role": (kind or name).lower(),
                    "hardware": hardware,
                    "slug": _slug(args, env),
                    "args": args,
                }
            )
    return out


def build(ref: str = "main") -> dict[str, Any]:
    sha = _resolve_ref(ref)
    files = _fetch(sha)

    recipes: dict[str, dict[str, Any]] = {}
    for path, text in sorted(files.items()):
        for deployment in _deployments(path, text):
            slug = deployment.pop("slug")
            if not slug:
                continue
            entry = recipes.setdefault(slug, {"model_id": slug, "deployments": []})
            entry["deployments"].append(deployment)

    return {
        "source": SOURCE,
        "ref": sha,
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "recipes": dict(sorted(recipes.items())),
        "index": {slug.lower(): slug for slug in sorted(recipes)},
    }


def main() -> int:
    ref = sys.argv[1] if len(sys.argv) > 1 else "main"
    snapshot = build(ref)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=2, sort_keys=False) + "\n")
    size = OUT.stat().st_size / 1024
    count = sum(len(entry["deployments"]) for entry in snapshot["recipes"].values())
    print(
        f"{len(snapshot['recipes'])} models, {count} worker launches -> "
        f"{OUT} ({size:.0f} KiB) at {snapshot['ref'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
