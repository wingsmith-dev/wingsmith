import colors from "tailwindcss/colors"
import { describe, expect, it } from "vitest"
import css from "./index.css?raw"

/**
 * DESIGN.md commits the console to WCAG 2.1 AA text contrast (4.5:1). This
 * measures the theme tokens in index.css on the surfaces text actually sits on,
 * in both themes, and scans the source for the ways around them: fading text
 * with an opacity modifier or `opacity-*`, and raw palette text colors that
 * were never measured.
 */
const sources = import.meta.glob<string>(
  ["./**/*.{ts,tsx}", "!./**/*.test.{ts,tsx}", "!./routeTree.gen.ts"],
  { query: "?raw", import: "default", eager: true }
)

const AA = 4.5

type Rgb = [number, number, number]

/** oklch → gamma-encoded sRGB, clipped to gamut the way browsers paint it. */
function oklch(value: string): Rgb {
  const match = /oklch\(\s*([\d.]+)(%?)\s+([\d.]+)\s+([\d.]+)/.exec(value)
  if (!match) throw new Error(`not an oklch color: ${value}`)
  const l = Number(match[1]) / (match[2] ? 100 : 1)
  const c = Number(match[3])
  const h = (Number(match[4]) * Math.PI) / 180
  const a = c * Math.cos(h)
  const b = c * Math.sin(h)
  const L = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const M = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const S = (l - 0.0894841775 * a - 1.291485548 * b) ** 3
  const linear = [
    4.0767416621 * L - 3.3077115913 * M + 0.2309699292 * S,
    -1.2684380046 * L + 2.6097574011 * M - 0.3413193965 * S,
    -0.0041960863 * L - 0.7034186147 * M + 1.707614701 * S,
  ]
  return linear.map((v) => {
    const x = Math.min(1, Math.max(0, v))
    return x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055
  }) as Rgb
}

/** `bg-x/NN` over a surface: alpha compositing in sRGB, as the browser does. */
function over(top: Rgb, alpha: number, under: Rgb): Rgb {
  return top.map((v, i) => v * alpha + under[i] * (1 - alpha)) as Rgb
}

function luminance(rgb: Rgb): number {
  const [r, g, b] = rgb.map((v) =>
    v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
  )
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

function tokens(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`)
  const body = css.slice(start, css.indexOf("}", start))
  return Object.fromEntries(
    [...body.matchAll(/--([\w-]+):\s*(oklch\([^)]*\))/g)].map((m) => [
      m[1],
      m[2],
    ])
  )
}

const LIGHT = tokens(":root")
const THEMES = { light: LIGHT, dark: { ...LIGHT, ...tokens(".dark") } }

/** Tailwind's own palette, the one `text-sky-700` and `bg-amber-500/10` resolve to. */
function palette(hue: string, shade: string): Rgb {
  const scale = (colors as unknown as Record<string, Record<string, string>>)[
    hue
  ]
  return oklch(scale[shade])
}

const SOLID = [
  "background",
  "card",
  "popover",
  "muted",
  "secondary",
  "accent",
  "sidebar",
  "sidebar-accent",
]
const TONES = ["primary", "destructive", "warning", "success"]
/** The raw palette washes that tone text sits on. */
const TONE_WASH: Record<string, string> = {
  warning: "amber",
  success: "emerald",
}

function surfaces(theme: Record<string, string>) {
  const solid = Object.fromEntries(SOLID.map((s) => [s, oklch(theme[s])]))
  const tint = (top: Rgb, name: string, alphas: number[]) =>
    Object.fromEntries(
      alphas.flatMap((alpha) =>
        ["background", "card"].map((under) => [
          `${name}/${alpha * 100} on ${under}`,
          over(top, alpha, solid[under]),
        ])
      )
    )
  const tints: Record<string, Record<string, Rgb>> = {}
  for (const tone of TONES) {
    tints[tone] = {
      ...tint(oklch(theme[tone]), tone, [0.05, 0.1, 0.15]),
      ...(TONE_WASH[tone] &&
        tint(
          palette(TONE_WASH[tone], "500"),
          `${TONE_WASH[tone]}-500`,
          [0.05, 0.1, 0.15]
        )),
    }
  }
  return { solid, tints }
}

function failures(
  text: Rgb,
  backgrounds: Record<string, Rgb>,
  label: string
): string[] {
  return Object.entries(backgrounds)
    .map(([name, bg]) => [name, contrast(text, bg)] as const)
    .filter(([, ratio]) => ratio < AA)
    .map(([name, ratio]) => `${label} on ${name}: ${ratio.toFixed(2)}`)
}

/** Every `text-{hue}-{shade}` in the source, split by theme. */
function rawTextColors() {
  const light = new Set<string>()
  const dark = new Set<string>()
  const unpaired: string[] = []
  const hues = Object.keys(colors).join("|")
  const pattern = new RegExp(
    `(?<![\\w-])((?:[a-z-]+:)*)text-(${hues})-(\\d{2,3})(?![\\w/-])`,
    "g"
  )
  for (const [file, text] of Object.entries(sources)) {
    text.split("\n").forEach((line, i) => {
      const found = [...line.matchAll(pattern)]
      for (const [, variants, hue, shade] of found) {
        ;(variants.includes("dark:") ? dark : light).add(`${hue}-${shade}`)
      }
      const lightHues = found.filter((m) => !m[1].includes("dark:"))
      const darkHues = new Set(
        found.filter((m) => m[1].includes("dark:")).map((m) => m[2])
      )
      for (const m of lightHues) {
        if (!darkHues.has(m[2])) unpaired.push(`${file}:${i + 1} ${m[0]}`)
      }
    })
  }
  return { light, dark, unpaired }
}

/**
 * `opacity-*` between 0 and 100 fades whatever text is inside. These are the
 * places it dims something with no text in it. Disabled controls are exempt
 * under WCAG and are not listed: any variant chain naming "disabled" passes.
 */
const OPACITY_ALLOWED: [string, string][] = [
  ["<Icon", "a decorative icon"],
  ["animate-ping", "the status dot's pulse ring"],
  ['cn(fill, "opacity-10")', "a sparkline's area fill"],
  ['cn(stroke, "opacity-80")', "a sparkline's stroke"],
  ['cn(FILL[drawn[0].tone], "opacity-10")', "a sparkline's area fill"],
  ['cn(STROKE[curve.tone], "opacity-80")', "a sparkline's stroke"],
  ['dimmed && "opacity-20"', "a device brick, which carries no text"],
  ['origin === "reserved" && "opacity-40"', "a slice swatch"],
  ['fit && !fit.ok && "opacity-50"', "the fabric row's bricks, not its text"],
]

describe("text contrast", () => {
  it("scans the source tree and the theme", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50)
    expect(Object.keys(LIGHT).length).toBeGreaterThan(20)
    expect(THEMES.dark.background).not.toEqual(LIGHT.background)
  })

  it.each(Object.entries(THEMES))(
    "holds 4.5:1 for every text token in the %s theme",
    (_, theme) => {
      const { solid, tints } = surfaces(theme)
      const allTints = Object.assign({}, ...Object.values(tints))
      const hits = [
        ...["foreground", "muted-foreground"].flatMap((t) =>
          failures(oklch(theme[t]), { ...solid, ...allTints }, t)
        ),
        ...TONES.flatMap((t) =>
          failures(oklch(theme[t]), { ...solid, ...tints[t] }, t)
        ),
        ...[
          ["primary-foreground", "primary"],
          ["destructive-foreground", "destructive"],
          ["sidebar-primary-foreground", "sidebar-primary"],
        ].flatMap(([fg, bg]) =>
          failures(oklch(theme[fg]), { [bg]: oklch(theme[bg]) }, fg)
        ),
      ]
      expect(hits).toEqual([])
    }
  )

  it("holds 4.5:1 for every raw palette text color on its surfaces and its own wash", () => {
    const { light, dark, unpaired } = rawTextColors()
    expect(unpaired).toEqual([])
    const hits: string[] = []
    for (const [name, used] of [
      ["light", light],
      ["dark", dark],
    ] as const) {
      const { solid } = surfaces(THEMES[name])
      for (const color of used) {
        const [hue, shade] = color.split("-")
        const wash = palette(hue, "500")
        hits.push(
          ...failures(
            palette(hue, shade),
            {
              ...solid,
              [`${hue}-500/10 on background`]: over(
                wash,
                0.1,
                solid.background
              ),
              [`${hue}-500/10 on card`]: over(wash, 0.1, solid.card),
            },
            `${name} text-${color}`
          )
        )
      }
    }
    expect(hits).toEqual([])
  })

  it("never fades text with an opacity modifier or opacity-*", () => {
    const faded =
      /(?<![\w-])(?:[a-z-]+:)*text-(?:foreground|muted-foreground|primary|destructive|warning|success)\/\d+/
    const opacity = /(?<![\w/-])((?:[\w[\]=/-]+:)*)opacity-(\d+)(?![\w-])/g
    const hits: string[] = []
    for (const [file, text] of Object.entries(sources)) {
      text.split("\n").forEach((line, i) => {
        if (faded.test(line)) hits.push(`${file}:${i + 1} faded text color`)
        for (const [, variants, amount] of line.matchAll(opacity)) {
          if (amount === "0" || amount === "100") continue
          if (variants.includes("disabled")) continue
          if (OPACITY_ALLOWED.some(([snippet]) => line.includes(snippet))) {
            continue
          }
          hits.push(`${file}:${i + 1} opacity-${amount}`)
        }
      })
    }
    expect(hits).toEqual([])
  })
})
