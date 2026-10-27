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
  workloadsMatching,
  type ServingOverrides,
  type ServingProposal,
} from "@/modules/demand/deploy"
import {
  useArtifact,
  useArtifactServingDefaults,
  useCreateWorkload,
  useWorkloads,
} from "@/modules/demand/queries"
import {
  type Artifact,
  type ArtifactFit,
  type ServingFootprint,
} from "@/modules/demand/types"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { useMemo, useState } from "react"
import { toast } from "sonner"
import { ArtifactPicker } from "./ArtifactPicker"
import type { NewWorkloadSearch } from "./route"
import {
  RoleParamFields,
  TopologyChoice,
} from "@/components/demand/topology-declare"
import type { WorkloadRole } from "@/modules/demand/types"
import { Step } from "./Step"
import { AUTO_WHERE, WhereStep, type WhereChoice } from "./WhereStep"
import { ParamFields, Proposed, SizingDoubt } from "./ParamFields"

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
 * Deploying something, as four cards: artifact, topology, params, placement.
 *
 * A page and not a dialog. The fitment table is the picker for the topology and it is
 * six columns wide with three filters of its own; squeezed into a modal it becomes the
 * thing people click past. On a page it can be read, which is the point of having
 * derived it.
 *
 * Placement is a card and not a question: it renders as placement's answer with an
 * override hung off it, because a cluster offered as an ordinary field is how rule 3
 * stops being true in practice. The replica count and the name sit there with it —
 * they are the same sentence, and a card of their own said nothing the card below
 * did not already say better.
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
  /**
   * What has been typed over the proposal, by field. Absent means untouched.
   *
   * Held as overrides rather than as a copy of the block, because the proposal
   * arrives after the artifact is picked and again if it is picked twice: seeding
   * state from it would either overwrite what somebody had already typed or freeze
   * the first answer. An override survives both, and a field nobody touched keeps
   * following whatever the plane and the corpus now say.
   */
  const [overrides, setOverrides] = useState<ServingOverrides>({})
  const override = (patch: ServingOverrides) =>
    setOverrides((current) => ({ ...current, ...patch }))
  /**
   * `context_length` is the one override the topology step also writes, and that is
   * deliberate rather than a leak.
   *
   * The window is asked about twice — the fitment table sizes against it, the Params
   * step declares it — and it was two pieces of state that met nowhere: picking 800k
   * in the table left Params describing whatever the last clicked row happened to
   * say, and `derivedServingRequest` then submitted the stale one. It is one value,
   * so it is one field, and `overrides` is where a stated window already lived.
   *
   * Picking a row writes it too, because a fit the plane reduced is the plane's answer
   * to the question and accepting the row is accepting that answer. Raising it again
   * afterwards re-sizes the table, which is the escape hatch working as it reads.
   */
  /**
   * The prefill/decode split, or null for aggregated.
   *
   * Null and not `"aggregated"` with an empty list, so that the aggregated case
   * is expressible by absence all the way from this field to the request body —
   * the plane's digest only hashes the block when it is disaggregated, and
   * sending it unconditionally would change every existing digest in the fleet.
   */
  const [roles, setRoles] = useState<WorkloadRole[] | null>(null)
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
   * The block that would be written right now: the proposal with whatever has been
   * typed laid over it.
   *
   * Computed once and used for both the fields and the submit, so what the form shows
   * and what it sends cannot come apart. A form that displays one block and posts
   * another is the defect this shape exists to prevent, and it is the defect a second
   * `derivedServingRequest` call at submit time would reintroduce.
   */
  const request =
    artifact && footprint
      ? derivedServingRequest(artifact, footprint, proposal, overrides)
      : null
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
    // Other weights, other proposal. Keeping what was typed for the last artifact would
    // carry a parser somebody chose for a different model into this one silently.
    setOverrides({})
    setRoles(null)
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
    if (choice.footprint.context_length > 0)
      override({ context_length: choice.footprint.context_length })
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
    if (!artifact || !how || !request) return
    const cluster = where.mode === "pin" ? where.pinned : null

    try {
      const workload = await createWorkload.mutateAsync({
        serving: {
          ...request,
          /* Omitted entirely when aggregated rather than sent as `"aggregated"`
             with an empty role list: absent and empty must not both be sayable
             about a thing with one meaning, and the digest depends on the key
             not being there. */
          ...(roles
            ? {
                topology: {
                  serving_topology: "disaggregated" as const,
                  roles: roles.map((role) => ({
                    role: role.role,
                    weight: role.weight,
                    max_batch_tokens: role.max_batch_tokens,
                    engine_args: role.engine_args,
                  })),
                },
              }
            : {}),
          shape: {
            ...request.shape,
            // Inside the shape because that is what carries it: the fit derives
            // the answer and the Where step is where somebody disagrees.
            requires_single_topology_domain:
              where.fabric ?? request.shape.requires_single_topology_domain,
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
        description="declared · the artifact, its topology, its parameters and its placement"
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
          title="Artifact"
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
          title="Topology"
          hint="The shape of the devices — which card, how many, and how they are split. The plane sizes this from the weights and the count is yours to move."
          ready={!!artifact}
          open={open === 2}
          onReopen={() => setOpen(2)}
          answer={
            footprint && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs">
                  {footprintSummary(footprint, fit?.label)}
                </span>
                {roles && (
                  <Badge variant="outline" className="font-mono text-2xs">
                    {roles
                      .map((role) => `${role.weight} ${role.role}`)
                      .join(" : ")}{" "}
                    · {groupDevicesOf(roles, footprint.device_count)} per group
                  </Badge>
                )}
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
              contextLength={overrides.context_length ?? null}
              onContextLengthChange={(context_length) =>
                override({ context_length })
              }
              onPick={(picked, row) => pickHow({ footprint: picked, fit: row })}
            />
          )}
          {how && (
            /* Under the device shape and not in a step of its own, because it
               is the same question one level down: the fitment table answers
               how many devices a WORKER takes, and this answers how many
               workers an indivisible unit takes. Both decide the footprint,
               and 8 devices becoming 24 is not a detail to find later.

               Not called "topology" anywhere a person can see, even though the
               column is `serving_topology`: this step already owns that word
               for the fabric a replica has to fit inside, and two senses of it
               on one screen is a bug somebody writes. */
            <div className="mt-3 space-y-2 border-t pt-3">
              <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
                Prefill and decode
              </div>
              <TopologyChoice
                roles={roles}
                onChange={setRoles}
                deviceCount={how.footprint.device_count}
                deviceClass={how.footprint.device_class}
                budgetDevices={fit?.devices_allocatable}
              />
            </div>
          )}
        </Step>

        <Step
          index={3}
          title="Params"
          hint="Filled in from the weights and the vLLM recipe, and every one of them is yours to change. Where a value came from is written under it."
          ready={!!how}
          open={open >= 3}
        >
          <Proposed
            artifact={artifact}
            proposal={proposal}
            loading={proposed.isLoading}
            identical={identical}
            overrides={overrides}
          />
          {fit && !fit.fits && <SizingDoubt fit={fit} />}
          {roles && (
            <div className="mb-3 space-y-2">
              <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
                Per role
              </div>
              <RoleParamFields roles={roles} onChange={setRoles} />
            </div>
          )}
          {request && (
            <ParamFields
              request={request}
              proposal={proposal}
              artifact={artifact}
              overrides={overrides}
              onChange={override}
            />
          )}
        </Step>

        <Step
          index={4}
          title="Placement"
          hint="Not a question — placement's answer, with how much of it to run. Override the cluster only when you know something placement does not."
          ready={!!how}
          open={open >= 3}
        >
          <div className="space-y-3">
            <FormGrid>
              <NumberField
                label={roles ? "Groups" : "Replicas"}
                value={replicas}
                onChange={setReplicas}
                min={1}
                hint={
                  roles
                    ? `A total across every cluster, counted in groups — ${(replicas ?? 0) * groupDevicesOf(roles, how?.footprint.device_count ?? 0)} devices in all. Placement fits a whole group or none of it.`
                    : "A total across every cluster. Placement decides how these divide."
                }
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
              <p className="font-mono text-2xs text-muted-foreground">
                Charged to {sole.slug}, the only project. Its default priority
                class applies.
              </p>
            )}
            {projects?.length === 0 && (
              <p className="font-mono text-2xs text-warning">
                No projects exist. Quota is keyed by project, so one has to
                before anything can run.
              </p>
            )}

            {effectiveProject && (
              <WhereStep
                value={where}
                onChange={setWhere}
                footprint={footprint}
                project={effectiveProject}
                replicas={replicas ?? 1}
                fit={fit}
                derivedFabric={derivedFabric}
              />
            )}
          </div>
        </Step>

        <div className="flex items-center justify-between gap-3 pt-4">
          <p className="font-mono text-2xs text-muted-foreground">
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

/** `Σ weight × device_count`, for a shape that has no workload behind it yet. */
function groupDevicesOf(roles: WorkloadRole[], deviceCount: number): number {
  return roles.reduce((total, role) => total + role.weight * deviceCount, 0)
}
