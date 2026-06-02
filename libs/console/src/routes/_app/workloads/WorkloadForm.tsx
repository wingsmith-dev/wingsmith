import {
  CapabilityFields,
  ServingFields,
} from "@/components/demand/serving-config"
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
import { DeviceClassField } from "@/components/supply/device-class"
import { formatError } from "@/lib/format"
import {
  usePriorityClassOptions,
  useProjectOptions,
} from "@/modules/binding/queries"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import {
  WORKLOAD_KINDS,
  WORKLOAD_STRATEGIES,
  emptyCapabilities,
  emptyServing,
  scaleUnit,
  splittable,
} from "@/modules/demand/display"
import {
  useArtifactOptions,
  useCreateWorkload,
  useUpdateWorkload,
} from "@/modules/demand/queries"
import type { Workload } from "@/modules/demand/types"
import { useState } from "react"
import { toast } from "sonner"

/**
 * What to run, and how much of it — never where.
 *
 * There is no cluster to choose here, because choosing one is placement's job:
 * the form declares a total and the clusters the workload may use, and the
 * per-cluster table on the detail page is the answer. The one exception is the
 * pin, which is offered last and labelled as what it is — an override that
 * takes the decision away from the scheduler and forces a single cluster.
 *
 * The artifact, the project and the kind are create-only. A workload is weights ×
 * kind the way a deployment is workload × cluster; changing either is a new
 * workload, and the endpoint in front of it is the stable name that makes that
 * cheap. The rest of the serving block is not create-only — editing it recomputes
 * `serving_digest` and rolls the replicas, which is the point of it living here.
 *
 * Scale is create-only too. Changing it afterwards is `workload.scale`, an
 * operation with a phase, because it can sit pending on quota indefinitely.
 *
 * This is the long way in. The deploy flow at `/workloads/new` answers most of
 * these from a fitment row and is the path for an ordinary service; this form is
 * for a task, a gang, a deadline, or a field that flow does not ask about.
 */
export function WorkloadForm({
  open,
  onOpenChange,
  workload,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  workload?: Workload
}) {
  const editing = !!workload
  const create = useCreateWorkload()
  const update = useUpdateWorkload()
  const { data: clusters } = useClusterOptions()
  const { data: artifacts } = useArtifactOptions()
  const { data: projects } = useProjectOptions()
  const { data: priorities } = usePriorityClassOptions()

  const serving = workload?.serving
  const [artifact, setArtifact] = useState(workload?.serving.artifact_id ?? "")
  const [engine, setEngine] = useState(serving?.engine ?? "vllm")
  const [engineVersion, setEngineVersion] = useState(
    serving?.engine_version ?? ""
  )
  const [engineImage, setEngineImage] = useState(serving?.engine_image ?? "")
  const [deviceClass, setDeviceClass] = useState(
    serving?.shape.device_class ?? ""
  )
  const [deviceCount, setDeviceCount] = useState<number | null>(
    serving?.shape.device_count ?? 1
  )
  const [deviceMemory, setDeviceMemory] = useState<number | null>(
    serving?.shape.device_memory_mib_min ?? 0
  )
  const [singleDomain, setSingleDomain] = useState(
    serving?.shape.requires_single_topology_domain ?? false
  )
  const [domainKind, setDomainKind] = useState(
    serving?.shape.topology_domain_kind ?? ""
  )
  const [contextLength, setContextLength] = useState<number | null>(
    serving?.context_length ?? 8192
  )
  const [maxBatchTokens, setMaxBatchTokens] = useState<number | null>(
    serving?.max_batch_tokens ?? null
  )
  const [tensor, setTensor] = useState<number | null>(
    serving?.parallelism.tensor ?? 1
  )
  const [pipeline, setPipeline] = useState<number | null>(
    serving?.parallelism.pipeline ?? 1
  )
  const [expert, setExpert] = useState<number | null>(
    serving?.parallelism.expert ?? 1
  )
  const [quantization, setQuantization] = useState(serving?.quantization ?? "")
  const [dtype, setDtype] = useState(serving?.dtype ?? "")
  const [kvCacheDtype, setKvCacheDtype] = useState(
    serving?.cache.kv_cache_dtype ?? ""
  )
  const [kvCacheFraction, setKvCacheFraction] = useState<number | null>(
    serving?.cache.kv_cache_fraction ?? null
  )
  const [prefixCache, setPrefixCache] = useState(
    serving?.cache.prefix_cache_enabled ?? true
  )
  const [cpuCores, setCpuCores] = useState<number | null>(
    serving?.resources.cpu_cores ?? null
  )
  const [memoryMib, setMemoryMib] = useState<number | null>(
    serving?.resources.memory_mib ?? null
  )
  const [diskGib, setDiskGib] = useState<number | null>(
    serving?.resources.disk_gib ?? null
  )
  const [engineArgs, setEngineArgs] = useState<unknown>(
    serving?.engine_args ?? {}
  )
  const [argsValid, setArgsValid] = useState(true)
  const [behaviour, setBehaviour] = useState(serving?.serving ?? emptyServing())
  const [capabilities, setCapabilities] = useState(
    serving?.capabilities ?? emptyCapabilities()
  )
  const [project, setProject] = useState(workload?.project_id ?? "")
  const [name, setName] = useState(workload?.name ?? "")
  const [kind, setKind] = useState(workload?.kind ?? "service")
  const [priorityClass, setPriorityClass] = useState(
    workload?.priority_class ?? ""
  )
  const [minScale, setMinScale] = useState<number | null>(
    workload?.scale.min ?? 1
  )
  const [maxScale, setMaxScale] = useState<number | null>(
    workload?.scale.max ?? 1
  )
  const [desiredScale, setDesiredScale] = useState<number | null>(
    workload?.scale.desired ?? 1
  )
  const [strategy, setStrategy] = useState(workload?.strategy ?? "pack")
  const [maxClusters, setMaxClusters] = useState<number | null>(
    workload?.max_clusters ?? null
  )
  const [pinned, setPinned] = useState("")
  const [gangSize, setGangSize] = useState<number | null>(
    workload?.gang_size ?? 1
  )
  const [preemptible, setPreemptible] = useState(workload?.preemptible ?? false)
  const [deadline, setDeadline] = useState(workload?.deadline_at ?? "")
  const [overrides, setOverrides] = useState<unknown>(workload?.overrides ?? {})
  const [overridesValid, setOverridesValid] = useState(true)
  const [labels, setLabels] = useState<Record<string, string>>(
    workload?.labels ?? {}
  )

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error
  const unit = scaleUnit(kind)
  const fansOut = splittable(kind) && !pinned

  /**
   * The write shape is the read shape, so the groups go back the way they came.
   * `behaviour` is this form's local name for the block that travels as
   * `serving`; the key is what the plane hashes into the digest, so only the
   * variable is renamed and never the field.
   */
  const servingBody = {
    engine,
    engine_version: engineVersion,
    engine_image: engineImage || null,
    engine_args: (engineArgs as Record<string, unknown>) ?? {},
    quantization: quantization || null,
    dtype: dtype || null,
    parallelism: {
      tensor: tensor ?? 1,
      pipeline: pipeline ?? 1,
      expert: expert ?? 1,
    },
    context_length: contextLength ?? 0,
    max_batch_tokens: maxBatchTokens,
    cache: {
      kv_cache_dtype: kvCacheDtype || null,
      kv_cache_fraction: kvCacheFraction,
      prefix_cache_enabled: prefixCache,
      prefix_cache_config: serving?.cache.prefix_cache_config ?? {},
    },
    shape: {
      device_class: deviceClass,
      device_count: deviceCount ?? 1,
      device_memory_mib_min: deviceMemory ?? 0,
      requires_single_topology_domain: singleDomain,
      topology_domain_kind: domainKind || null,
    },
    resources: {
      cpu_cores: cpuCores,
      memory_mib: memoryMib,
      disk_gib: diskGib,
    },
    serving: behaviour,
    capabilities,
  }

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Workload ${verb}`, { description: name })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        {
          ref: workload.id,
          body: {
            name,
            serving: servingBody,
            priority_class: priorityClass || undefined,
            preemptible,
            deadline_at: deadline || null,
            strategy,
            max_clusters: maxClusters,
            overrides: (overrides as Record<string, unknown>) ?? {},
            labels,
          },
          revision: workload.revision,
        },
        {
          onSuccess: () => done("updated"),
          onError: (e) =>
            toast.error(formatError(e, "Could not update workload")),
        }
      )
      return
    }
    // A gang does not have a range: its ranks are co-scheduled, so the three
    // totals are its gang size and placement rejects anything else.
    const total = kind === "gang" ? (gangSize ?? 1) : (desiredScale ?? 1)
    create.mutate(
      {
        serving: { artifact, ...servingBody },
        name,
        project,
        kind,
        cluster: pinned || null,
        priority_class: priorityClass || null,
        min_scale: kind === "gang" ? total : (minScale ?? 1),
        max_scale: kind === "gang" ? total : (maxScale ?? 1),
        desired_scale: total,
        strategy,
        max_clusters: maxClusters,
        gang_size: gangSize ?? 1,
        preemptible,
        deadline_at: deadline || null,
        overrides: (overrides as Record<string, unknown>) ?? {},
        labels,
      },
      {
        onSuccess: () => done("created"),
        onError: (e) =>
          toast.error(formatError(e, "Could not create workload")),
      }
    )
  }

  return (
    <FormDialog
      icon="workload"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit workload" : "New workload"}
      description="Weights, how to serve them, a total, and the clusters it may use. Placement decides which ones."
      error={error}
      pending={pending}
      disabled={
        !name ||
        !engine ||
        !engineVersion ||
        !deviceClass ||
        !contextLength ||
        (!editing && (!artifact || !project)) ||
        !argsValid ||
        !overridesValid
      }
      onSubmit={submit}
    >
      {!editing && (
        <FormGrid>
          <SelectField
            label="Artifact"
            value={artifact}
            onChange={setArtifact}
            hint="Fixed at creation — other bytes are another workload."
            options={(artifacts ?? []).map((a) => ({
              value: a.id,
              label: a.name,
            }))}
          />
          <SelectField
            label="Project"
            value={project}
            onChange={setProject}
            options={(projects ?? []).map((p) => ({
              value: p.id,
              label: p.slug,
            }))}
          />
          <SelectField
            label="Kind"
            value={kind}
            onChange={setKind}
            options={WORKLOAD_KINDS}
          />
          {kind === "gang" && (
            <NumberField
              label="Gang size (ranks)"
              value={gangSize}
              onChange={setGangSize}
              min={1}
              hint="Co-scheduled on one cluster. A gang is never split."
            />
          )}
        </FormGrid>
      )}

      <FormGrid>
        <TextField label="Name" value={name} onChange={setName} />
        <SelectField
          label="Priority class"
          value={priorityClass}
          onChange={setPriorityClass}
          options={(priorities ?? []).map((p) => ({
            value: p.name,
            label: p.name,
          }))}
        />
        {!editing && kind !== "gang" && (
          <>
            <NumberField
              label={`Desired ${unit}`}
              value={desiredScale}
              onChange={setDesiredScale}
              min={0}
              hint="The total across every cluster, not a per-cluster number."
            />
            <NumberField
              label={`Minimum ${unit}`}
              value={minScale}
              onChange={setMinScale}
              min={0}
            />
            <NumberField
              label={`Maximum ${unit}`}
              value={maxScale}
              onChange={setMaxScale}
              min={0}
            />
          </>
        )}
        <TextField
          label="Deadline"
          value={deadline}
          onChange={setDeadline}
          placeholder="2026-01-01T00:00:00Z"
          hint="RFC 3339, or blank for none."
        />
      </FormGrid>

      {fansOut && (
        <FormGrid>
          <SelectField
            label="Strategy"
            value={strategy}
            onChange={setStrategy}
            options={WORKLOAD_STRATEGIES}
          />
          <NumberField
            label="Cluster fan-out cap"
            value={maxClusters}
            onChange={setMaxClusters}
            min={1}
            hint="Blank for no cap. Pack fills a cluster before reaching for the next."
          />
        </FormGrid>
      )}

      {!editing && (
        <SelectField
          label="Pin to cluster"
          value={pinned}
          onChange={setPinned}
          options={(clusters ?? []).map((c) => ({
            value: c.id,
            label: clusterLabel(c),
          }))}
          hint="An override, not the normal path: a pinned workload gets exactly one deployment and is never checked for capacity."
        />
      )}

      <SwitchField
        label="Preemptible"
        hint="Whether higher-priority work may take this capacity back."
        checked={preemptible}
        onChange={setPreemptible}
      />

      <FormGrid>
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
          hint="Leave empty unless you are pinning a specific build. The plane resolves the image from the engine and version above, and the same image fetches the weights — a workload with no image is one whose weights never download."
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
        value={behaviour}
        onChange={setBehaviour}
        engineArgs={(engineArgs as Record<string, unknown>) ?? {}}
      />

      <CapabilityFields value={capabilities} onChange={setCapabilities} />

      <JsonField
        label="Overrides"
        value={overrides}
        rows={4}
        hint="Departures from the serving block, applied on every cluster this runs on."
        onChange={(value, valid) => {
          setOverrides(value ?? {})
          setOverridesValid(valid)
        }}
      />

      <LabelsField value={labels} onChange={setLabels} />
    </FormDialog>
  )
}
