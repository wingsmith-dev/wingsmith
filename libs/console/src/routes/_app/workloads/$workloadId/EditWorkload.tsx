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
import { usePriorityClassOptions } from "@/modules/binding/queries"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { WORKLOAD_STRATEGIES } from "@/modules/demand/display"
import {
  declaredRoles,
  groupDevices,
  reservedRoleArgs,
  rolesAtGroupCount,
} from "@/modules/demand/topology"
import { useUpdateWorkload } from "@/modules/demand/queries"
import type {
  Workload,
  WorkloadServingUpdate,
  WorkloadUpdateRequest,
} from "@/modules/demand/types"
import { useState } from "react"
import { toast } from "sonner"

/**
 * Editing a workload one answer at a time.
 *
 * The whole declared object in one dialog is unreadable and unsafe in the same
 * way: forty fields is nothing anybody reads before saving, and the fields that
 * roll every replica sit in it beside the fields that do not. Each dialog here
 * is one question, opened from the panel that displays the answer, and it PATCHes
 * only its own keys — the API takes a partial, so a name change sends a name.
 *
 * `serving` is the exception the API states: its sub-blocks replace whole, so a
 * dialog that touches one sends that block complete, from the workload it was
 * opened on.
 */
function useSection(workload: Workload, onOpenChange: (open: boolean) => void) {
  const update = useUpdateWorkload()
  const save = (body: WorkloadUpdateRequest, what: string) =>
    update.mutate(
      { ref: workload.id, body, revision: workload.revision },
      {
        onSuccess: () => {
          onOpenChange(false)
          toast.success(`${what} updated`, { description: workload.name })
        },
        onError: (e) =>
          toast.error(formatError(e, "Could not update workload")),
      }
    )
  return { save, pending: update.isPending, error: update.error }
}

/**
 * What a write to `serving` costs, said before it is paid.
 *
 * Any change under `serving` recomputes `serving_digest`, and every replica of
 * the old digest is replaced. That is a rollout across every cluster placement
 * chose, so the dialogs that can cause one arm first and save second: the button
 * does not become the write until the operator has read what the write does.
 */
function RollNotice({
  workload,
  armed,
}: {
  workload: Workload
  armed: boolean
}) {
  const replicas = workload.scale.ready
  const clusters = workload.deployments.length
  if (clusters === 0) {
    return (
      <p className="rounded-md border bg-muted/50 px-3 py-2 text-xs text-muted-foreground">
        This changes the serving digest. Nothing is running yet, so nothing
        rolls.
      </p>
    )
  }
  return (
    <p
      className={
        armed
          ? "rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400"
          : "rounded-md border bg-muted/50 px-3 py-2 text-xs text-muted-foreground"
      }
    >
      {armed
        ? "Saving now replaces "
        : "Saving recomputes the serving digest, replacing "}
      {replicas} {workload.scale.unit} across {clusters} cluster
      {clusters === 1 ? "" : "s"}
      {armed ? ". Press the button again to proceed." : "."}
    </p>
  )
}

/** The two-step submit the rolling dialogs share. */
function useArmed(workload: Workload) {
  const [armed, setArmed] = useState(false)
  const rolls = workload.deployments.length > 0
  return {
    armed,
    disarm: () => setArmed(false),
    /** True when the caller should write; false when this click only armed it. */
    ready: () => {
      if (!rolls || armed) return true
      setArmed(true)
      return false
    },
    label: armed ? "Apply and roll" : "Save",
  }
}

/** Name, deadline, labels — nothing here touches a running replica. */
export function EditDetailsDialog({
  workload,
  open,
  onOpenChange,
}: {
  workload: Workload
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { save, pending, error } = useSection(workload, onOpenChange)
  const [name, setName] = useState(workload.name)
  const [deadline, setDeadline] = useState(workload.deadline_at ?? "")
  const [labels, setLabels] = useState<Record<string, string>>(
    workload.labels ?? {}
  )

  return (
    <FormDialog
      icon="workload"
      open={open}
      onOpenChange={onOpenChange}
      title="Details"
      description="What this workload is called, and how the scheduler treats it."
      error={error}
      pending={pending}
      disabled={!name}
      onSubmit={() =>
        save({ name, deadline_at: deadline || null, labels }, "Details")
      }
    >
      <FormGrid>
        <TextField label="Name" value={name} onChange={setName} />
        <TextField
          label="Deadline"
          value={deadline}
          onChange={setDeadline}
          placeholder="2026-01-01T00:00:00Z"
          hint="RFC 3339, or blank for none."
        />
      </FormGrid>
      <LabelsField value={labels} onChange={setLabels} />
    </FormDialog>
  )
}

/**
 * Everything placement reads before it chooses, and nothing it does not.
 *
 * Priority and preemptibility are here rather than under Details because this is
 * the panel that displays them: they are read by the scheduler, not by a person
 * looking for the workload's name. None of it rebuilds a replica — the plane
 * re-runs placement and moves shares between clusters, so the digest is
 * untouched and no pod is replaced for a change made here.
 *
 * Two fields on the panel are deliberately absent. Affinity, anti-affinity and
 * placement grouping are not modelled by the plane at all — the panel shows
 * seeded values marked `TODO-API` — and the pin is create-only, because a
 * pinned workload is never checked for capacity and moving that decision after
 * the fact is a different operation from editing a policy.
 */
export function EditPlacementDialog({
  workload,
  open,
  onOpenChange,
}: {
  workload: Workload
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { save, pending, error } = useSection(workload, onOpenChange)
  const { data: priorities } = usePriorityClassOptions()
  const { data: clusters } = useClusterOptions()
  const [priorityClass, setPriorityClass] = useState(
    workload.priority_class ?? ""
  )
  const [preemptible, setPreemptible] = useState(workload.preemptible)
  const [strategy, setStrategy] = useState(workload.strategy)
  const [maxClusters, setMaxClusters] = useState<number | null>(
    workload.max_clusters ?? null
  )
  const [floors, setFloors] = useState<Record<string, number>>(
    workload.cluster_floors ?? {}
  )

  return (
    <FormDialog
      icon="workload"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title="Scheduling constraints"
      description="What placement is allowed to do with this workload. It chooses the clusters; these are the rules it chooses under."
      error={error}
      pending={pending}
      onSubmit={() =>
        save(
          {
            priority_class: priorityClass || undefined,
            preemptible,
            strategy,
            max_clusters: maxClusters,
            cluster_floors: floors,
          },
          "Scheduling constraints"
        )
      }
    >
      <FormGrid>
        <SelectField
          label="Priority class"
          value={priorityClass}
          onChange={setPriorityClass}
          options={(priorities ?? []).map((p) => ({
            value: p.name,
            label: p.name,
          }))}
        />
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

      <SwitchField
        label="Preemptible"
        hint="Whether higher-priority work may take this capacity back."
        checked={preemptible}
        onChange={setPreemptible}
      />

      <ClusterFloorsField
        value={floors}
        onChange={setFloors}
        clusters={(clusters ?? []).map((c) => ({
          id: c.id,
          label: clusterLabel(c),
        }))}
      />
    </FormDialog>
  )
}

/**
 * The floor under a cluster's share: the minimum placement must leave there,
 * whatever the strategy would otherwise do.
 *
 * Blank is not zero. A cluster with no floor is one placement may empty; a
 * cluster with a floor of 0 is the same statement said twice, so a cleared row
 * is removed from the map rather than written as a zero.
 */
function ClusterFloorsField({
  value,
  onChange,
  clusters,
}: {
  value: Record<string, number>
  onChange: (value: Record<string, number>) => void
  clusters: { id: string; label: string }[]
}) {
  const set = (id: string, floor: number | null) => {
    const next = { ...value }
    if (floor == null) delete next[id]
    else next[id] = floor
    onChange(next)
  }
  if (clusters.length === 0) return null
  return (
    <div className="space-y-2">
      <div className="space-y-1">
        <h3 className="text-xs font-medium">Cluster floors</h3>
        <p className="text-2xs text-muted-foreground/70">
          The minimum share placement must keep on a cluster. Blank means no
          floor — placement may take the share to zero.
        </p>
      </div>
      <FormGrid>
        {clusters.map((cluster) => (
          <NumberField
            key={cluster.id}
            label={cluster.label}
            value={value[cluster.id] ?? null}
            onChange={(floor) => set(cluster.id, floor)}
            min={0}
          />
        ))}
      </FormGrid>
    </div>
  )
}

/**
 * The engine, the shape it runs on, and the numbers the engine is started with.
 *
 * Every field here is under `serving`, so saving rolls. The blocks are sent
 * whole because that is how the API takes them.
 */
export function EditEngineDialog({
  workload,
  open,
  onOpenChange,
}: {
  workload: Workload
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { save, pending, error } = useSection(workload, onOpenChange)
  const arm = useArmed(workload)
  const serving = workload.serving
  const [engine, setEngine] = useState(serving.engine)
  const [engineRuntimeVersion, setEngineRuntimeVersion] = useState(
    serving.engine_runtime_version
  )
  const [engineRuntimeImage, setEngineRuntimeImage] = useState(
    serving.engine_runtime_image ?? ""
  )
  const [frontendImage, setFrontendImage] = useState(
    serving.frontend_image ?? ""
  )
  const [deviceClass, setDeviceClass] = useState(serving.shape.device_class)
  const [deviceCount, setDeviceCount] = useState<number | null>(
    serving.shape.device_count
  )
  const [deviceMemory, setDeviceMemory] = useState<number | null>(
    serving.shape.device_memory_mib_min
  )
  const [singleDomain, setSingleDomain] = useState(
    serving.shape.requires_single_topology_domain
  )
  const [domainKind, setDomainKind] = useState(
    serving.shape.topology_domain_kind ?? ""
  )
  const [contextLength, setContextLength] = useState<number | null>(
    serving.context_length
  )
  const [maxBatchTokens, setMaxBatchTokens] = useState<number | null>(
    serving.max_batch_tokens
  )
  const [tensor, setTensor] = useState<number | null>(
    serving.parallelism.tensor
  )
  const [pipeline, setPipeline] = useState<number | null>(
    serving.parallelism.pipeline
  )
  const [expert, setExpert] = useState<number | null>(
    serving.parallelism.expert
  )
  const [quantization, setQuantization] = useState(serving.quantization ?? "")
  const [dtype, setDtype] = useState(serving.dtype ?? "")
  const [kvCacheDtype, setKvCacheDtype] = useState(
    serving.cache.kv_cache_dtype ?? ""
  )
  const [kvCacheFraction, setKvCacheFraction] = useState<number | null>(
    serving.cache.kv_cache_fraction
  )
  const [prefixCache, setPrefixCache] = useState(
    serving.cache.prefix_cache_enabled
  )
  const [cpuCores, setCpuCores] = useState<number | null>(
    serving.resources.cpu_cores
  )
  const [memoryMib, setMemoryMib] = useState<number | null>(
    serving.resources.memory_mib
  )
  const [diskGib, setDiskGib] = useState<number | null>(
    serving.resources.disk_gib
  )

  const body: WorkloadServingUpdate = {
    engine,
    engine_runtime_version: engineRuntimeVersion,
    engine_runtime_image: engineRuntimeImage || null,
    frontend_image: frontendImage || null,
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
      prefix_cache_config: serving.cache.prefix_cache_config ?? {},
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
  }

  return (
    <FormDialog
      icon="workload"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title="Engine & hardware"
      description="Which engine, on what devices, started with which numbers."
      error={error}
      pending={pending}
      submitLabel={arm.label}
      disabled={
        !engine || !engineRuntimeVersion || !deviceClass || !contextLength
      }
      onSubmit={() => {
        if (arm.ready()) save({ serving: body }, "Engine & hardware")
      }}
    >
      <FormGrid>
        <TextField
          label="Engine"
          value={engine}
          onChange={(v) => {
            arm.disarm()
            setEngine(v)
          }}
          hint="Which engine the worker runs. It picks the module and the flag names, and carries no version of its own."
        />
        <TextField
          label="Engine runtime version"
          value={engineRuntimeVersion}
          onChange={(v) => {
            arm.disarm()
            setEngineRuntimeVersion(v)
          }}
          placeholder="latest"
          hint="A tag of the Dynamo runtime the engine runs in — 1.4.1 — and never the engine's own version."
        />
        <TextField
          label="Engine runtime image"
          value={engineRuntimeImage}
          onChange={(v) => {
            arm.disarm()
            setEngineRuntimeImage(v)
          }}
          placeholder="Resolved from the engine and the runtime version"
          hint="Leave empty unless you are pinning a specific build. The same image fetches the weights."
        />
        <TextField
          label="Frontend image"
          value={frontendImage}
          onChange={(v) => {
            arm.disarm()
            setFrontendImage(v)
          }}
          placeholder="Whatever the cluster runs its frontends on"
          hint="A second image, not a spelling of the one above. Empty means the cluster decides."
        />
        <DeviceClassField
          value={deviceClass}
          onChange={(v) => {
            arm.disarm()
            setDeviceClass(v)
          }}
        />
        <NumberField
          label="Device count"
          value={deviceCount}
          onChange={(v) => {
            arm.disarm()
            setDeviceCount(v)
          }}
          min={1}
        />
        <NumberField
          label="Device memory min (MiB)"
          value={deviceMemory}
          onChange={(v) => {
            arm.disarm()
            setDeviceMemory(v)
          }}
          min={0}
        />
        <TextField
          label="Topology domain kind"
          value={domainKind}
          onChange={(v) => {
            arm.disarm()
            setDomainKind(v)
          }}
          placeholder="nvlink"
        />
        <NumberField
          label="Context length"
          value={contextLength}
          onChange={(v) => {
            arm.disarm()
            setContextLength(v)
          }}
          min={1}
        />
        <NumberField
          label="Max batch tokens"
          value={maxBatchTokens}
          onChange={(v) => {
            arm.disarm()
            setMaxBatchTokens(v)
          }}
          min={0}
        />
        <NumberField
          label="Tensor parallel"
          value={tensor}
          onChange={(v) => {
            arm.disarm()
            setTensor(v)
          }}
          min={1}
        />
        <NumberField
          label="Pipeline parallel"
          value={pipeline}
          onChange={(v) => {
            arm.disarm()
            setPipeline(v)
          }}
          min={1}
        />
        <NumberField
          label="Expert parallel"
          value={expert}
          onChange={(v) => {
            arm.disarm()
            setExpert(v)
          }}
          min={1}
        />
        <TextField
          label="Quantization"
          value={quantization}
          onChange={(v) => {
            arm.disarm()
            setQuantization(v)
          }}
          placeholder="fp8"
        />
        <TextField
          label="dtype"
          value={dtype}
          onChange={(v) => {
            arm.disarm()
            setDtype(v)
          }}
          placeholder="bfloat16"
        />
        <TextField
          label="KV cache dtype"
          value={kvCacheDtype}
          onChange={(v) => {
            arm.disarm()
            setKvCacheDtype(v)
          }}
        />
        <NumberField
          label="KV cache fraction"
          value={kvCacheFraction}
          onChange={(v) => {
            arm.disarm()
            setKvCacheFraction(v)
          }}
        />
        <NumberField
          label="CPU cores"
          value={cpuCores}
          onChange={(v) => {
            arm.disarm()
            setCpuCores(v)
          }}
          min={0}
        />
        <NumberField
          label="Memory (MiB)"
          value={memoryMib}
          onChange={(v) => {
            arm.disarm()
            setMemoryMib(v)
          }}
          min={0}
        />
        <NumberField
          label="Disk (GiB)"
          value={diskGib}
          onChange={(v) => {
            arm.disarm()
            setDiskGib(v)
          }}
          min={0}
        />
      </FormGrid>

      <SwitchField
        label="Requires a single topology domain"
        hint="Tensor-parallel work that must not span a fabric boundary."
        checked={singleDomain}
        onChange={(v) => {
          arm.disarm()
          setSingleDomain(v)
        }}
      />
      <SwitchField
        label="Prefix cache"
        checked={prefixCache}
        onChange={(v) => {
          arm.disarm()
          setPrefixCache(v)
        }}
      />

      <RollNotice workload={workload} armed={arm.armed} />
    </FormDialog>
  )
}

/** The escape hatch for flags nothing models, and the per-cluster departures from them. */
export function EditEngineArgsDialog({
  workload,
  open,
  onOpenChange,
}: {
  workload: Workload
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { save, pending, error } = useSection(workload, onOpenChange)
  const arm = useArmed(workload)
  const [engineArgs, setEngineArgs] = useState<unknown>(
    workload.serving.engine_args ?? {}
  )
  const [argsValid, setArgsValid] = useState(true)
  const [overrides, setOverrides] = useState<unknown>(workload.overrides ?? {})
  const [overridesValid, setOverridesValid] = useState(true)

  return (
    <FormDialog
      icon="workload"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title="Engine arguments"
      description="What is passed to the engine that no typed field above covers."
      error={error}
      pending={pending}
      submitLabel={arm.label}
      disabled={!argsValid || !overridesValid}
      onSubmit={() => {
        if (arm.ready())
          save(
            {
              serving: {
                engine_args: (engineArgs as Record<string, unknown>) ?? {},
              },
              overrides: (overrides as Record<string, unknown>) ?? {},
            },
            "Engine arguments"
          )
      }}
    >
      <JsonField
        label="Engine args"
        value={engineArgs}
        rows={6}
        hint="The escape hatch for a flag nothing models. A flag that is modelled in Engine & hardware is set there instead."
        onChange={(value, valid) => {
          arm.disarm()
          setEngineArgs(value ?? {})
          setArgsValid(valid)
        }}
      />
      <JsonField
        label="Overrides"
        value={overrides}
        rows={4}
        hint="Departures from the serving block, applied on every cluster this runs on."
        onChange={(value, valid) => {
          arm.disarm()
          setOverrides(value ?? {})
          setOverridesValid(valid)
        }}
      />

      <RollNotice workload={workload} armed={arm.armed} />
    </FormDialog>
  )
}

/** The typed serving block — what the engine is told to do with a request. */
export function EditServingDialog({
  workload,
  open,
  onOpenChange,
}: {
  workload: Workload
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { save, pending, error } = useSection(workload, onOpenChange)
  const arm = useArmed(workload)
  const [behaviour, setBehaviour] = useState(workload.serving.serving)

  return (
    <FormDialog
      icon="workload"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title="Serving"
      description="Reasoning, tool calls and structured output, as the engine is configured to handle them."
      error={error}
      pending={pending}
      submitLabel={arm.label}
      onSubmit={() => {
        if (arm.ready()) save({ serving: { serving: behaviour } }, "Serving")
      }}
    >
      <ServingFields
        plain
        engine={workload.serving.engine}
        value={behaviour}
        onChange={(value) => {
          arm.disarm()
          setBehaviour(value)
        }}
        engineArgs={workload.serving.engine_args ?? {}}
      />

      <RollNotice workload={workload} armed={arm.armed} />
    </FormDialog>
  )
}

/** What the model is claimed to support. Asserted, not enforced — and still part of the digest. */
export function EditCapabilitiesDialog({
  workload,
  open,
  onOpenChange,
}: {
  workload: Workload
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { save, pending, error } = useSection(workload, onOpenChange)
  const arm = useArmed(workload)
  const [capabilities, setCapabilities] = useState(
    workload.serving.capabilities
  )

  return (
    <FormDialog
      icon="workload"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title="Capabilities"
      description="What this configuration claims to support. The plane does not check it, and the digest still counts it."
      error={error}
      pending={pending}
      submitLabel={arm.label}
      onSubmit={() => {
        if (arm.ready()) save({ serving: { capabilities } }, "Capabilities")
      }}
    >
      <CapabilityFields
        plain
        value={capabilities}
        onChange={(value) => {
          arm.disarm()
          setCapabilities(value)
        }}
      />

      <RollNotice workload={workload} armed={arm.armed} />
    </FormDialog>
  )
}

/**
 * What a ratio change costs, said before it is paid — and it costs nothing.
 *
 * This notice exists to be the opposite of `RollNotice`, and the pair is the
 * point. A person editing a disaggregated workload has two controls in front of
 * them that look alike and are not: one adds and removes workers, the other
 * takes the model down for about twenty minutes per role. Showing the same
 * amber warning on both would teach them to click through it, and then it is
 * not there for the one that matters.
 */
function ScaleNotice({
  workload,
  groups,
}: {
  workload: Workload
  groups: number
}) {
  const roles = rolesAtGroupCount(workload, groups)
  return (
    <p className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-700 dark:text-emerald-400">
      {roles.map((role) => `${role.count} ${role.role}`).join(" · ")}. The
      weights are not in the serving digest, so this adds and removes workers
      and reloads no model weights. The role that is not changing keeps its
      pods.
    </p>
  )
}

/**
 * What a per-role configuration change costs, and it is more than it looks.
 *
 * Dynamo's worker spec hash is one value across every worker component in the
 * graph, not one per component. So retuning prefill's batch size renames
 * decode's objects too, and decode had no reason to restart. Both roles reload
 * their weights, at roughly twenty minutes each.
 *
 * The second sentence is the one that is easy to leave out and expensive to
 * discover: the rollout holds both generations at once, and a 1:2 group of
 * three nodes needs six to do that. On a fleet this size the new generation has
 * nowhere to land until the old one lets go.
 */
function ReshapeNotice({
  workload,
  armed,
}: {
  workload: Workload
  armed: boolean
}) {
  const clusters = workload.deployments.length
  if (clusters === 0) {
    return (
      <p className="rounded-md border bg-muted/50 px-3 py-2 text-xs text-muted-foreground">
        This changes the serving digest. Nothing is running yet, so nothing
        reloads.
      </p>
    )
  }
  return (
    <div
      className={
        armed
          ? "space-y-1 rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-xs text-destructive"
          : "space-y-1 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400"
      }
    >
      <p>
        {armed ? "Saving now restarts " : "Saving restarts "}
        <strong>both roles</strong> on {clusters} cluster
        {clusters === 1 ? "" : "s"}, at about twenty minutes of weight load
        each. Dynamo hashes one worker spec across the whole graph, so a change
        to one role renames the other role&rsquo;s pods as well.
      </p>
      <p>
        The rollout holds both generations at once. A group of{" "}
        {groupDevices(workload) ?? "these"} devices needs that much again free
        before the new generation can land, so on a small fleet the model is
        down for the whole reload rather than rolled through it.
      </p>
      {armed && <p>Press the button again to proceed.</p>}
    </div>
  )
}

/**
 * The ratio, and nothing else that lives on a role.
 *
 * One dialog for the weights alone, because the weights are the only part of a
 * role that is free to change. Everything else a role carries goes through
 * `EditRolesDialog`, which arms first. Two forms over one table is deliberate
 * duplication: the split is not about the fields, it is about the cost, and a
 * person needs to be able to change the ratio without reading a warning that
 * does not apply to what they are doing.
 */
export function EditRatioDialog({
  workload,
  open,
  onOpenChange,
}: {
  workload: Workload
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { save, pending, error } = useSection(workload, onOpenChange)
  const roles = declaredRoles(workload)
  const [weights, setWeights] = useState<Record<string, number | null>>(() =>
    Object.fromEntries(roles.map((role) => [role.role, role.weight]))
  )

  const invalid = roles.some((role) => (weights[role.role] ?? 0) < 1)
  /**
   * The workload as the new weights would make it, so that the device cost and
   * the process counts below are the ones being asked for rather than the ones
   * already running. `group_devices` is blanked because the plane's derived
   * number is the old ratio's, and here the arithmetic has to be redone.
   */
  const preview: Workload = {
    ...workload,
    serving: {
      ...workload.serving,
      topology: {
        serving_topology: "disaggregated",
        group_nodes_hint: "",
        group_devices: 0,
        roles: roles.map((role) => ({
          ...role,
          weight: weights[role.role] ?? role.weight,
        })),
      },
    },
  }

  return (
    <FormDialog
      icon="workload"
      open={open}
      onOpenChange={onOpenChange}
      title="Ratio"
      description="How many of each role a group holds. Scale multiplies these; it does not change them."
      error={error}
      pending={pending}
      disabled={invalid}
      submitLabel="Save"
      onSubmit={() =>
        save(
          {
            serving: {
              topology: {
                serving_topology: "disaggregated",
                roles: roles.map((role) => ({
                  role: role.role,
                  weight: weights[role.role] ?? role.weight,
                })),
              },
            },
          },
          "Ratio"
        )
      }
    >
      <FormGrid>
        {roles.map((role) => (
          <NumberField
            key={role.role}
            label={`${role.role} per group`}
            value={weights[role.role] ?? null}
            onChange={(value) =>
              setWeights((current) => ({ ...current, [role.role]: value }))
            }
            min={1}
          />
        ))}
      </FormGrid>
      <p className="text-xs text-muted-foreground">
        A group is now {groupDevices(preview) ?? "—"} devices, at{" "}
        {workload.serving.shape.device_count} per worker. Placement fits a whole
        group or none of it, so a wide group is harder to place than a narrow
        one — the plane will say so rather than round it down.
      </p>
      <ScaleNotice workload={preview} groups={workload.scale.desired} />
    </FormDialog>
  )
}

/**
 * What each role IS: its batch size and the arguments only it gets.
 *
 * Separate from the ratio above and armed like the other digest writes, because
 * this is the edit that takes the model down. The reserved arguments are
 * refused here as well as at the plane — `disaggregation-mode` is the role
 * itself and `kv-transfer-config` is the same string on both roles, so a value
 * for either is a contradiction of a decision already acted on, and finding
 * that out at a failed reconcile is finding it out too late to fix cheaply.
 */
export function EditRolesDialog({
  workload,
  open,
  onOpenChange,
}: {
  workload: Workload
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { save, pending, error } = useSection(workload, onOpenChange)
  const arm = useArmed(workload)
  const roles = declaredRoles(workload)
  const [batch, setBatch] = useState<Record<string, number | null>>(() =>
    Object.fromEntries(roles.map((r) => [r.role, r.max_batch_tokens]))
  )
  const [args, setArgs] = useState<Record<string, unknown>>(() =>
    Object.fromEntries(roles.map((r) => [r.role, r.engine_args ?? {}]))
  )
  const [valid, setValid] = useState(true)

  const reserved = roles.flatMap((role) =>
    reservedRoleArgs((args[role.role] ?? {}) as Record<string, unknown>).map(
      (key) => `${role.role}: ${key}`
    )
  )

  return (
    <FormDialog
      icon="workload"
      open={open}
      onOpenChange={onOpenChange}
      title="Role configuration"
      description="What each role's engine is told, over and above the workload's own arguments."
      error={error}
      pending={pending}
      disabled={!valid || reserved.length > 0}
      submitLabel={arm.label}
      onSubmit={() => {
        if (arm.ready())
          save(
            {
              serving: {
                topology: {
                  serving_topology: "disaggregated",
                  /* The weights ride along unchanged. The block replaces
                     whole, the way every sub-block of `serving` does, so
                     omitting them here would read as a request to reset the
                     ratio rather than as a request to leave it alone. */
                  roles: roles.map((role) => ({
                    role: role.role,
                    weight: role.weight,
                    max_batch_tokens: batch[role.role] ?? null,
                    engine_args: (args[role.role] ?? {}) as Record<
                      string,
                      unknown
                    >,
                  })),
                },
              },
            },
            "Role configuration"
          )
      }}
    >
      {roles.map((role) => (
        <div key={role.role} className="space-y-2">
          <div className="font-mono text-xs">{role.role}</div>
          <NumberField
            label="Max batch tokens"
            value={batch[role.role] ?? null}
            onChange={(value) => {
              arm.disarm()
              setBatch((current) => ({ ...current, [role.role]: value }))
            }}
            min={0}
            hint="Blank inherits the workload's own value."
          />
          <JsonField
            label="Engine args"
            value={args[role.role] ?? {}}
            rows={4}
            onChange={(value, ok) => {
              arm.disarm()
              setValid(ok)
              if (ok) setArgs((current) => ({ ...current, [role.role]: value }))
            }}
          />
        </div>
      ))}

      {reserved.length > 0 && (
        <p className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {reserved.join(", ")} — the topology decides these, not the role. The
          plane refuses them.
        </p>
      )}

      <ReshapeNotice workload={workload} armed={arm.armed} />
    </FormDialog>
  )
}
