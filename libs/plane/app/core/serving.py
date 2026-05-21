from __future__ import annotations

from typing import Protocol

from app.db.models import Recipe
from app.db.models import Artifact


SERVED_MODEL_ARGS = ("served-model-name", "served_model_name")
"""The keys an override can arrive under, in the order the engine would see them.

`engine_args` holds flag names, so the hyphenated one is the real spelling: the
operator renders `--` + the key verbatim (`strings.TrimPrefix(key, "--")` in
`render/dynamo.go`) and vLLM's flag is `--served-model-name`. The underscore form
is accepted because this function looked for nothing else for a while, so it is
what anyone who read this code rather than the operator would have written — but
it does not survive the render, and a recipe carrying it gets an engine that
refuses to start on an unrecognised argument. Both are read here so that the
plane's answer never silently disagrees with the command line.

Matched against `flag_name`, never against the raw key. Real recipes store the
leading dashes — `{"--served-model-name": "moonshotai/kimi-k3"}` — because that is
what a person copying a command line writes, and the operator strips them before
rendering. Comparing the raw key to these names missed every one of those, which
is not a lookup that fails loudly: the plane fell through to the recipe's declared
name, published it on the routing table, and the gateway rewrote `model` to a
string the engine had never been told to answer to. Kimi K3 served every request
as a 404 from inside the cluster with the deployment healthy and the tunnel open.
"""


def flag_name(key: str) -> str:
    """An `engine_args` key as the operator will key it: without the leading dashes.

    The one normalisation, spelled once. `render/dynamo.go` does
    `strings.TrimPrefix(key, "--")` and renders `"--" + key`, so a recipe may hold
    `served-model-name` or `--served-model-name` and mean the same flag. Anything
    in the plane that reads a specific flag out of `engine_args` has to agree with
    that or it is reading a different dictionary than the engine was given.
    """
    return key.lstrip("-")


class HasServedModelName(Protocol):
    """A recipe or a workload, read only for its declared override.

    A structural type rather than a union because the two objects share nothing
    else and the tests fake both. Nothing here needs a row.
    """

    served_model_name: str | None


def derived_served_model(artifact: Artifact) -> str:
    """The lowercase Hugging Face repository slug the artifact names.

    `moonshotai/Kimi-K3` becomes `moonshotai/kimi-k3`. Lowercase because that is
    what a caller types and what every registry treats as canonical, and the
    organisation is kept because a bare `kimi-k3` is not a model id anybody can
    look up.

    The artifact's `name` is the slug when it holds one. An artifact adopted by
    watching a cluster serve it carries the reference in `digest` as
    `hf:org/model` instead, so that is read when the name is not a slug at all.
    """
    name = (artifact.name or "").strip()
    if "/" not in name:
        digest = (artifact.digest or "").strip()
        if digest.lower().startswith("hf:"):
            name = digest[3:].lstrip("/") or name
    return name.lower()


def served_model(recipe: Recipe, artifact: Artifact, workload: HasServedModelName | None = None) -> str:
    """The `model_name` the engine reports itself under, most authoritative first.

    `engine_args` first, because it wins on the command line — the operator
    renders the derived flags and then lays `engine_args` over them, so anything
    decided here that it contradicts is a lie about the running process. It is the
    escape hatch for a flag this plane has never heard of and an escape hatch that
    loses to a default is not one.

    Then the workload's declared `served_model_name`, then the recipe's. Both are
    explicit and both are declared; the workload wins because the workload is the
    object a person creates, and a recipe is shared by every workload that names
    it.

    Last, the artifact's lowercase Hugging Face slug. This used to be `Recipe.name`
    instead, which put the fit on the public API surface — `kimi-k3-mxfp4-tp8-1024k`
    — so re-fitting the same model at a different tensor degree renamed the model
    every client codes against. The slug is a fact about the weights and survives
    a re-fit.

    Here in `core/` rather than in a module because four of them ask the same
    question for different reasons: metrics and benchmarks narrow a series by it,
    endpoints put it on the routing table so the gateway can rewrite an inbound
    `model` to it, and the desired-state builder sends it to the cluster. It is
    machinery — the one place that knows how a recipe becomes the string an engine
    answers to — and a module that owned it would have the others importing across
    the domain to reach it.
    """
    args = {flag_name(key): value for key, value in (recipe.engine_args or {}).items()}
    for key in SERVED_MODEL_ARGS:
        override = args.get(key)
        if override:
            return str(override)
    return declared_served_model(recipe, artifact, workload)


def declared_served_model(recipe: Recipe, artifact: Artifact, workload: HasServedModelName | None = None) -> str:
    """`served_model` without the `engine_args` hatch, for what the plane declares.

    The wire carries this rather than the full answer: the operator lays
    `engine_args` over the derived flags itself, so sending the resolved hatch
    would only give the same value twice and a chance for the two to disagree.
    """
    if workload is not None and (workload.served_model_name or "").strip():
        return str(workload.served_model_name).strip()
    if (recipe.served_model_name or "").strip():
        return str(recipe.served_model_name).strip()
    return derived_served_model(artifact) or str(recipe.name)
