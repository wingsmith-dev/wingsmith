# 0039: Manifests are listed and watched the Kubernetes way

**Status:** accepted · 2026-11-27

## Context

ADR 0038 gave Workloads, Endpoints and Artifacts an ordered change log and a `resourceVersion`.
Clients (`wings get -w`, GitOps controllers, the console) need to list a kind and then follow it
without missing or reordering a change, in a shape people who know Kubernetes already read.

## Decision

- **List.** `GET /v1/manifests/{workloads,endpoints}` (every project) or `/{kind}/{project}`, and
  `/v1/manifests/artifacts` (every scope; `/artifacts/{name}` stays the public read) return
  `{apiVersion, kind: <Kind>List, metadata.resourceVersion, items}`. The items and
  `changes.current()` are read in one read-only `REPEATABLE READ` transaction, so the RV is exactly
  what the items reflect: a commit that lands during the list is either in the items or after the
  RV. No pagination or `continue` yet.
- **Watch.** The same paths with `?watch=true&resourceVersion=N` stream chunked JSON lines,
  `{"type": ..., "object": ...}`, the Kubernetes watch format; no SSE or websockets. An unset or `0`
  RV first sends an `ADDED` per current object. The watcher reads the log after its cursor up to
  the head in batches, each its own short snapshot session (never a pooled session held across the
  stream), and re-reads each change's row: a missing row with a `DELETED` change is a `DELETED`
  built from the log (name, namespace, uid, generation, the tombstone's RV); a row whose RV is past
  the change is skipped, since a later change reports it (coalescing; a coalesced `ADDED` is still
  reported as `ADDED`); otherwise the change's op with the rendered manifest.
- **Expiry.** An RV at or below `compacted_through` is a 410 before the stream starts, and an
  `ERROR` event carrying a `Status` with code 410 once it has. The client relists.
- **Wakeups.** One `LISTEN wingsmith_changes` per process, started with the first watch and
  stopped with the last, wakes every watcher; each also polls every 5s, so a lost notification
  costs latency, never an event.
- **Selectors.** Namespace by path (or `fieldSelector=metadata.namespace=` for artifacts),
  `labelSelector` equality terms (`a=b,c!=d`) applied in Python, `fieldSelector=metadata.name=`.
  The log keeps no labels, so a `DELETED` is sent whatever the label selector says, and an object
  relabelled out of the selector gets no `DELETED` (deferred).
- **Lifetime.** `timeoutSeconds` ends the stream (default 300s plus up to 10% jitter, capped at
  1800s). A `BOOKMARK` with the cursor's RV every 30s; `allowWatchBookmarks` is accepted and they
  are always on.
- **Backpressure.** Each watch buffers at most 1000 events; a client that leaves the buffer full
  for 10s is closed. A process serves at most 256 watches; one more is 429. Watches are admin
  routes like the rest of `/v1/manifests`.
- `/v1/changes` stays as the low-level feed of hints.

## Consequences

- Each event re-renders the manifest through the service read, several queries per object; a
  list is O(objects) such renders. Fine at today's scale, the first thing to batch if not.
- `wings get <kind> [name] -w` lists, prints, watches from the list's RV, resumes from the last RV
  when the plane ends the stream, and relists on 410.
