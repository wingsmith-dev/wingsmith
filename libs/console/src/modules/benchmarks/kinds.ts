import type { StatusTone } from "@/components/layout/status-dot"

/**
 * What a run is *asking*, one level above which harness answers it.
 *
 * A harness is an implementation: aiperf, evalscope, a probe suite. A kind is
 * the question, and it is what somebody scanning this list is filtering by:
 * they came to find out whether the model is fast, whether it behaves like the
 * provider it claims to match, whether it still gets the answers right, or
 * whether a real client can drive it. Two harnesses answering the same question
 * belong under one filter; one harness never spans two.
 *
 * The mapping lives here instead of on the plane only because the plane's
 * harness table has no `kind` field yet. It should get one (the console is
 * currently deciding what a harness means, which is the plane's call), and this
 * table becomes a fallback for a harness registered before that field existed.
 */
export type RunKind =
  "performance" | "conformance" | "evaluation" | "agent" | "other"

export interface RunKindMeta {
  kind: RunKind
  label: string
  /** The question, in the words somebody would use to ask it out loud. */
  question: string
  tone: StatusTone
  /** The summary key this kind's headline column reads, when it has one. */
  headline: { key: string; header: string } | null
}

export const RUN_KINDS: Record<RunKind, RunKindMeta> = {
  performance: {
    kind: "performance",
    label: "Performance",
    question: "How fast is it, and where does it stop scaling?",
    tone: "info",
    headline: { key: "time_to_first_token", header: "TTFT p50" },
  },
  conformance: {
    kind: "conformance",
    label: "Conformance",
    question: "Does it behave the way the provider it claims to match behaves?",
    tone: "warn",
    headline: null,
  },
  evaluation: {
    kind: "evaluation",
    label: "Evaluation",
    question: "Does it still get the answers right?",
    tone: "ok",
    headline: null,
  },
  agent: {
    kind: "agent",
    label: "Agent",
    question: "Can a real client drive it through a multi-turn tool loop?",
    tone: "neutral",
    headline: null,
  },
  other: {
    kind: "other",
    label: "Other",
    question: "A harness this console has not been taught to group.",
    tone: "neutral",
    headline: null,
  },
}

/** Display order: fastest question to slowest, which is also how often each is asked. */
export const RUN_KIND_ORDER: RunKind[] = [
  "performance",
  "conformance",
  "agent",
  "evaluation",
]

const BY_HARNESS: Record<string, RunKind> = {
  aiperf: "performance",
  "vllm-bench": "performance",
  guidellm: "performance",
  conformance: "conformance",
  evalscope: "evaluation",
  lm_eval: "evaluation",
  agentic: "agent",
}

export function runKind(harness: string): RunKind {
  return BY_HARNESS[harness] ?? "other"
}

export function runKindMeta(harness: string): RunKindMeta {
  return RUN_KINDS[runKind(harness)]
}
