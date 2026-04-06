import {
  FormDialog,
  FormGrid,
  JsonField,
  NumberField,
  SelectField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { useProjectOptions } from "@/modules/binding/queries"
import { useCreateArtifact, useUpdateArtifact } from "@/modules/demand/queries"
import {
  ARTIFACT_KINDS,
  type Artifact,
  type ArtifactLocationRequest,
} from "@/modules/demand/types"
import { useState } from "react"
import { toast } from "sonner"

const UNSET = "__none__"

/**
 * Create and edit in one component. The digest is only offered on create: it is
 * the artifact's identity, and the plane has no verb for re-pointing one set of
 * bytes at another.
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
  const { data: projects } = useProjectOptions()

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
      { digest, ...shared },
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
      disabled={!name || (!editing && !digest) || !jsonValid || !locationsValid}
      onSubmit={submit}
    >
      {!editing && (
        <TextField
          label="Digest"
          value={digest}
          onChange={setDigest}
          placeholder="sha256:…"
          hint="Identity. It cannot be changed later."
        />
      )}

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
