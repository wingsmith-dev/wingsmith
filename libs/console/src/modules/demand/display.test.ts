import { describe, expect, it } from "vitest"

import { resolvedEngineArgs, servingCompanionArgs } from "./display"

import type { ServingBehaviour } from "./types"

const serving = (over: Partial<ServingBehaviour> = {}): ServingBehaviour =>
  ({
    reasoning: {
      parser: null,
      default_on: false,
      transport: "reasoning_content",
    },
    stream_usage: "final_chunk",
    context_overflow: "reject",
    structured_output_backend: null,
    tool_call_parser: null,
    max_images_per_request: null,
    ...over,
  }) as ServingBehaviour

describe("companion flags", () => {
  it("gives a vLLM tool parser the flag that makes it run", () => {
    // vLLM parses tool calls with one flag and dispatches them with another. Set
    // only the parser and the engine loads something it never runs, so the workload
    // reads as configured while the endpoint returns the call as prose.
    expect(
      resolvedEngineArgs("vllm", {}, serving({ tool_call_parser: "openai" }))
    ).toEqual({
      "tool-call-parser": "openai",
      "enable-auto-tool-choice": "",
    })
  })

  it("adds nothing on an engine that dispatches on the parser alone", () => {
    expect(
      resolvedEngineArgs("sglang", {}, serving({ tool_call_parser: "qwen25" }))
    ).toEqual({
      "tool-call-parser": "qwen25",
    })
  })

  it("leaves an explicitly declared companion flag alone", () => {
    const resolved = resolvedEngineArgs(
      "vllm",
      { "--enable-auto-tool-choice": "true" },
      serving({ tool_call_parser: "openai" })
    )
    expect(resolved["enable-auto-tool-choice"]).toBe("true")
    expect(resolved["tool-call-parser"]).toBe("openai")
  })

  it("stays inert on a serving block that declares nothing", () => {
    expect(servingCompanionArgs("vllm", serving())).toEqual({})
    expect(
      resolvedEngineArgs("vllm", { "--dtype": "bfloat16" }, serving())
    ).toEqual({
      dtype: "bfloat16",
    })
  })
})
