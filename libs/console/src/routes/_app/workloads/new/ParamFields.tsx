import { FormGrid, NumberField, TextField } from "@/components/layout/form"
import { Badge } from "@/components/ui/badge"
import {
  servingProvenance,
  type SeededFrom,
  type ServingOverrides,
  type ServingProposal,
} from "@/modules/demand/deploy"
import {
  REASONING_PARSERS,
  TOOL_CALL_PARSERS,
  type Artifact,
  type ArtifactFit,
  type UpstreamServing,
  type Workload,
  type WorkloadServingRequest,
} from "@/modules/demand/types"
import { Link } from "@tanstack/react-router"
import { SuggestField } from "@/components/demand/suggest-field"

/**
 * What will be written under the shape, and where each number came from.
 *
 * The complaint this flow exists to answer is that defining a serving
 * configuration assumes the operator knows the right parameters, and they do not.
 * So the answer is filled in and then attributed: `weights` is the plane's own
 * arithmetic over `config.json`, `upstream` is a third party's tested
 * configuration with a permalink behind it, and anything unlabeled came off the
 * fitment row. A field nobody touches is still right, and it can say why.
 *
 * The identical-configuration line is informational and resolves nothing. Two
 * workloads with one configuration carry one `serving_digest`, which is what
 * reuse means now; whether a second one should exist is not this form's question.
 */
const SEEDED_LABEL: Record<SeededFrom, string> = {
  derived: "from the fit",
  weights: "from the weights",
  upstream: "from vLLM",
  publisher: "from the publisher",
}

/** Fields that carry their own attribution, so the badge row does not repeat it. */
const FIELD_ATTRIBUTED = new Set([
  "engine",
  "engine_runtime_version",
  "dtype",
  "kv_cache_dtype",
  "reasoning_parser",
  "tool_call_parser",
])

/** `dtype · from the weights`: a field's hint, with where its value came from. */
function sourced(hint: string, from?: SeededFrom): string {
  return from ? `${hint} · ${SEEDED_LABEL[from]}` : hint
}

/**
 * The proposed serving block, as fields somebody can disagree with.
 *
 * The flow exists because nobody should have to know the right parameters, and it was
 * read as nobody being *allowed* to change them: every value was filled in, attributed,
 * and then rendered as a badge. Attribution without an input is a claim you cannot act
 * on, so the inputs are here and the attribution moved under them.
 *
 * Only what something actually proposes. `max_batch_tokens`, `kv_cache_fraction` and
 * the resource request are still absent, because nothing here has a sound value for
 * them and a blank number box invites a guess where the plane's default is better. They
 * are on the workload the moment it exists.
 */
export function ParamFields({
  request,
  proposal,
  artifact,
  overrides,
  onChange,
}: {
  request: WorkloadServingRequest
  proposal: ServingProposal
  artifact: Artifact | null
  overrides: ServingOverrides
  onChange: (patch: ServingOverrides) => void
}) {
  const at = servingProvenance(proposal, artifact?.upstream, overrides)
  const floor = proposal.upstream?.engine_version_min

  return (
    <FormGrid>
      <TextField
        label="Engine"
        value={request.engine}
        onChange={(engine) => onChange({ engine })}
        hint={sourced(
          "Which engine the worker runs. It picks the module and the flag names, not a version.",
          at.engine
        )}
      />
      <TextField
        label="Engine runtime version"
        value={request.engine_runtime_version}
        onChange={(engine_runtime_version) =>
          onChange({ engine_runtime_version })
        }
        placeholder="latest"
        hint={
          "A Dynamo runtime tag, such as 1.4.1, never the engine's own version. `latest` means whatever the plane ships." +
          (floor
            ? ` The recipe needs vLLM ${floor} or newer; nothing records which vLLM a runtime carries, so that is yours to check.`
            : "")
        }
      />
      <TextField
        label="Engine runtime image"
        value={request.engine_runtime_image ?? ""}
        onChange={(engine_runtime_image) => onChange({ engine_runtime_image })}
        placeholder="Resolved from the engine and the runtime version"
        hint="Leave empty unless upstream says the derived runtime cannot serve these weights. The same image fetches the weights, so a wrong one is a download that never starts."
      />
      <TextField
        label="Frontend image"
        value={request.frontend_image ?? ""}
        onChange={(frontend_image) => onChange({ frontend_image })}
        placeholder="Whatever the cluster runs its frontends on"
        hint="A second image and not a spelling of the one above: the worker loads the weights, the frontend tokenizes the request, and they are separately versioned builds. Empty means the cluster decides, not that it reuses the engine runtime image."
      />
      <NumberField
        label="Context length"
        value={request.context_length || null}
        onChange={(context_length) =>
          onChange({ context_length: context_length ?? 0 })
        }
        min={0}
        placeholder="What the fit costed"
        hint={sourced(
          "Tokens one sequence may reach. The fit halves this down until its sizing is happy, so a window shorter than the model declares is an estimate's opinion and not the model's. Raise it back if you disagree.",
          at.context_length
        )}
      />
      <NumberField
        label="Per-device memory floor (MiB)"
        value={request.shape.device_memory_mib_min || null}
        onChange={(mib) => onChange({ device_memory_mib_min: mib ?? 0 })}
        min={0}
        placeholder="No floor: the device class already names the memory"
        hint="Filters devices within the class the fit chose, so it is empty by default: a class is itself a memory figure, and a number derived from one here is a floor that describes a shape nobody asked for. State one only to exclude a card of this class that a cluster reports as smaller than the catalog says."
      />
      <TextField
        label="dtype"
        value={request.dtype ?? ""}
        onChange={(dtype) => onChange({ dtype })}
        placeholder="bfloat16"
        hint={sourced("What the weights load as.", at.dtype)}
      />
      <TextField
        label="KV cache dtype"
        value={request.cache?.kv_cache_dtype ?? ""}
        onChange={(kv_cache_dtype) => onChange({ kv_cache_dtype })}
        placeholder="auto"
        hint={sourced(
          "What the cache stores at, which is not always what the weights load as.",
          at.kv_cache_dtype
        )}
      />
      <SuggestField
        label="Reasoning parser"
        value={request.serving?.reasoning.parser ?? null}
        onChange={(parser) => onChange({ reasoning_parser: parser ?? "" })}
        options={REASONING_PARSERS}
        hint={sourced(
          "What separates thinking from the answer. Setting it claims the capability the plane requires under it.",
          at.reasoning_parser
        )}
      />
      <SuggestField
        label="Tool call parser"
        value={request.serving?.tool_call_parser ?? null}
        onChange={(parser) => onChange({ tool_call_parser: parser ?? "" })}
        options={TOOL_CALL_PARSERS}
        hint={sourced(
          "In practice this is what makes tool calling work. Setting it claims tool calling too, because the plane refuses one half.",
          at.tool_call_parser
        )}
      />
    </FormGrid>
  )
}

/**
 * The sizing's objection to the shape somebody picked, said once and not enforced.
 *
 * Every fitment row is selectable now, including the ones the estimate dislikes, because
 * an estimate that refuses is worse than an estimate that warns: the cost of being wrong
 * in one direction is a worker that OOMs and reports it, and in the other it is a model
 * that cannot be deployed at all with nothing to argue against. This is where the
 * argument goes, so choosing such a row is a decision somebody made, not one they did
 * not notice.
 *
 * What the plane still refuses on its own is the certain half (weights that cannot sit
 * on the cards, heads that do not divide across the tensor group), and those come back
 * as a submit error, which is the right place for a refusal nothing here can talk out of.
 */
export function SizingDoubt({ fit }: { fit: ArtifactFit }) {
  return (
    <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
      <p className="font-mono text-2xs tracking-wider text-warning uppercase">
        The sizing does not think this fits
      </p>
      <p className="mt-1 font-mono text-2xs text-muted-foreground">
        {fit.reason}
      </p>
      <p className="mt-1.5 font-mono text-2xs text-muted-foreground">
        It is an estimate and you can overrule it: engines pack a cache better
        than arithmetic predicts, and interleaved attention makes the KV figure
        a ceiling, not a cost. If it turns out to be right the worker fails to
        start and says so, which is recoverable in a way an undeployable model
        is not.
      </p>
    </div>
  )
}

export function Proposed({
  artifact,
  proposal,
  loading,
  identical,
  overrides,
}: {
  artifact: Artifact | null
  proposal: ServingProposal
  loading: boolean
  identical: Workload[]
  overrides: ServingOverrides
}) {
  const upstream = proposal.upstream ?? null
  const at = servingProvenance(proposal, artifact?.upstream, overrides)
  // Only what has no field of its own below. Everything else is attributed under the
  // input it filled, where the attribution sits beside the value it explains rather
  // than in a list the reader has to match up by name.
  const seeded = Object.entries(at).filter(
    ([field, from]) => from !== "derived" && !FIELD_ATTRIBUTED.has(field)
  )

  return (
    <div className="space-y-2 rounded-lg border bg-muted/20 p-3">
      {loading && (
        <p className="font-mono text-2xs text-muted-foreground">
          reading what these weights imply…
        </p>
      )}

      {!loading && seeded.length === 0 && (
        <p className="font-mono text-2xs text-muted-foreground">
          Nothing is proposed for these weights beyond the fit. Every parameter
          under the shape is the plane's own default, and all of them are
          editable on the workload afterwards.
        </p>
      )}

      {seeded.length > 0 && (
        <div className="space-y-1">
          <p className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
            Filled in for you
          </p>
          <div className="flex flex-wrap gap-1.5">
            {seeded.map(([field, from]) => (
              <Badge
                key={field}
                variant="outline"
                className="font-mono text-2xs"
              >
                {field.replace(/_/g, " ")}
                <span className="ml-1 text-muted-foreground">
                  {SEEDED_LABEL[from]}
                </span>
              </Badge>
            ))}
          </div>
        </div>
      )}

      {upstream?.docker_image && <OwnBuildWarning upstream={upstream} />}

      {upstream && <UpstreamClaim upstream={upstream} />}

      {identical.length > 0 && (
        <p className="border-t pt-2 font-mono text-2xs text-muted-foreground">
          {identical.length} other workload
          {identical.length === 1 ? "" : "s"} run this exact configuration:{" "}
          {identical.slice(0, 4).map((workload, index) => (
            <span key={workload.id}>
              {index > 0 && ", "}
              <Link
                to="/workloads/$workloadId"
                params={{ workloadId: workload.id }}
                className="underline hover:text-foreground"
              >
                {workload.name}
              </Link>
            </span>
          ))}
          {identical.length > 4 && ` and ${identical.length - 4} more`}. Nothing
          stops a second one, since they carry the same serving digest.
        </p>
      )}
    </div>
  )
}

/**
 * Upstream serves this model from a build of its own, so the derived one will not.
 *
 * The one claim in the corpus that is a warning instead of a value. Every other
 * upstream field seeds a form input; this one cannot, because the image it names
 * is a third party's runtime with no `ai-dynamo` in it -- pinning `engine_runtime_image`
 * to it swaps a Transformers "does not recognize this architecture" for a missing
 * `dynamo.vllm` module, which is no closer to serving.
 *
 * What it is worth is the negative: a model whose recipe names its own image is
 * one the plane's `<engine>-runtime:<engine_runtime_version>` cannot load, and the failure lands
 * two minutes into a pod where nobody is looking for it. GLM-5.3-Flash is the
 * case -- `glm5_next` exists only in Z-AI's vLLM, three minor versions ahead of
 * what every released Dynamo runtime carries.
 */
function OwnBuildWarning({ upstream }: { upstream: UpstreamServing }) {
  return (
    <p className="rounded border border-amber-500/40 bg-amber-500/5 p-2 font-mono text-2xs text-warning">
      {upstream.source} serves {upstream.model_id} from{" "}
      <span className="font-semibold">{upstream.docker_image}</span>, a build of
      its own. The image the plane derives from the engine and the runtime
      version will not load these weights. Set an engine runtime image below
      that carries both this engine build and the Dynamo runtime. That address
      is not one, because it has no{" "}
      <span className="font-semibold">ai-dynamo</span> in it.
    </p>
  )
}

/**
 * Who ran this, and on what.
 *
 * `verified_hardware` is the most useful sentence on the page and also the most
 * easily misread: it says the configuration works on that node shape, and says
 * nothing at all about whether the fleet owns one.
 *
 * Two links, because they answer two questions. "Read it" goes to the corpus's own
 * branch: what upstream recommends now, which is what somebody following a link
 * from a form is asking. "As captured" is the permalink at the commit these values
 * were snapshotted from, and is the one to open when a value here and the file it
 * came from no longer agree.
 *
 * The vLLM floor is stated and never checked. It is the engine's version, and no
 * field anywhere records which engine build a Dynamo runtime image carries, so this
 * is a thing to weigh, not a thing the plane can enforce.
 */
function UpstreamClaim({ upstream }: { upstream: UpstreamServing }) {
  return (
    <p className="font-mono text-2xs text-muted-foreground">
      {upstream.source} publishes a tested configuration for {upstream.model_id}
      {upstream.verified_hardware.length > 0 &&
        `, verified on ${upstream.verified_hardware.join(", ")}`}
      .{" "}
      <a
        href={upstream.source_uri}
        target="_blank"
        rel="noreferrer"
        className="underline hover:text-foreground"
      >
        Read it
      </a>
      {upstream.snapshot_uri && (
        <>
          {" · "}
          <a
            href={upstream.snapshot_uri}
            target="_blank"
            rel="noreferrer"
            className="underline hover:text-foreground"
          >
            as captured
          </a>
        </>
      )}
      {upstream.engine_version_min &&
        ` · needs vLLM ${upstream.engine_version_min} or newer, which is the engine's version and not the runtime tag above`}
      {upstream.nightly_required && " · needs a nightly engine build"}
    </p>
  )
}
