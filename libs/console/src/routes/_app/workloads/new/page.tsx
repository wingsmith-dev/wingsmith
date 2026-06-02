import { ArtifactFitmentPanel } from "@/components/demand/artifact-fitment"
import {
  FormGrid,
  NumberField,
  SelectField,
  TextField,
} from "@/components/layout/form"
import { Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { WriteAction } from "@/components/layout/write-action"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { formatError, shortenId } from "@/lib/format"
import { useProjectOptions } from "@/modules/binding/queries"
import {
  derivedServingRequest,
  requiresSingleDomain,
  derivedWorkloadName,
  footprintSummary,
  servingProvenance,
  workloadsMatching,
  type ServingProposal,
} from "@/modules/demand/deploy"
import {
  useArtifact,
  useArtifactServingDefaults,
  useCreateWorkload,
  useWorkloads,
} from "@/modules/demand/queries"
import type {
  Artifact,
  ArtifactFit,
  ServingFootprint,
  UpstreamServing,
  Workload,
} from "@/modules/demand/types"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"
import { useMemo, useState } from "react"
import { toast } from "sonner"
import { ArtifactPicker } from "./ArtifactPicker"
import type { NewWorkloadSearch } from "./route"
import { Step } from "./Step"
import { AUTO_WHERE, WhereStep, type WhereChoice } from "./WhereStep"

/** The six numbers a fitment row is, if the link carried all of them. */
function footprintFromSearch(
  search: NewWorkloadSearch
): ServingFootprint | null {
  if (!search.class || !search.devices) return null
  return {
    device_class: search.class,
    device_count: search.devices,
    tensor_parallel: search.tp ?? search.devices,
    pipeline_parallel: search.pp ?? 1,
    context_length: search.ctx ?? 0,
    quantization: search.quant ?? "",
  }
}

/**
 * The chosen shape, and the fitment row it came from.
 *
 * `fit` is null when the footprint arrived in the URL from an artifact page's Deploy
 * rather than from a row clicked here. The six numbers are the whole of the footprint,
 * but the fleet counts alongside them are not in the link, so the Where step says less.
 */
interface How {
  footprint: ServingFootprint
  fit: ArtifactFit | null
}

/**
 * Deploying something, as three questions and an answer.
 *
 * A page and not a dialog. The fitment table is the picker for question two and it is
 * six columns wide with three filters of its own; squeezed into a modal it becomes the
 * thing people click past. On a page it can be read, which is the point of having
 * derived it.
 *
 * The questions are what, how and how much. Where is deliberately not one of them — it
 * is rendered as placement's answer with an override hung off it, because a cluster
 * offered as an ordinary field is how rule 3 stops being true in practice.
 *
 * Kind is fixed to `service`. A task or a gang has a deadline, a completion count or
 * co-scheduled ranks to declare, none of which a fitment row implies, and the workload
 * form still exists for them.
 */
export function NewWorkloadPage() {
  const navigate = useNavigate()
  const search = useSearch({ from: "/_app/workloads/new" })
  const { data: projects } = useProjectOptions()
  const createWorkload = useCreateWorkload()

  /** An artifact named in the link. Resolved here so the picker can be skipped. */
  const linked = useArtifact(search.artifact ?? "")
  const linkedFootprint = useMemo(
    () => footprintFromSearch(search),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      search.class,
      search.devices,
      search.tp,
      search.pp,
      search.ctx,
      search.quant,
    ]
  )

  const [picked, setArtifact] = useState<Artifact | null>(null)
  const [how, setHow] = useState<How | null>(
    linkedFootprint ? { footprint: linkedFootprint, fit: null } : null
  )
  const [project, setProject] = useState("")
  const [replicas, setReplicas] = useState<number | null>(1)
  const [name, setName] = useState("")
  const [touchedName, setTouchedName] = useState(false)
  const [where, setWhere] = useState<WhereChoice>(AUTO_WHERE)
  const [open, setOpen] = useState(
    linkedFootprint ? 3 : search.artifact ? 2 : 1
  )

  const artifact = picked ?? linked.data ?? null
  /**
   * What the plane proposes for these weights, fetched the moment one is chosen.
   *
   * The whole point of the flow is that nobody should have to know the right
   * parameters. The fit answers the shape; this answers everything under it, and
   * `upstream` says who tested what — a field nobody touches is still right, and
   * it can say why.
   */
  const proposed = useArtifactServingDefaults(artifact)
  const proposal: ServingProposal = {
    defaults: proposed.data?.serving,
    upstream: proposed.data?.upstream,
  }
  /** Only to avoid a name the list can already see taken. The plane owns collisions. */
  const workloads = useWorkloads({})
  const taken = useMemo(
    () => (workloads.items ?? []).map((workload) => workload.name),
    [workloads.items]
  )
  /**
   * Workloads already running exactly this. Shown, never acted on: the digest the
   * plane computes is what makes two of them one configuration, and a second
   * workload on one configuration is a normal thing to want.
   */
  const identical =
    artifact && how
      ? workloadsMatching(workloads.items ?? [], artifact, how.footprint)
      : []

  const footprint = how?.footprint ?? null
  const fit = how?.fit ?? null
  /**
   * What the chosen shape says about fabric, before anyone overrides it. The box the
   * Where step starts on is what would actually be written.
   */
  const derivedFabric = how ? requiresSingleDomain(how.footprint) : false

  const suggestedName = artifact ? derivedWorkloadName(artifact, taken) : ""
  const effectiveName = touchedName ? name : suggestedName

  /**
   * Not asked when there is nothing to ask.
   *
   * The plane requires a project — quota is keyed (project, cluster, device class), so
   * there is no such thing as a workload without one — but a field with a single
   * possible value is not a question, it is a formality. One project answers itself and
   * the field does not render; several and it does, unselected, because silently
   * charging a guess to the wrong project's quota is worse than one more click.
   */
  const sole = projects?.length === 1 ? projects[0] : undefined
  const effectiveProject = project || sole?.id || ""

  const pending = createWorkload.isPending
  const ready =
    !!artifact &&
    !!how &&
    !!effectiveProject &&
    !!effectiveName &&
    (replicas ?? 0) > 0 &&
    (where.mode !== "pin" || !!where.pinned)

  const pickArtifact = (picked: Artifact) => {
    setArtifact(picked)
    setHow(null)
    setWhere(AUTO_WHERE)
    setOpen(2)
  }

  /**
   * Picking a class is not the whole of step two when that class serves at more than
   * one count. The step stays open on it so the counts are visible where the choice is
   * made, and closes when the count is picked — collapsing on the class alone is what
   * made the recommendation look like the only shape on offer.
   */
  const pickHow = (choice: How) => {
    const refining =
      how?.footprint.device_class === choice.footprint.device_class
    const counts = choice.fit?.layouts.length ?? 1
    setHow(choice)
    setOpen(refining || counts <= 1 ? 3 : 2)
  }

  /**
   * One call, carrying the serving block the fit implies.
   *
   * Nothing is deduplicated here and nothing needs to be: two workloads configured
   * the same way carry the same `serving_digest`, which the plane computes, and that
   * is the whole of what recipe reuse ever bought.
   */
  const submit = async () => {
    if (!artifact || !how) return
    const cluster = where.mode === "pin" ? where.pinned : null

    const derived = derivedServingRequest(artifact, how.footprint, proposal)

    try {
      const workload = await createWorkload.mutateAsync({
        serving: {
          ...derived,
          shape: {
            ...derived.shape,
            // Inside the shape because that is what carries it: the fit derives
            // the answer and the Where step is where somebody disagrees.
            requires_single_topology_domain:
              where.fabric ?? derived.shape.requires_single_topology_domain,
          },
        },
        name: effectiveName,
        project: effectiveProject,
        kind: "service",
        cluster,
        // The plane turns either list into a Policy attached to the workload. Sent only
        // for the mode that means them, so switching modes cannot leave one behind.
        allowed_regions: where.mode === "region" ? where.regions : [],
        allowed_clusters: where.mode === "clusters" ? where.allowed : [],
        // Absent on purpose: every workload carries one and the project's default
        // answers it. Repeating it here is how defaults drift.
        min_scale: replicas ?? 1,
        max_scale: replicas ?? 1,
        desired_scale: replicas ?? 1,
      })
      toast.success("Workload created", { description: effectiveName })
      navigate({
        to: "/workloads/$workloadId",
        params: { workloadId: workload.id },
      })
    } catch (error) {
      toast.error(formatError(error, "Could not deploy"))
    }
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        title="New workload"
        description="declared · what to run, how to serve it, and how much of it"
        readActions={
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigate({ to: "/workloads" })}
          >
            Cancel
          </Button>
        }
      />

      <div className="mx-auto w-full max-w-4xl space-y-3 p-4">
        <Step
          index={1}
          title="What to run"
          hint="The weights. Everything below is derived from them."
          open={open === 1}
          onReopen={() => setOpen(1)}
          answer={
            artifact && (
              <div className="flex items-center gap-2">
                <span className="font-mono text-xs">{artifact.name}</span>
                <Id value={shortenId(artifact.digest)} />
              </div>
            )
          }
        >
          <ArtifactPicker onPick={pickArtifact} />
        </Step>

        <Step
          index={2}
          title="How to serve it"
          hint="Which card, how many, and how they are split. The plane sizes this from the weights and the count is yours to move."
          ready={!!artifact}
          open={open === 2}
          onReopen={() => setOpen(2)}
          answer={
            footprint && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs">
                  {footprintSummary(footprint, fit?.label)}
                </span>
                <Badge variant="outline" className="font-mono text-2xs">
                  derived from the fit
                </Badge>
              </div>
            )
          }
        >
          {artifact && (
            <ArtifactFitmentPanel
              bare
              mode="select"
              artifact={artifact}
              selected={how?.footprint ?? null}
              onPick={(picked, row) => pickHow({ footprint: picked, fit: row })}
            />
          )}
        </Step>

        {how && (
          <Proposed
            proposal={proposal}
            loading={proposed.isLoading}
            identical={identical}
          />
        )}

        <Step
          index={3}
          title="How much"
          hint="A total across every cluster, never a per-cluster number."
          ready={!!how}
          open={open >= 3}
        >
          <div className="space-y-3">
            <FormGrid>
              <NumberField
                label="Replicas"
                value={replicas}
                onChange={setReplicas}
                min={1}
                hint="Placement decides how these divide over clusters."
              />
              <TextField
                label="Name"
                value={effectiveName}
                onChange={(value) => {
                  setTouchedName(true)
                  setName(value)
                }}
                hint="Derived from the artifact. Change it if you want."
              />
              {(projects?.length ?? 0) > 1 && (
                <SelectField
                  label="Project"
                  value={project}
                  onChange={setProject}
                  hint="What quota this is charged to."
                  options={(projects ?? []).map((entry) => ({
                    value: entry.id,
                    label: entry.slug,
                  }))}
                />
              )}
            </FormGrid>

            {sole && (
              <p className="font-mono text-2xs text-muted-foreground/50">
                Charged to {sole.slug}, the only project. Its default priority
                class applies.
              </p>
            )}
            {projects?.length === 0 && (
              <p className="font-mono text-2xs text-amber-600 dark:text-amber-500">
                No projects exist. Quota is keyed by project, so one has to
                before anything can run.
              </p>
            )}
          </div>
        </Step>

        <Step
          index={4}
          title="Where"
          hint="Not a question — placement's answer. Override it only when you know something it does not."
          ready={!!how && !!effectiveProject}
          open={open >= 3}
        >
          <WhereStep
            value={where}
            onChange={setWhere}
            footprint={footprint}
            project={effectiveProject || null}
            replicas={replicas ?? 1}
            fit={fit}
            derivedFabric={derivedFabric}
          />
        </Step>

        <div className="flex items-center justify-between gap-3 pt-4">
          <p className="font-mono text-2xs text-muted-foreground/50">
            Need a task, a gang or a deadline? Those still go through the
            workload form on the list page.
          </p>
          <WriteAction>
            <Button disabled={!ready || pending} onClick={submit}>
              {pending ? "Deploying…" : "Deploy"}
            </Button>
          </WriteAction>
        </div>
      </div>
    </div>
  )
}

/**
 * What will be written under the shape, and where each number came from.
 *
 * The complaint this flow exists to answer is that defining a serving
 * configuration assumes the operator knows the right parameters, and they do not.
 * So the answer is filled in and then attributed: `weights` is the plane's own
 * arithmetic over `config.json`, `upstream` is a third party's tested
 * configuration with a permalink behind it, and anything unlabelled came off the
 * fitment row. A field nobody touches is still right, and it can say why.
 *
 * The identical-configuration line is informational and resolves nothing. Two
 * workloads with one configuration carry one `serving_digest`, which is what
 * reuse means now; whether a second one should exist is not this form's question.
 */
function Proposed({
  proposal,
  loading,
  identical,
}: {
  proposal: ServingProposal
  loading: boolean
  identical: Workload[]
}) {
  const upstream = proposal.upstream ?? null
  const at = servingProvenance(proposal)
  const seeded = Object.entries(at).filter(([, from]) => from !== "derived")

  return (
    <div className="space-y-2 rounded-lg border bg-muted/20 p-3">
      {loading && (
        <p className="font-mono text-2xs text-muted-foreground/50">
          reading what these weights imply…
        </p>
      )}

      {!loading && seeded.length === 0 && (
        <p className="font-mono text-2xs text-muted-foreground/60">
          Nothing is proposed for these weights beyond the fit. Every parameter
          under the shape is the plane's own default, and all of them are
          editable on the workload afterwards.
        </p>
      )}

      {seeded.length > 0 && (
        <div className="space-y-1">
          <p className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
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
                <span className="ml-1 text-muted-foreground/50">
                  {from === "weights" ? "from the weights" : "from vLLM"}
                </span>
              </Badge>
            ))}
          </div>
        </div>
      )}

      {upstream && <UpstreamClaim upstream={upstream} />}

      {identical.length > 0 && (
        <p className="border-t pt-2 font-mono text-2xs text-muted-foreground/70">
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
          stops a second one — they carry the same serving digest.
        </p>
      )}
    </div>
  )
}

/**
 * Who ran this, and on what.
 *
 * `verified_hardware` is the most useful sentence on the page and also the most
 * easily misread: it says the configuration works on that node shape, and says
 * nothing at all about whether the fleet owns one.
 */
function UpstreamClaim({ upstream }: { upstream: UpstreamServing }) {
  return (
    <p className="font-mono text-2xs text-muted-foreground/70">
      {upstream.source} publishes a tested configuration for {upstream.model_id}
      {upstream.verified_hardware.length > 0 &&
        ` — verified on ${upstream.verified_hardware.join(", ")}`}
      .{" "}
      <a
        href={upstream.source_uri}
        target="_blank"
        rel="noreferrer"
        className="underline hover:text-foreground"
      >
        Read it
      </a>
      {upstream.nightly_required && " · needs a nightly engine build"}
    </p>
  )
}
