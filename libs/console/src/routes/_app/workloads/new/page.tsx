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
  DEFAULT_ENGINE,
  derivedRecipeRequest,
  requiresSingleDomain,
  derivedWorkloadName,
  footprintOfRecipe,
  footprintSummary,
  matchExistingRecipe,
} from "@/modules/demand/deploy"
import {
  useArtifact,
  useCreateRecipe,
  useCreateWorkload,
  useRecipes,
  useWorkloads,
} from "@/modules/demand/queries"
import type {
  Artifact,
  ArtifactFit,
  Recipe,
  RecipeFootprint,
} from "@/modules/demand/types"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { useMemo, useState } from "react"
import { toast } from "sonner"
import { ArtifactPicker } from "./ArtifactPicker"
import { RecipeReuse } from "./RecipeReuse"
import type { NewWorkloadSearch } from "./route"
import { Step } from "./Step"
import { AUTO_WHERE, WhereStep, type WhereChoice } from "./WhereStep"

/**
 * How the weights will be served — either a recipe that already exists or a footprint
 * a fit proposed and no recipe has been written for yet.
 *
 * The two are kept apart rather than normalised to a footprint, because the difference
 * is load-bearing downstream: an existing recipe has an id, so placement preview can be
 * asked a real question, and a deploy against it writes nothing but a workload.
 */
/** The six numbers a fitment row is, if the link carried all of them. */
function footprintFromSearch(
  search: NewWorkloadSearch
): RecipeFootprint | null {
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

type How =
  | { kind: "existing"; recipe: Recipe }
  /**
   * `fit` is null when the footprint arrived in the URL from an artifact page's Deploy
   * rather than from a row clicked here. The six numbers are the whole of the footprint,
   * but the fleet counts alongside them are not in the link, so the Where step says less.
   */
  | { kind: "derive"; footprint: RecipeFootprint; fit: ArtifactFit | null }

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
  const createRecipe = useCreateRecipe()
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
    linkedFootprint
      ? { kind: "derive", footprint: linkedFootprint, fit: null }
      : null
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
  const recipes = useRecipes(artifact ? { artifact: artifact.id } : {})
  const existing = artifact ? (recipes.items ?? []) : []
  /** Only to avoid a name the list can already see taken. The plane owns collisions. */
  const workloads = useWorkloads({})
  const taken = useMemo(
    () => (workloads.items ?? []).map((workload) => workload.name),
    [workloads.items]
  )

  const footprint = how
    ? how.kind === "existing"
      ? footprintOfRecipe(how.recipe)
      : how.footprint
    : null
  const fit = how?.kind === "derive" ? how.fit : null
  const recipeId = how?.kind === "existing" ? how.recipe.id : null
  /**
   * What the chosen shape says about fabric, before anyone overrides it. Derived from the
   * footprint for a new recipe and read off an existing one, so the box the Where step
   * starts on is what would actually be written either way.
   */
  const derivedFabric =
    how?.kind === "existing"
      ? how.recipe.shape.requires_single_topology_domain
      : how
        ? requiresSingleDomain(how.footprint)
        : false

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

  const pending = createRecipe.isPending || createWorkload.isPending
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
      choice.kind === "derive" &&
      how?.kind === "derive" &&
      how.footprint.device_class === choice.footprint.device_class
    const counts =
      choice.kind === "derive" ? (choice.fit?.layouts.length ?? 1) : 1
    setHow(choice)
    setOpen(refining || counts <= 1 ? 3 : 2)
  }

  /**
   * Derive the recipe if one is needed, then create the workload.
   *
   * Two calls rather than one, because the plane has no deploy endpoint yet. The dedup
   * happens here for the same reason — `matchExistingRecipe` over the artifact's recipes
   * so that deploying the same thing twice reuses the recipe instead of leaving a second
   * identical one behind. That check belongs in the plane, where it could be a
   * constraint rather than a race, and this is the console standing in for it.
   */
  const submit = async () => {
    if (!artifact || !how) return
    const cluster = where.mode === "pin" ? where.pinned : null

    try {
      let recipe: string
      if (how.kind === "existing") {
        recipe = how.recipe.id
      } else {
        const match = matchExistingRecipe(
          existing,
          artifact,
          how.footprint,
          DEFAULT_ENGINE
        )
        recipe =
          match?.id ??
          (
            await createRecipe.mutateAsync(
              derivedRecipeRequest(artifact, how.footprint)
            )
          ).id
      }
      const workload = await createWorkload.mutateAsync({
        recipe,
        name: effectiveName,
        project: effectiveProject,
        kind: "service",
        cluster,
        // The plane turns either list into a Policy attached to the workload. Sent only
        // for the mode that means them, so switching modes cannot leave one behind.
        allowed_regions: where.mode === "region" ? where.regions : [],
        allowed_clusters: where.mode === "clusters" ? where.allowed : [],
        // Sent only when it disagrees with the recipe. Null is the ordinary value and
        // means the recipe's own requirement stands, so writing the derived answer here
        // would pin today's derivation onto a workload that should follow its recipe.
        requires_single_topology_domain: where.fabric,
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
                  {how?.kind === "existing"
                    ? `${how.recipe.name} v${how.recipe.version}`
                    : "new recipe, derived"}
                </Badge>
              </div>
            )
          }
        >
          {artifact && (
            <div className="space-y-4">
              <RecipeReuse
                recipes={existing}
                onPick={(recipe) => pickHow({ kind: "existing", recipe })}
              />
              <ArtifactFitmentPanel
                bare
                mode="select"
                artifact={artifact}
                selected={how?.kind === "derive" ? how.footprint : null}
                onPick={(picked, row) =>
                  pickHow({ kind: "derive", footprint: picked, fit: row })
                }
              />
            </div>
          )}
        </Step>

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
            recipe={recipeId}
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
