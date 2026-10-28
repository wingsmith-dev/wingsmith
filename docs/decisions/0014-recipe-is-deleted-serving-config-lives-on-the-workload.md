# 0014: Recipe is deleted; serving configuration lives on the workload, identified by a digest

**Status:** accepted · 2026-09-02

**Amends:** the founding object model (now [`docs/architecture`](../architecture/README.md)), which made
Recipe one of the seven objects of the minimal core, and answers the question it left open: whether a serving configuration is a catalog object or
a deployment-time choice. Also amends
[ADR 0010](0010-workload-is-the-global-umbrella.md), which moved `recipe_id` up onto the Workload;
the field it moved is now the fields themselves.

## Context

Recipe was a reproducible way to serve an artifact: engine and version, quantization, parallelism,
context length, cache configuration, engine args, and the device shape that falls out of them. It
was a declared object of its own, with its own name, version and lifecycle, its own row, its own
API surface, and its own frame on the wire. Two claims justified that separateness: a recipe is
**shared** by many workloads, and it **travels once** to a cluster that many deployments then refer
to. Neither survived contact with the code.

**Cross-cluster sharing saved nothing.**
[`libs/plane/app/ingestion/desired.py`](../../libs/plane/app/ingestion/desired.py) built each
cluster's demand stream by selecting the recipes reachable through the deployments *of that
cluster*, and sent them down that cluster's socket. A configuration used in three clusters was
serialised three times, exactly as three inlined copies would have been. The only arrangement that
saved a frame was two deployments in **one** cluster naming one recipe: two workloads over one
configuration, placed together. That is the canary case, and it is the only one.

**The separate frame had a price, and it was not small.** It bought an ordering rule on the demand
stream, a `RecipeNotDeclared` pending reason for a deployment whose recipe had not arrived, and a
resume race in which a reconnecting operator was sent a deployment but not the recipe it named, so
the deployment waited for a frame that was never coming. Three mechanisms to defend one saved frame
in the canary case.

**And the sharing it did offer was being reconstructed instead of used.** The console did not ask
which recipe to reuse; `matchExistingRecipe` compared eight fields of a candidate against every
existing recipe on the artifact to decide whether one of them was the same recipe. Comparing
contents to find an object is an admission that the contents *are* the object. Once that is said out
loud, the identity is a hash, not a row.

## Decision

1. **Recipe is deleted as an object.** `demand_recipe` is dropped and its columns move onto
   `demand_workload` with the same names and types. `/recipes` and its sub-resources are gone from
   the HTTP API, `wings recipe*` from the CLI, and the recipe routes from the console.
2. **The serving configuration reaches a cluster inlined on `deployment.desired.serving`.**
   `RecipeDesired` becomes `ServingSpec` and loses every identity and routing field it carried;
   `recipe.desired`, the ordering rule and `RecipeNotDeclared` are all removed with it.
   `ArtifactNotDeclared` stays: an artifact is genuinely a shared object with its own lifecycle,
   which is the test Recipe failed.
3. **Reuse is `serving_digest`.** A `svg_`-prefixed sha256 over canonical JSON of exactly the fields
   that decide what process runs: artifact, engine and image, args, quantization and dtype,
   parallelism, context, batch and cache settings, device shape, overlay. Two workloads with the
   same configuration have the same digest, and that is the whole mechanism. `imported_args`
   (observed, never rendered), `capabilities` (asserted, never rendered), `served_model_name`, labels
   and everything per-cluster are excluded: the digest answers *would this produce the same running
   process*, so only what reaches an engine is in it. It is computed by the plane on every write and
   never accepted from a client.
4. **The device shape still reaches the cluster on `placement`, not in the serving block.** It is in
   the digest because it decides what runs; it is not on the wire twice, because two copies of one
   fact on one wire are two chances to disagree.

## Consequences

- **Placement is unchanged in substance and shorter by a join.** It reads the device class, count
  and memory floor off the workload, not through it. The rule that the requirement is a
  device *class* and a memory floor, never a bare count, is untouched; it is now addressed to the
  workload, which is where it always had to end up once ADR 0010 made the workload the thing
  placement expands.
- **The three-level split becomes two, and the argument for it survives intact.** GLM-5.3-Flash at
  FP8/TP4/32k and the same weights at BF16/TP8/128k are still different hardware footprints, still
  different price points, still plausibly different clusters. They are now two Workloads over one
  Artifact, told apart by their `serving_digest`. Context length still determines the footprint and
  therefore still does not belong on a catalog entry.
- **Benchmarks anchor on something that cannot drift.** `bench_run` recorded `recipe_id` and
  `recipe_version` and the model already noted it denormalised them "because the recipe they name is
  mutable". A run now records `serving_digest`. A digest that no longer matches any workload is a
  configuration nobody runs any more, which is a true statement; a `recipe_id` that still resolves
  to a row someone has since edited is a false one.
- **The open question is answered: deployment-time configuration.** A serving configuration is not a
  sellable thing today, and nothing was willing to pay the ordering rule, the pending reason and the
  resume race for a catalog it did not have. If a named, versioned, sellable configuration is ever
  wanted ("GLM-5.3-Flash FP8 32k" with a price), it is a thin catalog object pointing at a
  `serving_digest`, and it is a decision nobody has had to make yet.
- **The real cost: `Recipe.name`, `Recipe.version` and `Recipe.lifecycle` are gone and are not
  replaced.** A workload has a name; a serving configuration no longer has one of its own, so
  "deploy the recipe a colleague published" is unsayable and the draft/published/deprecated gate on a
  configuration is gone with it. Recipe revisions (`GET /v1/recipes/{ref}/revisions`, which read
  earlier versions of a name as ordinary rows) go with the uniqueness constraint that made them
  possible. This is the part to revisit first if the catalog above is ever wanted.
- **The console loses a fork instead of a feature.** `matchExistingRecipe` and `footprintOfRecipe`
  are deleted outright and the create flow no longer asks "existing recipe or new footprint"; it
  always fills in a serving block, seeded from the artifact's proposed defaults, which is what
  `RecipeDefaults` already computed under a different name.
- **A workload is now the unit of change for a running process.** Editing the serving configuration
  of one workload cannot disturb another, which under a shared recipe it could and did. The
  symmetric loss is that fixing an engine arg across ten workloads is ten writes.
