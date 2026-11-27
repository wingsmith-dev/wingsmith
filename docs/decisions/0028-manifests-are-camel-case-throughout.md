# 0028: Manifests are camelCase throughout

**Status:** accepted · 2026-11-27 · supersedes the casing rule in ADR 0022

## Context

ADR 0022 spelled manifest `metadata` the Kubernetes way and kept the plane's snake_case in `spec`
and `status`, so a field read the same on the JSON API and in a manifest. The result is an object
that is half one convention and half the other. Kubernetes tooling, Argo and anyone used to
writing manifests expect camelCase in every section, and `workload_ref` beside `resourceVersion`
reads as a mistake.

## Decision

- **Every `plane.wingsmith.dev/v1alpha1` field is camelCase**, in `metadata`, `spec` and `status`
  alike, including the `Scale` subresource and nested serving shapes: `gangSize`, `workloadRef`,
  `servedModelName`, `readyReplicas`, `deviceClass`.
- **The JSON API (`/v1/...`) stays snake_case.** The manifest is still a translation of the same
  reads (ADR 0022); the translation now also renames. Manifest schemas use msgspec
  `rename="camel"`, and the HTTP schemas a manifest embeds are copied with camelCase names rather
  than renamed, so the JSON API does not move.
- **Free-form maps keep their keys.** `labels`, `annotations`, `runtime.args`, `overrides`,
  `properties` and engine args are user data and are never rewritten.
- **Conditions are Kubernetes-shaped:** `type`, `status`, `reason`, `message`,
  `lastTransitionTime`. The last is null where the plane derives the condition on read and keeps
  no history of it.

## Consequences

- A field has two spellings, one per surface (`workload_ref` on `/v1/endpoints`, `workloadRef` in a
  manifest). Docs name the manifest spelling when talking about manifests.
- `v1alpha1` has no stored manifests and no apply yet, so nothing is converted.
