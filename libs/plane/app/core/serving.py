from __future__ import annotations

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
"""


def served_model(recipe: Recipe, artifact: Artifact) -> str:
    """The `model_name` the engine reports itself under, most authoritative first.

    An explicit served-model-name wins because it is the operator saying so, and
    it wins on the command line too — `engine_args` renders after the derived
    arguments precisely so it can. `Recipe.name` comes next: it is the name the
    deployment is built and served under, and it is what the engine emits. The
    artifact is the last resort because it holds the repository a model is fetched
    from rather than the name it serves under — `google/gemma-4-31B-it` against a
    `model_name` of `gemma-4-31b-it` — so reaching it usually means matching no
    series at all.

    Here in `core/` rather than in a module because three of them ask the same
    question for different reasons: metrics and benchmarks narrow a series by it,
    and endpoints put it on the routing table so the gateway can rewrite an
    inbound `model` to it. It is machinery — the one place that knows how a
    recipe becomes the string an engine answers to — and a module that owned it
    would have the other two importing across the domain to reach it.
    """
    for key in SERVED_MODEL_ARGS:
        override = recipe.engine_args.get(key)
        if override:
            return str(override)
    return str(recipe.name or artifact.name)
