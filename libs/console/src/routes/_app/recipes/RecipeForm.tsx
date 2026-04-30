import {
  CapabilityFields,
  ServingFields,
} from "@/components/demand/recipe-serving"
import { DeviceClassField } from "@/components/supply/device-class"
import {
  FormDialog,
  FormGrid,
  JsonField,
  LabelsField,
  NumberField,
  SelectField,
  SwitchField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { emptyCapabilities, emptyServing } from "@/modules/demand/display"
import {
  useArtifactOptions,
  useCreateRecipe,
  useUpdateRecipe,
} from "@/modules/demand/queries"
import type { Recipe, RecipeFootprint } from "@/modules/demand/types"
import type { RecipeDraft } from "@/modules/discovered/types"
import { useState } from "react"
import { toast } from "sonner"

/**
 * The write shape is flat where the read shape is nested, so the form reads
 * `recipe.parallelism.tensor` back out into `tensor_parallel` rather than
 * posting the response shape.
 */
export function RecipeForm({
  open,
  onOpenChange,
  recipe,
  draft,
  footprint,
  artifactId,
  onCreated,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  recipe?: Recipe
  draft?: RecipeDraft
  /**
   * A footprint chosen from the fitment matrix, layered over `draft` and under
   * `recipe`. Not a `RecipeDraft`, because that is a reading of a workload a cluster is
   * already running and this is the opposite — nothing is running yet, and the only
   * thing picked was which card, how many, and how they are split.
   *
   * The two parallelism degrees come from the fit rather than from the device count.
   * A replica of sixteen devices across two domains is tensor-eight by pipeline-two,
   * and setting tensor to sixteen would ask a tensor group to reach over the fabric.
   */
  footprint?: RecipeFootprint
  artifactId?: string
  onCreated?: (recipe: Recipe) => void
}) {
  const editing = !!recipe
  const create = useCreateRecipe()
  const update = useUpdateRecipe()
  const { data: artifacts } = useArtifactOptions()

  const [pickedArtifact, setArtifact] = useState("")
  const [name, setName] = useState(recipe?.name ?? draft?.name ?? "")
  const [engine, setEngine] = useState(
    recipe?.engine ?? draft?.engine ?? "vllm"
  )
  const [engineVersion, setEngineVersion] = useState(
    recipe?.engine_version ?? draft?.engine_version ?? ""
  )
  const [engineImage, setEngineImage] = useState(
    recipe?.engine_image ?? draft?.engine_image ?? ""
  )
  const [deviceClass, setDeviceClass] = useState(
    recipe?.shape.device_class ??
      footprint?.device_class ??
      draft?.device_class ??
      ""
  )
  const [deviceCount, setDeviceCount] = useState<number | null>(
    recipe?.shape.device_count ??
      footprint?.device_count ??
      draft?.device_count ??
      1
  )
  const [deviceMemory, setDeviceMemory] = useState<number | null>(
    recipe?.shape.device_memory_mib_min ?? 0
  )
  const [singleDomain, setSingleDomain] = useState(
    recipe?.shape.requires_single_topology_domain ?? false
  )
  const [domainKind, setDomainKind] = useState(
    recipe?.shape.topology_domain_kind ?? ""
  )
  const [contextLength, setContextLength] = useState<number | null>(
    recipe?.context_length ??
      footprint?.context_length ??
      draft?.context_length ??
      8192
  )
  const [maxBatchTokens, setMaxBatchTokens] = useState<number | null>(
    recipe?.max_batch_tokens ?? draft?.max_batch_tokens ?? null
  )
  const [tensor, setTensor] = useState<number | null>(
    recipe?.parallelism.tensor ??
      footprint?.device_count ??
      draft?.tensor_parallel ??
      1
  )
  const [pipeline, setPipeline] = useState<number | null>(
    recipe?.parallelism.pipeline ??
      footprint?.pipeline_parallel ??
      draft?.pipeline_parallel ??
      1
  )
  const [expert, setExpert] = useState<number | null>(
    recipe?.parallelism.expert ?? draft?.expert_parallel ?? 1
  )
  const [quantization, setQuantization] = useState(
    recipe?.quantization ?? footprint?.quantization ?? draft?.quantization ?? ""
  )
  const [dtype, setDtype] = useState(recipe?.dtype ?? draft?.dtype ?? "")
  const [kvCacheDtype, setKvCacheDtype] = useState(
    recipe?.cache.kv_cache_dtype ?? ""
  )
  const [kvCacheFraction, setKvCacheFraction] = useState<number | null>(
    recipe?.cache.kv_cache_fraction ?? draft?.kv_cache_fraction ?? null
  )
  const [prefixCache, setPrefixCache] = useState(
    recipe?.cache.prefix_cache_enabled ?? false
  )
  const [cpuCores, setCpuCores] = useState<number | null>(
    recipe?.resources.cpu_cores ?? draft?.cpu_cores ?? null
  )
  const [memoryMib, setMemoryMib] = useState<number | null>(
    recipe?.resources.memory_mib ?? draft?.memory_mib ?? null
  )
  const [diskGib, setDiskGib] = useState<number | null>(
    recipe?.resources.disk_gib ?? null
  )
  const [engineArgs, setEngineArgs] = useState<unknown>(
    recipe?.engine_args ?? draft?.engine_args ?? {}
  )
  const [argsValid, setArgsValid] = useState(true)
  const [labels, setLabels] = useState<Record<string, string>>(
    recipe?.labels ?? {}
  )
  const [serving, setServing] = useState(recipe?.serving ?? emptyServing())
  const [capabilities, setCapabilities] = useState(
    recipe?.capabilities ?? emptyCapabilities()
  )

  /**
   * A draft names its weights, it does not know their id. The caller resolves
   * one when it can — the adoption flow registers the artifact first — and
   * otherwise it is matched by name against the catalogue, leaving the select
   * empty for a person to pick when nothing matches.
   */
  const matched = draft
    ? artifacts?.find(
        (a) =>
          a.name === draft.artifact.name || a.digest === draft.artifact.digest
      )
    : undefined
  const artifact =
    pickedArtifact || recipe?.artifact_id || artifactId || matched?.id || ""

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const shared = {
    name,
    engine,
    engine_version: engineVersion,
    engine_image: engineImage || null,
    engine_args: (engineArgs as Record<string, unknown>) ?? {},
    quantization: quantization || null,
    dtype: dtype || null,
    tensor_parallel: tensor ?? 1,
    pipeline_parallel: pipeline ?? 1,
    expert_parallel: expert ?? 1,
    context_length: contextLength ?? 0,
    max_batch_tokens: maxBatchTokens,
    kv_cache_dtype: kvCacheDtype || null,
    kv_cache_fraction: kvCacheFraction,
    prefix_cache_enabled: prefixCache,
    device_class: deviceClass,
    device_count: deviceCount ?? 1,
    device_memory_mib_min: deviceMemory ?? 0,
    requires_single_topology_domain: singleDomain,
    topology_domain_kind: domainKind || null,
    cpu_cores: cpuCores,
    memory_mib: memoryMib,
    disk_gib: diskGib,
    labels,
    serving,
    capabilities,
  }

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Recipe ${verb}`, { description: name })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        { ref: recipe.id, body: shared, revision: recipe.revision },
        {
          onSuccess: () => done("updated"),
          onError: (e) =>
            toast.error(formatError(e, "Could not update recipe")),
        }
      )
      return
    }
    create.mutate(
      { artifact, ...shared },
      {
        onSuccess: (created) => {
          done("created")
          onCreated?.(created)
        },
        onError: (e) => toast.error(formatError(e, "Could not create recipe")),
      }
    )
  }

  return (
    <FormDialog
      icon="recipe"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit recipe" : "New recipe"}
      description={
        draft
          ? "Read from a running workload. Nothing is written until you save."
          : "A reproducible way to serve one artifact on one device class."
      }
      error={error}
      pending={pending}
      disabled={
        !name ||
        !engine ||
        !engineVersion ||
        !deviceClass ||
        !contextLength ||
        (!editing && !artifact) ||
        !argsValid
      }
      onSubmit={submit}
    >
      {!editing && !artifactId && (
        <SelectField
          label="Artifact"
          value={artifact}
          onChange={setArtifact}
          hint="Fixed at creation — other bytes are another recipe."
          options={(artifacts ?? []).map((a) => ({
            value: a.id,
            label: a.name,
          }))}
        />
      )}

      <FormGrid>
        <TextField label="Name" value={name} onChange={setName} />
        <TextField label="Engine" value={engine} onChange={setEngine} />
        <TextField
          label="Engine version"
          value={engineVersion}
          onChange={setEngineVersion}
        />
        <TextField
          label="Engine image"
          value={engineImage}
          onChange={setEngineImage}
          placeholder="Resolved from the engine and version"
          hint="Leave empty unless you are pinning a specific build. The plane resolves the image from the engine and version above, and the same image fetches the weights — a recipe with no image is one whose weights never download."
        />
        <DeviceClassField value={deviceClass} onChange={setDeviceClass} />
        <NumberField
          label="Device count"
          value={deviceCount}
          onChange={setDeviceCount}
          min={1}
        />
        <NumberField
          label="Device memory min (MiB)"
          value={deviceMemory}
          onChange={setDeviceMemory}
          min={0}
        />
        <TextField
          label="Topology domain kind"
          value={domainKind}
          onChange={setDomainKind}
          placeholder="nvlink"
        />
        <NumberField
          label="Context length"
          value={contextLength}
          onChange={setContextLength}
          min={1}
        />
        <NumberField
          label="Max batch tokens"
          value={maxBatchTokens}
          onChange={setMaxBatchTokens}
          min={0}
        />
        <NumberField
          label="Tensor parallel"
          value={tensor}
          onChange={setTensor}
          min={1}
        />
        <NumberField
          label="Pipeline parallel"
          value={pipeline}
          onChange={setPipeline}
          min={1}
        />
        <NumberField
          label="Expert parallel"
          value={expert}
          onChange={setExpert}
          min={1}
        />
        <TextField
          label="Quantization"
          value={quantization}
          onChange={setQuantization}
          placeholder="fp8"
        />
        <TextField
          label="dtype"
          value={dtype}
          onChange={setDtype}
          placeholder="bfloat16"
        />
        <TextField
          label="KV cache dtype"
          value={kvCacheDtype}
          onChange={setKvCacheDtype}
        />
        <NumberField
          label="KV cache fraction"
          value={kvCacheFraction}
          onChange={setKvCacheFraction}
        />
        <NumberField
          label="CPU cores"
          value={cpuCores}
          onChange={setCpuCores}
          min={0}
        />
        <NumberField
          label="Memory (MiB)"
          value={memoryMib}
          onChange={setMemoryMib}
          min={0}
        />
        <NumberField
          label="Disk (GiB)"
          value={diskGib}
          onChange={setDiskGib}
          min={0}
        />
      </FormGrid>

      <SwitchField
        label="Requires a single topology domain"
        hint="Tensor-parallel work that must not span a fabric boundary."
        checked={singleDomain}
        onChange={setSingleDomain}
      />
      <SwitchField
        label="Prefix cache"
        checked={prefixCache}
        onChange={setPrefixCache}
      />

      <JsonField
        label="Engine args"
        value={engineArgs}
        rows={4}
        hint="The escape hatch for a flag nothing below models. A flag that is modelled below is set there instead."
        onChange={(value, valid) => {
          setEngineArgs(value ?? {})
          setArgsValid(valid)
        }}
      />

      <ServingFields
        engine={engine}
        value={serving}
        onChange={setServing}
        engineArgs={(engineArgs as Record<string, unknown>) ?? {}}
      />

      <CapabilityFields value={capabilities} onChange={setCapabilities} />

      <LabelsField value={labels} onChange={setLabels} />
    </FormDialog>
  )
}
