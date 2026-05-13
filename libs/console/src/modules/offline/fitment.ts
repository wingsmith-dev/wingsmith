/**
 * The offline stand-in for `GET /artifacts/:ref/fitment`.
 *
 * The arithmetic here is a transcription of `app/core/vram.py`, not an
 * approximation of it — same bytes-per-param table, same KV formula, same 15%
 * overhead, same 0.9 utilization, same halving loop, same ranking. A fixture that
 * produced plausible-but-different numbers would make a broken console look right,
 * which is the one thing this module exists not to do.
 *
 * What it cannot reproduce is a HuggingFace config, so the shape comes from the
 * artifact's own persisted `metadata.shape` exactly as the plane's does. An offline
 * artifact without one returns `sizable: false`, which is also what the plane does.
 */

import type {
  ArtifactFit,
  ArtifactFitment,
  FitmentScope,
} from "@/modules/demand/types"
import type { Artifact } from "@/modules/demand/types"
import type { DeviceClass } from "@/modules/supply/types"

const BYTES_PER_PARAM: Record<string, number> = {
  fp16: 2,
  bf16: 2,
  fp8: 1,
  int8: 1,
  awq: 0.5,
  gptq: 0.5,
  int4: 0.5,
  mxfp4: 0.5,
  nvfp4: 0.5,
  fp4: 0.5,
}

const DEFAULT_QUANTIZATION = "fp16"
const KV_BYTES = 2
const GPU_MEMORY_UTILIZATION = 0.9
const FIXED_PER_DEVICE_GB = 1.0
const ACTIVATION_TENSORS = 4
const DEFAULT_BATCH_TOKENS = 8192
const DEFAULT_HIDDEN_SIZE = 4096
const GIB = 1024 ** 3
const MIB_PER_GIB = 1024
const MIN_CONTEXT_LENGTH = 4096
const DEFAULT_DEVICES_PER_NODE = 8
const MAX_DEVICES = 512
const DEFAULT_FITMENT_LIMIT = 5
const FALLBACK_CONTEXT_LENGTH = 8192

interface ModelShape {
  num_hidden_layers: number
  num_key_value_heads: number | null
  head_dim: number | null
  kv_lora_rank: number | null
  qk_rope_head_dim: number | null
  context_length: number | null
  num_attention_heads: number | null
  hidden_size: number | null
}

interface Sizing {
  total_params: number
  /** Null when the artifact published no config. Costs the KV cache, not the whole answer. */
  shape: ModelShape | null
  quantization: string
}

export interface FitmentQuery {
  scope?: string
  context_length?: number
  concurrency?: number
  quantization?: string
  devices_per_node?: number
  max_devices?: number
  limit?: number
}

interface Layout {
  device_count: number
  node_count: number
  tensor_parallel: number
  pipeline_parallel: number
}

/**
 * Tensor parallelism stays inside a node; pipeline crosses them over the fabric.
 *
 * Capped at the node rather than divided into it, so a 72-device NVL72 holds a tensor-16
 * replica as one node instead of pretending to be nine chassis.
 */
function layoutFor(deviceCount: number, devicesPerNode: number): Layout {
  const ceiling = Math.max(1, Math.min(deviceCount, devicesPerNode))
  let tensor = 1
  while (tensor * 2 <= ceiling && deviceCount % (tensor * 2) === 0) tensor *= 2
  return {
    device_count: deviceCount,
    node_count: Math.ceil(deviceCount / Math.max(1, devicesPerNode)),
    tensor_parallel: tensor,
    pipeline_parallel: deviceCount / tensor,
  }
}

function deviceCounts(maxDevices: number): number[] {
  const counts: number[] = []
  for (let count = 1; count <= Math.max(1, maxDevices); count *= 2)
    counts.push(count)
  return counts
}

function devicesWords(
  layout: Layout,
  device: DeviceClass,
  devicesPerNode: number
): string {
  if (layout.node_count <= 1)
    return `${layout.device_count}x ${device.label} in one node`
  const perNode = Math.min(devicesPerNode, layout.device_count)
  return `${layout.node_count} nodes of ${perNode}x ${device.label} (${layout.device_count} devices)`
}

function parallelWords(layout: Layout): string {
  if (layout.pipeline_parallel <= 1)
    return `tensor-parallel ${layout.tensor_parallel}`
  return `tensor-parallel ${layout.tensor_parallel} across pipeline-parallel ${layout.pipeline_parallel}`
}

function bytesPerParam(quantization: string): number {
  return (
    BYTES_PER_PARAM[quantization.toLowerCase()] ??
    BYTES_PER_PARAM[DEFAULT_QUANTIZATION]
  )
}

/** MLA stores one compressed latent per layer; standard attention stores key+value per KV head. */
function kvBytesPerToken(shape: ModelShape): number {
  if (shape.kv_lora_rank) {
    const latent = shape.kv_lora_rank + (shape.qk_rope_head_dim ?? 0)
    return shape.num_hidden_layers * latent * KV_BYTES
  }
  return (
    2 *
    shape.num_hidden_layers *
    (shape.num_key_value_heads ?? 0) *
    (shape.head_dim ?? 0) *
    KV_BYTES
  )
}

/** Declared where possible; the heads-times-head-dim identity fails for MLA. */
function hiddenSizeOf(shape: ModelShape): number {
  if (shape.hidden_size) return shape.hidden_size
  if (shape.num_attention_heads && shape.head_dim)
    return shape.num_attention_heads * shape.head_dim
  return DEFAULT_HIDDEN_SIZE
}

/** Scales with tokens in flight and hidden size, and with nothing else. */
function activationBytes(shape: ModelShape, batchTokens: number): number {
  return batchTokens * hiddenSizeOf(shape) * KV_BYTES * ACTIVATION_TENSORS
}

/**
 * What the model holds: weights, KV cache, activation peak. Not grossed up — the
 * utilization envelope is applied once, on the capacity side.
 */
function neededGb(
  sizing: Sizing,
  contextLength: number,
  concurrency: number,
  batchTokens: number
): number {
  const weights = sizing.total_params * bytesPerParam(sizing.quantization)
  const kv = sizing.shape
    ? kvBytesPerToken(sizing.shape) * contextLength * concurrency
    : 0
  const activations = sizing.shape
    ? activationBytes(sizing.shape, batchTokens)
    : 0
  return (weights + kv + activations) / GIB
}

/**
 * What a set of devices offers, net of the envelope and the per-device CUDA reserve.
 *
 * The reserve is per device because CUDA context is a cost per GPU: it grows with how
 * many a replica uses, not with how big the model is.
 */
function usableGb(memoryMib: number, deviceCount: number): number {
  const perDevice =
    (memoryMib / MIB_PER_GIB) * GPU_MEMORY_UTILIZATION - FIXED_PER_DEVICE_GB
  return Math.max(0, deviceCount * perDevice)
}

function weightsGb(sizing: Sizing): number {
  return (sizing.total_params * bytesPerParam(sizing.quantization)) / GIB
}

/** Read back from the artifact rather than derived, the same way the plane reads it. */
function sizingOf(artifact: Artifact, quantization?: string): Sizing | null {
  if (!artifact.parameter_count) return null
  const quant =
    quantization ||
    (artifact.metadata?.quantization as string | undefined) ||
    DEFAULT_QUANTIZATION
  const stored = artifact.metadata?.shape as Partial<ModelShape> | undefined
  if (!stored?.num_hidden_layers)
    return {
      total_params: artifact.parameter_count,
      shape: null,
      quantization: quant,
    }
  return {
    total_params: artifact.parameter_count,
    shape: {
      num_hidden_layers: stored.num_hidden_layers,
      num_key_value_heads: stored.num_key_value_heads ?? null,
      head_dim: stored.head_dim ?? null,
      kv_lora_rank: stored.kv_lora_rank ?? null,
      qk_rope_head_dim: stored.qk_rope_head_dim ?? null,
      context_length: stored.context_length ?? null,
      num_attention_heads: stored.num_attention_heads ?? null,
      hidden_size: stored.hidden_size ?? null,
    },
    quantization: quant,
  }
}

/** Tensor parallelism has to divide the heads evenly; no amount of memory fixes it. */
function shardableHeads(
  shape: ModelShape | null,
  deviceCount: number
): boolean {
  const heads = shape?.num_attention_heads
  if (!heads || deviceCount <= 1) return true
  return heads % deviceCount === 0
}

function fitAt(
  sizing: Sizing,
  device: DeviceClass,
  contextLength: number,
  concurrency: number,
  deviceCount: number,
  devicesPerNode: number,
  batchTokens: number = DEFAULT_BATCH_TOKENS
): ArtifactFit {
  const aggregate = usableGb(device.memory_mib, deviceCount)
  const weights = weightsGb(sizing)
  const layout = layoutFor(deviceCount, devicesPerNode)
  const words = devicesWords(layout, device, devicesPerNode)
  const base = {
    device_class: device.name,
    label: device.label,
    device_count: deviceCount,
    memory_mib_per_device: device.memory_mib,
    aggregate_gb: aggregate,
    weights_gb: weights,
    devices_total: device.inventory.devices,
    supply_short: false,
    devices_allocatable: device.inventory.allocatable,
    cluster_count: device.inventory.clusters.length,
    in_catalog: device.in_catalog,
    kv_counted: sizing.shape !== null,
    node_count: layout.node_count,
    devices_per_node: devicesPerNode,
    tensor_parallel: layout.tensor_parallel,
    pipeline_parallel: layout.pipeline_parallel,
  }

  if (device.memory_mib <= 0)
    return {
      ...base,
      fits: false,
      reduced: false,
      required_gb: 0,
      context_length: contextLength,
      reason: `the catalogue carries no memory size for ${device.name}, so it cannot be fitted against`,
    }

  if (!shardableHeads(sizing.shape, layout.tensor_parallel))
    return {
      ...base,
      fits: false,
      reduced: false,
      required_gb: weights,
      context_length: contextLength,
      reason: `${sizing.shape?.num_attention_heads} attention heads do not divide evenly across a tensor-parallel group of ${layout.tensor_parallel}`,
    }

  if (weights > aggregate)
    return {
      ...base,
      fits: false,
      reduced: false,
      required_gb: weights,
      context_length: contextLength,
      reason:
        `the weights need ${Math.round(weights)} GB at ${sizing.quantization} and ` +
        `${words} offers ${Math.round(aggregate)} GB usable`,
    }

  if (!sizing.shape) {
    const usable = aggregate
    return {
      ...base,
      fits: true,
      reduced: false,
      required_gb: weights,
      context_length: 0,
      reason:
        `needs ${words} to hold ${Math.round(weights)} GB of weights in ${Math.round(usable)} GB usable, ` +
        `${parallelWords(layout)}; no context window is costed, so the real requirement is higher`,
    }
  }

  let window = contextLength || FALLBACK_CONTEXT_LENGTH
  let need = neededGb(sizing, window, concurrency, batchTokens)
  while (need > aggregate && window > MIN_CONTEXT_LENGTH) {
    window = Math.max(MIN_CONTEXT_LENGTH, Math.floor(window / 2))
    need = neededGb(sizing, window, concurrency, batchTokens)
  }
  const reduced = window < contextLength
  const fits = need <= aggregate

  if (!fits)
    return {
      ...base,
      fits: false,
      reduced,
      required_gb: need,
      context_length: window,
      reason:
        `${words} holds the weights but not a usable context: ` +
        `${window.toLocaleString()} tokens needs ${Math.round(need)} GB of ${Math.round(aggregate)} GB`,
    }

  return {
    ...base,
    fits: true,
    reduced,
    required_gb: need,
    context_length: window,
    reason: reduced
      ? `needs ${words} at ${window.toLocaleString()} tokens rather than the ${contextLength.toLocaleString()} asked for, ${parallelWords(layout)}`
      : `needs ${words} at the full ${window.toLocaleString()} tokens, ${parallelWords(layout)}`,
  }
}

/** Smallest count carrying the full window, else the smallest carrying any, else the widest attempt. */
function fit(
  sizing: Sizing,
  device: DeviceClass,
  contextLength: number,
  concurrency: number,
  maxDevices: number,
  devicesPerNode: number
): ArtifactFit {
  const attempts: ArtifactFit[] = []
  for (const count of deviceCounts(maxDevices)) {
    const attempt = fitAt(
      sizing,
      device,
      contextLength,
      concurrency,
      count,
      devicesPerNode
    )
    if (attempt.fits && !attempt.reduced) return short(attempt)
    attempts.push(attempt)
  }
  const chosen = attempts.find((a) => a.fits) ?? attempts[attempts.length - 1]
  return short(chosen)
}

/**
 * The fleet half saying no: a fit on a class we own too few of to run one replica.
 * Mirrors `_short` in the plane, and is set on the chosen attempt rather than on
 * every attempt, because the count it is measured against is the chosen one.
 */
function short(fit: ArtifactFit): ArtifactFit {
  return {
    ...fit,
    supply_short:
      fit.fits && fit.devices_total > 0 && fit.devices_total < fit.device_count,
  }
}

export function fitment(
  artifact: Artifact,
  classes: DeviceClass[],
  query: FitmentQuery,
  nodes?: Record<string, number>
): ArtifactFitment {
  const scope = (query.scope ?? "fleet") as FitmentScope
  const concurrency = query.concurrency ?? 1
  const maxDevices = Math.min(query.max_devices ?? MAX_DEVICES, MAX_DEVICES)
  const sizing = sizingOf(artifact, query.quantization)

  const declared = artifact.metadata?.context_length as number | undefined
  const contextLength =
    query.context_length ||
    declared ||
    sizing?.shape?.context_length ||
    FALLBACK_CONTEXT_LENGTH

  const header = {
    artifact_id: artifact.id,
    scope,
    context_length: contextLength,
    concurrency,
    max_devices: maxDevices,
  }

  if (!sizing)
    return {
      ...header,
      sizable: false,
      considered: 0,
      quantization: query.quantization || DEFAULT_QUANTIZATION,
      note:
        "this artifact publishes no parameter count, so not even its weights can be " +
        "sized; re-resolving its source records what a fit needs",
      items: [],
    }

  const candidates =
    scope === "fleet"
      ? classes.filter((entry) => entry.inventory.devices > 0)
      : classes
  const items = candidates
    .map((device) =>
      fit(
        sizing,
        device,
        contextLength,
        concurrency,
        maxDevices,
        nodes?.[device.name] ||
          query.devices_per_node ||
          DEFAULT_DEVICES_PER_NODE
      )
    )
    .sort(
      (a, b) =>
        Number(!a.fits) - Number(!b.fits) ||
        Number(a.supply_short) - Number(b.supply_short) ||
        Number(a.reduced) - Number(b.reduced) ||
        a.node_count - b.node_count ||
        a.device_count - b.device_count ||
        a.aggregate_gb - b.aggregate_gb ||
        a.device_class.localeCompare(b.device_class)
    )

  const limit = query.limit === undefined ? DEFAULT_FITMENT_LIMIT : query.limit
  return {
    ...header,
    sizable: true,
    considered: items.length,
    quantization: sizing.quantization,
    note: !items.length
      ? scope === "fleet"
        ? "no cluster has reported a device yet, so there is no fleet to fit against"
        : "the device table could not be read, so there are no classes to fit against"
      : sizing.shape === null
        ? "this artifact publishes no attention shape, so these rows size the weights only " +
          "and no context window is costed — every requirement shown is a floor. Re-resolving " +
          "its source records the shape and prices the window."
        : null,
    items: limit <= 0 ? items : items.slice(0, limit),
  }
}
