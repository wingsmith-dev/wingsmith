import { describe, expect, it } from "vitest"

import { artifacts, deviceClasses } from "./data"
import { fitment } from "./fitment"

const llama = artifacts[0]
const embed = artifacts[1]

describe("offline fitment", () => {
  it("ranks the cheapest workable class first", () => {
    const result = fitment(llama, deviceClasses(), {
      scope: "catalog",
      limit: 0,
    })
    expect(result.sizable).toBe(true)
    expect(result.items[0].fits).toBe(true)

    const fits = result.items.filter((item) => item.fits)
    expect(fits[0].device_count).toBeLessThanOrEqual(
      fits[fits.length - 1].device_count
    )
  })

  it("keeps no-fits at the bottom rather than dropping them", () => {
    const result = fitment(llama, deviceClasses(), {
      scope: "catalog",
      limit: 0,
    })
    const flags = result.items.map((item) => item.fits)
    expect(flags).toEqual([...flags].sort((a, b) => Number(b) - Number(a)))
  })

  it("marks a fit the fleet holds too few cards for, and sinks it", () => {
    const classes = deviceClasses().map((entry) =>
      entry.inventory.devices > 0
        ? {
            ...entry,
            inventory: { ...entry.inventory, devices: 1, allocatable: 1 },
          }
        : entry
    )
    const result = fitment(llama, classes, { scope: "fleet", limit: 0 })

    const short = result.items.filter((item) => item.supply_short)
    expect(short.length).toBeGreaterThan(0)
    for (const item of short) {
      expect(item.fits).toBe(true)
      expect(item.device_count).toBeGreaterThan(item.devices_total)
    }
    const flags = result.items
      .filter((item) => item.fits)
      .map((item) => item.supply_short)
    expect(flags).toEqual([...flags].sort((a, b) => Number(a) - Number(b)))
  })

  it("asks a different question in each scope", () => {
    const fleet = fitment(llama, deviceClasses(), { scope: "fleet", limit: 0 })
    const catalog = fitment(llama, deviceClasses(), {
      scope: "catalog",
      limit: 0,
    })

    expect(catalog.items.length).toBeGreaterThan(fleet.items.length)
    expect(fleet.items.every((item) => item.devices_total > 0)).toBe(true)
    expect(catalog.items.some((item) => item.devices_total === 0)).toBe(true)
  })

  it("halves the weights when asked about a smaller precision", () => {
    const native = fitment(llama, deviceClasses(), {})
    const quantized = fitment(llama, deviceClasses(), { quantization: "int4" })

    expect(native.quantization).toBe("fp16")
    expect(quantized.quantization).toBe("int4")
    expect(quantized.items[0].weights_gb).toBeCloseTo(
      native.items[0].weights_gb / 4,
      1
    )
  })

  it("offers a truncated window rather than calling it a failure", () => {
    const result = fitment(llama, deviceClasses(), {
      scope: "catalog",
      concurrency: 256,
      limit: 0,
      max_devices: 8,
    })
    const reduced = result.items.filter((item) => item.fits && item.reduced)

    expect(reduced.length).toBeGreaterThan(0)
    expect(reduced[0].context_length).toBeLessThan(result.context_length)
    expect(reduced[0].reason).toContain("rather than")
  })

  it("defaults to the artifact's own declared window", () => {
    expect(fitment(llama, deviceClasses(), {}).context_length).toBe(131_072)
    expect(
      fitment(llama, deviceClasses(), { context_length: 8192 }).context_length
    ).toBe(8192)
  })

  it("gives a shapeless artifact a matrix of floors, not a blank panel", () => {
    const result = fitment(embed, deviceClasses(), {})

    expect(result.sizable).toBe(true)
    expect(result.items.length).toBeGreaterThan(0)
    expect(result.items.every((item) => !item.kv_counted)).toBe(true)
    expect(result.items.every((item) => item.context_length === 0)).toBe(true)
    expect(
      result.items.every((item) => item.required_gb === item.weights_gb)
    ).toBe(true)
    expect(result.note).toContain("floor")
  })

  it("reports an artifact with no parameter count instead of an empty table", () => {
    const result = fitment(
      { ...embed, parameter_count: null },
      deviceClasses(),
      {}
    )
    expect(result.sizable).toBe(false)
    expect(result.items).toEqual([])
    expect(result.note).toContain("no parameter count")
  })

  it("counts the cache and stays quiet when the shape is there", () => {
    const result = fitment(llama, deviceClasses(), {})
    expect(result.items.every((item) => item.kv_counted)).toBe(true)
    expect(result.note).toBeNull()
  })

  it("carries each class's own memory rather than the catalog's", () => {
    const classes = deviceClasses()
    const result = fitment(llama, classes, { scope: "fleet", limit: 0 })

    for (const item of result.items) {
      const source = classes.find((entry) => entry.name === item.device_class)
      expect(item.memory_mib_per_device).toBe(source?.memory_mib)
      expect(item.in_catalog).toBe(source?.in_catalog)
    }
  })

  it("cannot fit a class nothing has reported a memory size for", () => {
    const unknown = {
      ...deviceClasses()[0],
      name: "some-new-accelerator",
      label: "some-new-accelerator",
      memory_mib: 0,
      in_catalog: false,
    }
    const result = fitment(llama, [unknown], { scope: "catalog" })

    expect(result.items[0].fits).toBe(false)
    expect(result.items[0].reason).toContain("no memory size")
  })

  it("returns the ranked head and says how many it ranked", () => {
    const capped = fitment(llama, deviceClasses(), { scope: "catalog" })
    const every = fitment(llama, deviceClasses(), {
      scope: "catalog",
      limit: 0,
    })

    expect(capped.items.length).toBe(5)
    expect(capped.considered).toBe(every.considered)
    expect(capped.considered).toBeGreaterThan(capped.items.length)
    expect(capped.items.map((i) => i.device_class)).toEqual(
      every.items.slice(0, 5).map((i) => i.device_class)
    )
  })

  it("answers a frontier model in nodes rather than refusing it", () => {
    const kimi = {
      ...llama,
      parameter_count: 2_800_000_000_000,
      metadata: { quantization: "fp8" },
    }
    const result = fitment(kimi, deviceClasses(), {
      scope: "catalog",
      limit: 0,
    })
    const b300 = result.items.find((i) => i.device_class === "b300-288gb-sxm")

    expect(b300?.fits).toBe(true)
    expect(b300?.device_count).toBe(16)
    expect(b300?.node_count).toBe(2)
    expect(b300?.tensor_parallel).toBe(8)
    expect(b300?.pipeline_parallel).toBe(2)
  })

  it("treats an NVL72 rack as one node rather than nine chassis", () => {
    const kimi = {
      ...llama,
      parameter_count: 2_800_000_000_000,
      metadata: { quantization: "fp8" },
    }
    const rack = fitment(kimi, deviceClasses(), {
      scope: "catalog",
      devices_per_node: 72,
      limit: 0,
    })
    const b300 = rack.items.find((i) => i.device_class === "b300-288gb-sxm")

    expect(b300?.device_count).toBe(16)
    expect(b300?.node_count).toBe(1)
    expect(b300?.pipeline_parallel).toBe(1)
    expect(b300?.tensor_parallel).toBe(16)
  })

  it("applies the utilization envelope once, on the capacity side", () => {
    const result = fitment(llama, deviceClasses(), { scope: "fleet", limit: 0 })
    const h100 = result.items.find((i) => i.device_class === "h100-80gb-sxm")

    // Usable, not nameplate: net of the 0.9 envelope and 1 GiB per device.
    const nameplate = (h100!.device_count * h100!.memory_mib_per_device) / 1024
    expect(h100!.aggregate_gb).toBeLessThan(nameplate * 0.9)
    expect(h100!.aggregate_gb).toBeGreaterThan(nameplate * 0.8)
  })

  it("matches the plane on the case the compounded overhead was refusing", () => {
    const qwen = {
      ...llama,
      parameter_count: 27_781_427_952,
      metadata: {
        quantization: "fp16",
        context_length: 262_144,
        shape: {
          num_hidden_layers: 64,
          num_key_value_heads: 4,
          head_dim: 256,
          kv_lora_rank: null,
          qk_rope_head_dim: null,
          context_length: 262_144,
          num_attention_heads: 24,
          hidden_size: 6144,
        },
      },
    }
    const at = (ctx: number) =>
      fitment(qwen, deviceClasses(), {
        scope: "catalog",
        context_length: ctx,
        limit: 0,
      }).items.find((i) => i.device_class === "rtx-6000-ada-48gb-pcie")

    expect(at(131_072)?.device_count).toBe(2)
    expect(at(262_144)?.device_count).toBe(4)
  })
})
