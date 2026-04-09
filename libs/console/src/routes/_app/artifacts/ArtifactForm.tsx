import {
  FormDialog,
  FormGrid,
  JsonField,
  NumberField,
  SelectField,
  TextField,
} from "@/components/layout/form"
import { Button } from "@/components/ui/button"
import { NotFoundError } from "@/lib/api"
import { formatError } from "@/lib/format"
import { useProjectOptions } from "@/modules/binding/queries"
import { formatBytes } from "@/modules/demand/display"
import {
  useCreateArtifact,
  useResolveArtifact,
  useUpdateArtifact,
} from "@/modules/demand/queries"
import {
  ARTIFACT_KINDS,
  type Artifact,
  type ArtifactLocationRequest,
  type RecipeDefaults,
} from "@/modules/demand/types"
import { useState } from "react"
import { toast } from "sonner"

const UNSET = "__none__"

/**
 * Create and edit in one component. The digest is only offered on create: it is
 * the artifact's identity, and the plane has no verb for re-pointing one set of
 * bytes at another.
 *
 * On create there are two ways in. Naming a HuggingFace repo is the one most
 * people want — the plane reads the repo and derives the digest from its per-file
 * oids, so the field is filled in and read-only rather than typed. Typing every
 * field by hand stays, because not every set of weights lives on the Hub.
 */
export function ArtifactForm({
  open,
  onOpenChange,
  artifact,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  artifact?: Artifact
}) {
  const editing = !!artifact
  const create = useCreateArtifact()
  const update = useUpdateArtifact()
  const resolve = useResolveArtifact()
  const { data: projects } = useProjectOptions()

  const [source, setSource] = useState("")
  const [resolved, setResolved] = useState<string | null>(null)
  const [recipe, setRecipe] = useState<RecipeDefaults | null>(null)
  const [notes, setNotes] = useState<string[]>([])
  const [existing, setExisting] = useState<string | null>(null)

  const [digest, setDigest] = useState(artifact?.digest ?? "")
  const [name, setName] = useState(artifact?.name ?? "")
  const [kind, setKind] = useState(artifact?.kind ?? "weights")
  const [format, setFormat] = useState(artifact?.format ?? "")
  const [sizeBytes, setSizeBytes] = useState<number | null>(
    artifact?.size_bytes ?? 0
  )
  const [licence, setLicence] = useState(artifact?.licence ?? "")
  const [parameterCount, setParameterCount] = useState<number | null>(
    artifact?.parameter_count ?? null
  )
  const [project, setProject] = useState(artifact?.project_id ?? UNSET)
  const [metadata, setMetadata] = useState<unknown>(artifact?.metadata ?? {})
  const [locations, setLocations] = useState<unknown>(
    artifact?.locations.map((l) => ({
      scheme: l.scheme,
      uri: l.uri,
      region: l.region,
      preferred: l.preferred,
    })) ?? []
  )
  const [jsonValid, setJsonValid] = useState(true)
  const [locationsValid, setLocationsValid] = useState(true)

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const shared = {
    name,
    kind,
    format: format || null,
    size_bytes: sizeBytes ?? 0,
    licence: licence || null,
    parameter_count: parameterCount,
    project: project === UNSET ? null : project,
    metadata: (metadata as Record<string, unknown>) ?? {},
    locations: (locations as ArtifactLocationRequest[]) ?? [],
  }

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Artifact ${verb}`, { description: name })
  }

  /**
   * The source is kept and re-sent on submit rather than the digest it produced.
   * The plane derives identity either way, and posting the source lets it derive
   * from the repo instead of trusting a value that sat in a form. What is kept is
   * the slug as typed, not the returned `hf://owner/name@sha` — the plane reads a
   * source as `owner/name` and would take the pinned commit for part of the name.
   */
  const populate = () => {
    const slug = source.trim()
    if (!slug) return
    resolve.mutate(slug, {
      onSuccess: (draft) => {
        setResolved(slug)
        setRecipe(draft.recipe)
        setNotes(draft.notes)
        setExisting(draft.existing_id)
        setDigest(draft.digest)
        setName(draft.name)
        setKind(draft.kind)
        setFormat(draft.format ?? "")
        setSizeBytes(draft.size_bytes)
        setLicence(draft.licence ?? "")
        setParameterCount(draft.parameter_count)
        setMetadata(draft.metadata)
        setLocations(draft.locations)
        if (draft.existing_id) {
          toast.info("These weights are already registered", {
            description: draft.existing_id,
          })
          return
        }
        toast.success("Resolved", { description: draft.reference })
      },
    })
  }

  const clearSource = () => {
    setResolved(null)
    setRecipe(null)
    setNotes([])
    setExisting(null)
    setDigest("")
    resolve.reset()
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        { ref: artifact.id, body: shared, revision: artifact.revision },
        {
          onSuccess: () => done("updated"),
          onError: (e) =>
            toast.error(formatError(e, "Could not update artifact")),
        }
      )
      return
    }
    create.mutate(
      resolved ? { source: resolved, ...shared } : { digest, ...shared },
      {
        onSuccess: () => done("registered"),
        onError: (e) =>
          toast.error(formatError(e, "Could not register artifact")),
      }
    )
  }

  return (
    <FormDialog
      icon="artifact"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit artifact" : "New artifact"}
      description="Immutable, content-addressed bytes. Identity is the digest."
      error={error}
      pending={pending}
      submitLabel={editing ? "Save" : "Register"}
      disabled={
        !name ||
        (!editing && !resolved && !digest) ||
        !jsonValid ||
        !locationsValid
      }
      onSubmit={submit}
    >
      {!editing && (
        <div className="space-y-2 rounded-md border bg-muted/40 p-3">
          <div className="flex items-end gap-2">
            <TextField
              className="flex-1"
              label="HuggingFace repo"
              value={source}
              onChange={(value) => {
                setSource(value)
                if (resolved) clearSource()
              }}
              placeholder="openai/gpt-oss-20b"
              hint="Or hf://owner/name. The plane reads the repo and fills in the rest."
            />
            <Button
              type="button"
              variant="outline"
              className="mb-5"
              disabled={!source.trim() || resolve.isPending}
              onClick={populate}
            >
              {resolve.isPending ? "Reading repo…" : "Resolve"}
            </Button>
          </div>

          {resolve.isPending && (
            <p className="text-2xs text-muted-foreground">
              Walking the repo tree. A large one takes a few seconds.
            </p>
          )}

          {!!resolve.error && <ResolveError error={resolve.error} />}

          {!!existing && (
            <p className="font-mono text-2xs text-amber-600 dark:text-amber-400">
              Already registered as {existing}. Registering again will conflict.
            </p>
          )}

          {notes.length > 0 && (
            <ul className="space-y-1 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-2xs text-amber-600 dark:text-amber-400">
              {notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {!editing &&
        (resolved ? (
          <TextField
            label="Digest"
            value={digest}
            onChange={() => {}}
            disabled
            hint="Derived from the repo's per-file checksums. You do not set this."
          />
        ) : (
          <TextField
            label="Digest"
            value={digest}
            onChange={setDigest}
            placeholder="sha256:…"
            hint="Identity. It cannot be changed later."
          />
        ))}

      <FormGrid>
        <TextField label="Name" value={name} onChange={setName} />
        <SelectField
          label="Kind"
          value={kind}
          onChange={setKind}
          options={[...ARTIFACT_KINDS]}
        />
        <TextField
          label="Format"
          value={format}
          onChange={setFormat}
          placeholder="safetensors"
        />
        <TextField label="Licence" value={licence} onChange={setLicence} />
        <NumberField
          label="Size (bytes)"
          value={sizeBytes}
          onChange={setSizeBytes}
          min={0}
        />
        <NumberField
          label="Parameters"
          value={parameterCount}
          onChange={setParameterCount}
          min={0}
        />
        <SelectField
          label="Project"
          value={project}
          onChange={setProject}
          options={[
            { value: UNSET, label: "none — shared" },
            ...(projects ?? []).map((p) => ({ value: p.id, label: p.slug })),
          ]}
        />
      </FormGrid>

      {recipe && <RecipeSuggestion recipe={recipe} />}

      <JsonField
        label="Locations"
        value={locations}
        rows={5}
        hint="Array of {scheme, uri, region, preferred}."
        onChange={(value, valid) => {
          setLocations(value ?? [])
          setLocationsValid(valid)
        }}
      />

      <JsonField
        label="Metadata"
        value={metadata}
        rows={3}
        onChange={(value, valid) => {
          setMetadata(value ?? {})
          setJsonValid(valid)
        }}
      />
    </FormDialog>
  )
}

/**
 * A repo that does not exist and a repo that exists but is gated are the same
 * HTTP failure to a client and completely different problems to a person: one is
 * a typo, the other is a licence to accept and a token to configure.
 */
function ResolveError({ error }: { error: unknown }) {
  const missing = error instanceof NotFoundError
  return (
    <div className="space-y-1 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
      <p>{formatError(error, "Could not read that repo")}</p>
      <p className="text-2xs opacity-80">
        {missing
          ? "No such repo on HuggingFace. Check the owner and name."
          : "Accept the licence on HuggingFace, then give the plane a token with access."}
      </p>
    </div>
  )
}

/**
 * Sizing implied by the weights, shown and never submitted. It belongs to the
 * recipe someone writes next; an artifact has no opinion on how it is served.
 */
function RecipeSuggestion({ recipe }: { recipe: RecipeDefaults }) {
  const rows: [string, string][] = [
    ["Devices", String(recipe.device_count)],
    ["Device memory", formatBytes(recipe.device_memory_mib_min * 1024 * 1024)],
    [
      "Context length",
      recipe.context_length ? recipe.context_length.toLocaleString() : "—",
    ],
    ["Quantization", recipe.quantization ?? "none"],
    ["Dtype", recipe.dtype ?? "—"],
  ]
  return (
    <div className="space-y-2 rounded-md border bg-muted/40 px-3 py-2.5">
      <p className="text-xs font-medium">Suggested serving shape</p>
      <p className="text-2xs text-muted-foreground/70">
        Read from the repo's config. Nothing here is saved on the artifact — it
        carries over when you write a recipe for these weights.
      </p>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
        {rows.map(([label, value]) => (
          <div key={label} className="space-y-0.5">
            <dt className="text-2xs text-muted-foreground/70">{label}</dt>
            <dd className="font-mono text-xs">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
