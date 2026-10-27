/**
 * Invented data for the three workbench views whose harnesses do not exist yet.
 *
 * This file is a design tool and nothing else. Conformance has no image built,
 * `agentic` is not a registered harness, and evalscope has never been pointed at
 * anything, so the ledger, the transcript and the score table cannot be judged
 * against real output. They are laid out against this instead.
 *
 * Two rules keep that from turning into a lie:
 *
 * - Every consumer renders `MockNotice` above it. A number on this screen that
 *   is not marked is a bug, not a placeholder.
 * - The shapes here are a proposal, not a contract. The plane stores whatever
 *   the harness emits, so when the real thing lands these types are replaced by
 *   what it actually produced instead of the harness being bent to fit them.
 *
 * Deterministic from the id, so a page does not reshuffle itself on every
 * render and two people looking at the same URL see the same table.
 */

import type { BenchmarkRequest, BenchmarkRun } from "./types"

/* ---- deterministic noise ----------------------------------------------- */

function seed(id: string): () => number {
  let value = 0
  for (let index = 0; index < id.length; index += 1) {
    value = (value * 31 + id.charCodeAt(index)) >>> 0
  }
  return () => {
    value = (value * 1664525 + 1013904223) >>> 0
    return value / 0x100000000
  }
}

function pick<T>(next: () => number, options: readonly T[]): T {
  return options[Math.floor(next() * options.length)]
}

/* ---- conformance -------------------------------------------------------- */

/**
 * Three-valued, and the third value is the point.
 *
 * A probe that could not be observed (the endpoint refused the parameter, the
 * suite skipped it) is not a pass. Folding it into either bucket makes the
 * tally a number nobody can defend in front of the person whose acceptance
 * document it is.
 */
export type ProbeOutcome = "agrees" | "differs" | "not-observed"

export interface ProbeRow {
  id: string
  group: string
  claim: string
  /** What our endpoint answered, collapsed to the shape the standard compares. */
  observed: string
  /** What the reference provider answered. Absent when no reference was set. */
  reference: string | null
  outcome: ProbeOutcome
  /** Raw bytes, for the row that has to be defended across a table. */
  exchange: string
}

const PROBE_CLAIMS: [string, string, string, string][] = [
  ["protocol", "chat.completions answers 200", "200", "200"],
  ["protocol", "completions answers 200", "200", "200"],
  ["protocol", "embeddings answers 404", "404", "404"],
  ["streaming", "usage position in the stream", "final-chunk", "final-chunk"],
  ["streaming", "role delta appears once", "first-chunk", "first-chunk"],
  ["streaming", "terminal [DONE] sentinel", "present", "present"],
  ["thinking", "reasoning on by default", "off", "on"],
  [
    "thinking",
    "reasoning block field path",
    "reasoning_content",
    "reasoning_content",
  ],
  ["tools", "tool_choice=required honored", "honored", "honored"],
  [
    "tools",
    "parallel tool calls emitted",
    "many-per-message",
    "one-per-message",
  ],
  ["tools", "arguments arrive as a JSON string", "string", "string"],
  ["structured", "json_schema strict rejects drift", "rejected", "rejected"],
  ["structured", "json_object without schema", "accepted", "accepted"],
  ["params", "temperature=1.1", "200", "400"],
  ["params", "top_p=0.5", "200", "400"],
  ["params", "presence_penalty=0.4", "200", "400"],
  ["params", "logprobs=true", "400", "400"],
  ["errors", "unknown model id", "404 model_not_found", "404 model_not_found"],
  ["errors", "missing credential", "401", "401"],
  ["errors", "malformed JSON body", "400", "400"],
  ["length", "max_tokens beyond the window", "400", "400"],
  ["length", "finish_reason at the cap", "length", "length"],
  ["eos", "stop sequence not echoed", "absent", "absent"],
  ["tokens", "prompt token accounting", "1284", "1284"],
  [
    "cache",
    "cached token field path",
    "prompt_tokens_details",
    "prompt_tokens_details",
  ],
  ["auth", "bearer in the wrong scheme", "401", "401"],
  ["multimodal", "image_url part accepted", "415", "200"],
]

export function mockProbes(id: string, withReference = true): ProbeRow[] {
  const next = seed(id)
  return PROBE_CLAIMS.map(([group, claim, observed, reference], index) => {
    const skipped = next() < 0.07
    const outcome: ProbeOutcome = skipped
      ? "not-observed"
      : observed === reference
        ? "agrees"
        : "differs"
    return {
      id: `${group}.${index}`,
      group,
      claim,
      observed: skipped ? "—" : observed,
      reference: withReference ? (skipped ? "—" : reference) : null,
      outcome: withReference ? outcome : skipped ? "not-observed" : "agrees",
      exchange: [
        `POST /v1/chat/completions`,
        `content-type: application/json`,
        ``,
        `{"model":"kimi-k2","messages":[{"role":"user","content":"ping"}]}`,
        ``,
        `--- 200 OK`,
        `{"id":"chatcmpl-${id.slice(0, 8)}${index}","object":"chat.completion",`,
        ` "choices":[{"index":0,"finish_reason":"stop"}],`,
        ` "usage":{"prompt_tokens":9,"completion_tokens":4,"total_tokens":13}}`,
      ].join("\n"),
    }
  })
}

export interface ProbeTally {
  agrees: number
  differs: number
  notObserved: number
  total: number
}

export function tally(rows: ProbeRow[]): ProbeTally {
  return {
    agrees: rows.filter((row) => row.outcome === "agrees").length,
    differs: rows.filter((row) => row.outcome === "differs").length,
    notObserved: rows.filter((row) => row.outcome === "not-observed").length,
    total: rows.length,
  }
}

/* ---- agent -------------------------------------------------------------- */

export interface AgentTurn {
  index: number
  role: "user" | "assistant" | "tool"
  /** The tool a turn called, when it called one. */
  tool: string | null
  text: string
  /** Milliseconds this turn took end to end. */
  ms: number
}

export interface AgentSession {
  id: string
  client: string
  task: string
  completed: boolean
  turns: number
  toolCalls: number
  retries: number
  ms: number
  /** The first thing that went wrong, verbatim. Null when nothing did. */
  failure: string | null
}

const CLIENTS = ["claude-code", "opencode", "aider"] as const
const TASKS = [
  "fix the failing test in the fixture repo",
  "add a flag to the CLI and update its help",
  "rename a symbol across three files",
  "summarize a 200k-token file and cite line numbers",
] as const
const FAILURES = [
  "tool_choice=required returned a message with no tool_calls",
  "stream truncated mid tool-call arguments",
  "assistant repeated the same edit four times",
  null,
  null,
  null,
] as const

export function mockSessions(id: string, count = 6): AgentSession[] {
  const next = seed(id)
  return Array.from({ length: count }, (_, index) => {
    const failure = pick(next, FAILURES)
    return {
      id: `${id.slice(0, 6)}-s${index + 1}`,
      client: pick(next, CLIENTS),
      task: pick(next, TASKS),
      completed: failure === null,
      turns: 4 + Math.floor(next() * 22),
      toolCalls: 2 + Math.floor(next() * 30),
      retries: Math.floor(next() * 3),
      ms: 8_000 + Math.floor(next() * 90_000),
      failure,
    }
  })
}

export function mockTranscript(id: string): AgentTurn[] {
  const next = seed(`${id}:transcript`)
  const script: [AgentTurn["role"], string | null, string][] = [
    ["user", null, "The test in fixtures/parser_test.go is failing. Fix it."],
    ["assistant", "bash", "go test ./fixtures/ -run TestParser"],
    [
      "tool",
      "bash",
      "--- FAIL: TestParser (0.00s)\n    parser_test.go:41: want 3, got 0",
    ],
    ["assistant", "read", "fixtures/parser.go"],
    ["tool", "read", "func Parse(s string) []Token { ... 84 lines ... }"],
    [
      "assistant",
      "edit",
      "fixtures/parser.go: the loop breaks before the final token",
    ],
    ["tool", "edit", "1 replacement applied"],
    ["assistant", "bash", "go test ./fixtures/ -run TestParser"],
    ["tool", "bash", "ok  \tfixtures\t0.004s"],
    [
      "assistant",
      null,
      "The loop exited one token early. Fixed and the test passes.",
    ],
  ]
  return script.map(([role, tool, text], index) => ({
    index,
    role,
    tool,
    text,
    ms: 200 + Math.floor(next() * 4_000),
  }))
}

/* ---- walkable rows ------------------------------------------------------ */

/**
 * Whole `BenchmarkRun` and `BenchmarkRequest` objects, so the three unbuilt
 * kinds can actually be clicked through.
 *
 * Without these the new views are unreachable: the plane registers three
 * harnesses, only `aiperf` has ever run, and a layout nobody can navigate to
 * has not been reviewed. These rows appear only behind the list page's Mock
 * switch, carry a `mock-` id that no plane object can collide with, and every
 * page that resolves one says so on the screen.
 */
export const MOCK_PREFIX = "mock-"

export function isMockId(id: string | undefined | null): boolean {
  return typeof id === "string" && id.startsWith(MOCK_PREFIX)
}

const MOCK_HARNESS: Record<string, string> = {
  conformance: "conformance",
  agent: "agentic",
  evaluation: "evalscope",
  performance: "aiperf",
}

/** The made-up id for one kind, stable so a bookmarked mock URL keeps working. */
export function mockRunId(kind: string, index = 0): string {
  return `${MOCK_PREFIX}run-${kind}-${index}`
}

export function mockRequestId(kind: string): string {
  return `${MOCK_PREFIX}req-${kind}`
}

function hoursAgo(hours: number): string {
  return new Date(Date.now() - hours * 3_600_000).toISOString()
}

export function mockRun(id: string): BenchmarkRun {
  const kind = id.split("-")[2] ?? "conformance"
  const next = seed(id)
  const harness = MOCK_HARNESS[kind] ?? "conformance"
  return {
    id,
    request_id: mockRequestId(kind),
    deployment_id: "dep-mock-0001",
    cluster_id: "usw2",
    endpoint_id: null,
    operation_id: null,
    harness,
    harness_version: "0.1.0",
    image: `mock/${harness}:0.1.0`,
    parameters:
      kind === "conformance"
        ? { suite: "all", reference_base_url: "api.moonshot.ai" }
        : kind === "agent"
          ? { agent: "claude-code", rounds: 6, max_turns: 40 }
          : { datasets: "gsm8k,math-500,humaneval", limit: 0 },
    step_index: 0,
    vantage: null,
    discard: false,
    requested_by: "mock",
    reason: "laid out before the harness exists",
    provenance: {
      model: "kimi-k2",
      deployment_generation: 7,
      serving_digest: "sha256:mock",
      engine_args: {},
      serving: {},
    },
    phase: "succeeded",
    summary: {},
    artifacts_uri: null,
    log_tail: null,
    error: null,
    created_at: hoursAgo(3 + next() * 40),
    updated_at: null,
    started_at: hoursAgo(3 + next() * 40),
    finished_at: hoursAgo(2 + next() * 30),
  }
}

export function mockRequest(id: string): BenchmarkRequest {
  const kind = id.split("-")[2] ?? "conformance"
  const run = mockRun(mockRunId(kind))
  return {
    id,
    // The plane refuses endpoint-level requests until that executor exists.
    level: kind === "conformance" ? "deployment" : "workload",
    target_id: kind === "conformance" ? "dep-mock-0001" : "wl-mock-0001",
    harness: run.harness,
    image: run.image,
    parameters: run.parameters,
    mode: "single",
    criteria_version: "2026.08",
    tier: null,
    homogeneous: true,
    coverage: [],
    requested_by: "mock",
    reason: run.reason,
    phase: "succeeded",
    runs: [run.id],
    created_at: run.created_at,
    updated_at: null,
  }
}

/** One finished run per unbuilt kind, newest first. */
export function mockRuns(): BenchmarkRun[] {
  return ["conformance", "agent", "evaluation"].map((kind) =>
    mockRun(mockRunId(kind))
  )
}
