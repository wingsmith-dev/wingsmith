# Console: design brief

What the console must feel like to use, and what it must never look like. The system it is a
window onto is described in [`docs/architecture`](../../docs/architecture/README.md); this file constrains
the interface only.

## Users

The single operator of a Wingsmith plane: a platform, SRE, or ML infrastructure engineer, or a small team sharing that one admin identity, running GPU clusters their own organization owns. They serve models to their own organization's teams and carry the pager for the fleet.

They work at a desktop during capacity planning, model rollouts, and incident response. Their inference fleet spans several clusters and several kinds of GPU. They need to understand global intent and per-cluster reality without mentally joining several systems, and without dropping into `kubectl` on four clusters to answer one question.

## Brand Personality

Precise, operational, calm. Wingsmith should feel trustworthy under pressure and concise during routine work.

## Anti-references

Avoid generic cloud-console dashboards made from interchangeable metric cards, decorative sci-fi infrastructure visuals, and telemetry walls that expose every available signal without helping an operator decide what matters. Avoid the Kubernetes-dashboard lineage (Lens, Rancher, raw CRD trees) where the user is handed cluster primitives and left to reconstruct intent. Avoid hiding domain relationships behind Kubernetes terminology or requiring users to rebuild cross-cluster intent from per-cluster objects.

## Design Principles

1. Lead with intent, then reveal where reality differs.
2. Make global-to-cluster relationships visible without requiring mental joins.
3. Put operational state beside the action or object it affects.
4. An operation is a thing with progress and failure, never a status field.
5. Prefer dense, familiar controls over decorative dashboard furniture.
6. Keep calm states quiet so degraded states are unmistakable.

## Accessibility & Inclusion

Target WCAG 2.1 AA. Never rely on color alone for status, preserve keyboard navigation and visible focus, respect reduced-motion preferences, and keep dense operational text legible at browser zoom.

How the contrast part is held in code:

- Every text token (`foreground`, `muted-foreground`, `primary`, `destructive`, `warning`, `success`) holds at least 4.5:1 on every surface token (`background`, `card`, `popover`, `muted`, `secondary`, `accent`, `sidebar`, `sidebar-accent`) in both themes. The tone tokens also hold it on their own `/5`, `/10` and `/15` tints, which is where badges and callouts put them. The filled pairs (`primary-foreground` on `primary`, `destructive-foreground` on `destructive`) hold it too, which is why the dark theme's filled buttons use dark text.
- `text-muted-foreground` is the faintest text color. Hierarchy comes from size, weight and position, not from fading text further.
- No opacity modifier on a text color (`text-muted-foreground/60` and the like), and no `opacity-*` on anything that contains text. Disabled controls are the exception WCAG allows, through `disabled:opacity-50`; icons, sparklines and device bricks may be dimmed because they carry no text.
- Tone text uses the theme tokens. A raw palette text color (`text-sky-700`) is only for categorical color, always paired with a `dark:` shade on the same line, and both shades must hold 4.5:1 on the surfaces and on their own `-500/10` wash.
- `src/text-contrast.test.ts` measures all of this from `src/index.css` and Tailwind's palette, and scans the source for faded text, so a token change or a stray class fails the suite.
