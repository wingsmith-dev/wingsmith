/**
 * One color per workload, held stable by hashing its key.
 *
 * The board's whole argument is that you can see the same workload scattered
 * across fabrics, so the color has to be a property of the workload and not of
 * where it happens to appear in a list. Emerald, amber and red are missing on
 * purpose: they mean free, cordoned and faulted everywhere else in the console,
 * and a workload wearing one of them would read as a state.
 */
export interface BrickColor {
  /** The device cell itself. */
  fill: string
  /**
   * The same cell, for capacity promised to this workload but not yet bound to
   * it. Its own tone instead of an opacity applied to `fill`: it
   * has to stay clearly the workload's color against both themes while never
   * being mistakable for the solid fill beside it.
   */
  ghost: string
  /** The cell's edge, for the hovered and selected states. */
  edge: string
  /** A legend or inspector swatch. */
  swatch: string
  /** Text on the page background, for names and counts. */
  text: string
  /** A tinted panel behind a name. */
  wash: string
}

const PALETTE: BrickColor[] = [
  {
    fill: "bg-sky-500/80",
    ghost: "bg-sky-500/25 dark:bg-sky-500/30",
    edge: "ring-sky-400",
    swatch: "bg-sky-500",
    text: "text-sky-700 dark:text-sky-300",
    wash: "bg-sky-500/10",
  },
  {
    fill: "bg-violet-500/80",
    ghost: "bg-violet-500/25 dark:bg-violet-500/30",
    edge: "ring-violet-400",
    swatch: "bg-violet-500",
    text: "text-violet-700 dark:text-violet-300",
    wash: "bg-violet-500/10",
  },
  {
    fill: "bg-teal-500/80",
    ghost: "bg-teal-500/25 dark:bg-teal-500/30",
    edge: "ring-teal-400",
    swatch: "bg-teal-500",
    text: "text-teal-700 dark:text-teal-300",
    wash: "bg-teal-500/10",
  },
  {
    fill: "bg-fuchsia-500/80",
    ghost: "bg-fuchsia-500/25 dark:bg-fuchsia-500/30",
    edge: "ring-fuchsia-400",
    swatch: "bg-fuchsia-500",
    text: "text-fuchsia-700 dark:text-fuchsia-300",
    wash: "bg-fuchsia-500/10",
  },
  {
    fill: "bg-indigo-500/80",
    ghost: "bg-indigo-500/25 dark:bg-indigo-500/30",
    edge: "ring-indigo-400",
    swatch: "bg-indigo-500",
    text: "text-indigo-700 dark:text-indigo-300",
    wash: "bg-indigo-500/10",
  },
  {
    fill: "bg-cyan-500/80",
    ghost: "bg-cyan-500/25 dark:bg-cyan-500/30",
    edge: "ring-cyan-400",
    swatch: "bg-cyan-500",
    text: "text-cyan-700 dark:text-cyan-300",
    wash: "bg-cyan-500/10",
  },
  {
    fill: "bg-pink-500/80",
    ghost: "bg-pink-500/25 dark:bg-pink-500/30",
    edge: "ring-pink-400",
    swatch: "bg-pink-500",
    text: "text-pink-700 dark:text-pink-300",
    wash: "bg-pink-500/10",
  },
  {
    fill: "bg-blue-500/80",
    ghost: "bg-blue-500/25 dark:bg-blue-500/30",
    edge: "ring-blue-400",
    swatch: "bg-blue-500",
    text: "text-blue-700 dark:text-blue-300",
    wash: "bg-blue-500/10",
  },
]

export function brickColor(key: string): BrickColor {
  let hash = 0
  for (let i = 0; i < key.length; i++) {
    hash = (hash * 31 + key.charCodeAt(i)) % 0xffffffff
  }
  return PALETTE[hash % PALETTE.length]
}
