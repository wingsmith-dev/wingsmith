import type { StatusTone } from "@/components/layout/status-dot"
import type { DiscoveryStatus } from "./types"

const STATUS_TONES: Record<DiscoveryStatus, StatusTone> = {
  new: "warn",
  tracked: "info",
  ignored: "neutral",
  dismissed: "neutral",
  promoted: "ok",
}

export function discoveryStatusTone(status: string): StatusTone {
  return STATUS_TONES[status as DiscoveryStatus] ?? "neutral"
}

/** `namespace/name`, which is how an operator refers to it in the cluster. */
export function workloadLabel(workload: {
  namespace: string
  name: string
}): string {
  return `${workload.namespace}/${workload.name}`
}

/** The tag off the first image, which is the part anyone reads. */
export function imageTag(images: string[]): string | null {
  const image = images[0]
  if (!image) return null
  const slash = image.lastIndexOf("/")
  return slash === -1 ? image : image.slice(slash + 1)
}
