# proto

The plane ↔ operator contract, and the single source of truth for it. Go is generated for
`libs/operator`, Python for `libs/plane`.

These messages are **our domain objects** — Cluster, Device, Artifact, Recipe, Deployment and the
rest. Kubernetes types must never appear here. If a message starts looking like a manifest,
something has leaked in the wrong direction.

Nothing here yet.
