# 0022: Plane kinds are manifests beside the HTTP API

**Status:** accepted · 2026-11-26

## Context

A declarative API (`apply`, `diff`, later `kubectl` and Argo against the plane) needs one written
shape per object: what the user declares, apart from what the plane derives. The `/v1` request and
read schemas mix the two, name related objects by id, and differ between create and read.

## Decision

Workload, Endpoint and Artifact have a manifest, `apiVersion: plane.wingsmith.dev/v1alpha1`, defined
as msgspec structs in `app/modules/manifest/` and read at `GET /v1/manifests/{kind}/{project}/{name}`.

- **Beside, not instead.** The manifest is translated from the same reads `/v1` serves, so it
  enforces the same visibility and needs no schema change. The HTTP API keeps its shapes.
- **Versioned separately.** `v1alpha1` moves on its own schedule; a manifest version carries a
  conversion obligation the HTTP API does not. `wingsmith.dev` stays the operator's CRD group, and
  plane kinds are never installed as CRDs.
- **Metadata is Kubernetes-shaped** (`name`, `namespace`, `labels`, `annotations`, and read-only
  `uid`, `generation`, `creationTimestamp`, `resourceVersion` = `revision`). `namespace` is the
  project slug. `spec` and `status` keep the plane's snake_case field names.
- **Identity is a name.** An Artifact's name is its handle. A public Artifact has no namespace and
  is read at `/v1/manifests/artifacts/{name}`; a private one only under its own project. References
  in a spec are names too: artifact handles, cluster names, deployment object names.
- **Engine-agnostic.** `spec.runtime` (`kind: dynamo`, `engine`, `version`, `image`, `args`) sits
  beside `spec.serving` and maps onto today's flat serving fields. Custom workers add
  `runtime.kind: container`.
- **Status** is what the plane derives: workload phase, served model name, serving digest,
  resolved and imported engine args, placed/ready, deployments; an endpoint's resolved deployments;
  an artifact's `verified` and residency. Fitment stays a parameterised query on its own route.
- **Markers in the JSON Schema.** `x-immutable` marks create-only fields (Workload `kind`,
  `gang_size`, `cluster`, `enabled`, `allowed_*`; Artifact `digest`, `source`). `x-defaulted`
  marks fields the plane fills when omitted (`priority_class`, slugs, fields an `hf://` source
  resolves). A diff compares a defaulted field only when the manifest declared it. The OpenAPI
  document drops these markers, so `libs/cli/scripts/manifest-schema.py` exports the schema to the CLI.
- A workload's `note` is the `plane.wingsmith.dev/note` annotation, since it lands on a revision.

## Consequences

`wings get <kind> <name> -o yaml` prints a manifest that can later be applied back. Create-only
values not stored on the object (`enabled`, an artifact's `source`) read back null. `namespace` is a
slug and slugs are unique per tenant, so a slug two tenants share is refused, as on `/v1`. The
write path (`PUT`), the endpoint target-by-workload shape and scale-as-spec are decided in P1.6.
