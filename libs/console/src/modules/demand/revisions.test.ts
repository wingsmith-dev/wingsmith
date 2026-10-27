import { describe, expect, it } from "vitest"

import {
  ABSENT,
  changesAgainstPrevious,
  isDeclared,
  liveDigests,
  servingChanges,
} from "./revisions"
import type { WorkloadRevision, WorkloadServingUpdate } from "./types"

/**
 * A snapshot stated as the few fields a case is about.
 *
 * Cast rather than filled in: the blocks are whole-or-nothing on the wire, so a literal naming
 * one field of `shape` does not typecheck — and spelling out every sibling would bury the one
 * field each case exists to move.
 */
function body(fields: Record<string, unknown>): WorkloadServingUpdate {
  return fields as WorkloadServingUpdate
}

function revision(over: Partial<WorkloadRevision> = {}): WorkloadRevision {
  return {
    id: "wkr_01",
    number: 1,
    serving_digest: "svg_aaa",
    artifact: "art_01",
    reason: "created",
    note: null,
    created_at: "2026-09-01T00:00:00Z",
    serving: {},
    ...over,
  } as WorkloadRevision
}

describe("servingChanges", () => {
  it("names a scalar that moved by the path a patch would use", () => {
    expect(
      servingChanges(
        body({ context_length: 8192 }),
        body({ context_length: 16_384 })
      )
    ).toEqual([{ path: "context_length", before: "8192", after: "16384" }])
  })

  it("recurses into the blocks rather than reporting the block as one change", () => {
    expect(
      servingChanges(
        body({ parallelism: { tensor: 2, pipeline: 1 } }),
        body({ parallelism: { tensor: 8, pipeline: 1 } })
      )
    ).toEqual([{ path: "parallelism.tensor", before: "2", after: "8" }])
  })

  it("recurses into engine_args, where the flag that moved is the whole answer", () => {
    expect(
      servingChanges(
        body({ engine_args: { "max-num-seqs": "256" } }),
        body({ engine_args: { "max-num-seqs": "512" } })
      )
    ).toEqual([
      { path: "engine_args.max-num-seqs", before: "256", after: "512" },
    ])
  })

  it("reads a field the older revision never carried as absent, not as a value", () => {
    expect(servingChanges(body({}), body({ quantization: "fp8" }))).toEqual([
      { path: "quantization", before: ABSENT, after: "fp8" },
    ])
    expect(servingChanges(body({ quantization: "fp8" }), body({}))).toEqual([
      { path: "quantization", before: "fp8", after: ABSENT },
    ])
  })

  it("treats an explicit null and an absent field as the same thing", () => {
    expect(servingChanges(body({ quantization: null }), body({}))).toEqual([])
  })

  it("says nothing about a configuration that did not move", () => {
    const stated = { context_length: 8192, shape: { device_class: "h100" } }
    expect(servingChanges(body(stated), body({ ...stated }))).toEqual([])
  })

  it("compares arrays whole, because the blocks replace whole", () => {
    expect(
      servingChanges(
        body({ capabilities: { input_modalities: ["text"] } }),
        body({ capabilities: { input_modalities: ["text", "image"] } })
      )
    ).toEqual([
      {
        path: "capabilities.input_modalities",
        before: '["text"]',
        after: '["text","image"]',
      },
    ])
  })
})

describe("changesAgainstPrevious", () => {
  const history = [
    revision({ number: 3, serving: body({ context_length: 32_768 }) }),
    revision({ number: 2, serving: body({ context_length: 16_384 }) }),
    revision({ number: 1, serving: body({ context_length: 8192 }) }),
  ]

  it("diffs against the entry below it in a newest-first list", () => {
    expect(changesAgainstPrevious(history, 0)).toEqual([
      { path: "context_length", before: "16384", after: "32768" },
    ])
  })

  it("gives the oldest entry no diff, because a starting point is not a change", () => {
    expect(changesAgainstPrevious(history, 2)).toEqual([])
  })
})

describe("isDeclared", () => {
  it("matches on the digest, so a configuration returned to is declared again", () => {
    expect(isDeclared(revision({ serving_digest: "svg_aaa" }), "svg_aaa")).toBe(
      true
    )
    expect(isDeclared(revision({ serving_digest: "svg_bbb" }), "svg_aaa")).toBe(
      false
    )
  })
})

describe("liveDigests", () => {
  it("sums ready replicas per configuration across every cluster", () => {
    expect(
      liveDigests([
        { live_digests: { svg_aaa: 2, svg_bbb: 1 } },
        { live_digests: { svg_aaa: 3 } },
      ])
    ).toEqual({ svg_aaa: 5, svg_bbb: 1 })
  })

  it("survives a cluster that has reported no digest at all", () => {
    expect(liveDigests([{ live_digests: {} }])).toEqual({})
  })
})
