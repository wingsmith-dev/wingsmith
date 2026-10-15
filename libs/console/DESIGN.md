# Console — design brief

What the console must feel like to use, and what it must never look like. The system it is a
window onto is described in [`docs/architecture`](../../docs/architecture/README.md); this file constrains
the interface only.

## Users

Platform, SRE, and ML infrastructure engineers who operate GPU clusters their own organisation owns — a neocloud, an enterprise AI platform team, or a research organisation running several clusters across regions. They serve either internal teams or paying external customers, and they carry the pager for both.

They work at a desktop during capacity planning, model rollouts, and incident response. Their inference fleet spans several clusters and several kinds of GPU. They need to understand global intent and per-cluster reality without mentally joining several systems, and without dropping into `kubectl` on four clusters to answer one question.

## Brand Personality

Precise, operational, calm. Hangar should feel trustworthy under pressure and concise during routine work.

## Anti-references

Avoid generic cloud-console dashboards made from interchangeable metric cards, decorative sci-fi infrastructure visuals, and telemetry walls that expose every available signal without helping an operator decide what matters. Avoid the Kubernetes-dashboard lineage — Lens, Rancher, raw CRD trees — where the user is handed cluster primitives and left to reconstruct intent. Avoid hiding domain relationships behind Kubernetes terminology or requiring users to rebuild cross-cluster intent from per-cluster objects.

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

- `text-muted-foreground` is the faintest text colour. It holds at least 4.5:1 on `background`, `card` and `muted` in both themes. Hierarchy comes from size, weight and position, not from fading text further.
- No opacity modifier on a text colour (`text-muted-foreground/60` and the like). Disabled controls are the exception WCAG allows, through `disabled:opacity-50`.
- Warning and success text use the `warning` and `success` tokens, which are set per theme. Raw palette colours such as `text-amber-500` read on one theme and fail on the other.
- `src/text-contrast.test.ts` fails the suite if either of the last two comes back.
