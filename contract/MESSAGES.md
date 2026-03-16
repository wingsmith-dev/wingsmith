# Message types

The envelope, correlation rules, and resume semantics are in [`PROTOCOL.md`](PROTOCOL.md). This
document is the type registry: what exists, which way it flows, and what its payload holds.

Naming is `<noun>.<verb>`. The noun is an object from
[`docs/plans/01-object-model-and-architecture.md`](../docs/plans/01-object-model-and-architecture.md);
the verb says what kind of frame it is.

| Verb | Direction | Shape |
|---|---|---|
| `.desired` | hub → operator | declared state for one object. Full object, not a patch. |
| `.observed` | operator → hub | observed state for one object. Full object, not a patch. |
| `.event` | operator → hub | something happened, at a point in time. Never inferred from state. |
| `.request` / `.response` | either | correlated pair, per §3 |

**Full objects, never patches.** A patch stream that loses a frame is unrecoverable; a full-object
stream that loses a frame is stale until the next one, which resume then fixes. The cost is
bandwidth we do not care about at this scale.

---

## Session

| Type | Direction | Payload |
|---|---|---|
| `hello` | op → hub | `cluster_id`, `agent_version`, `resume_from` |
| `welcome` | hub → op | `session_id`, `ack_seq`, `full_resync` |
| `ping` / `pong` | either | empty |
| `error` | either | `code`, `message`, `retryable` |
| `stream.end` | either | `count` |

## Supply — operator → hub

| Type | Payload |
|---|---|
| `node.observed` | identity, membership, conditions, kubelet and driver versions, cordon state |
| `node.event` | joined, left, cordoned, degraded |
| `device.observed` | per-accelerator: uuid, model, memory, health, ECC and throttle state, MIG profile, current allocation |
| `device.event` | degraded, recovered, reset, fell off the bus |
| `topology.observed` | which devices share an NVLink domain, PCIe root, NUMA node |
| `capacity.observed` | derived rollup per pool: free, allocated, unhealthy, by device class |

Device metrics — utilization, temperature, throughput — are **not** here. See PROTOCOL §7.

## Demand — hub → operator

| Type | Payload |
|---|---|
| `deployment.desired` | recipe reference, scale bounds, priority, placement constraints |
| `deployment.deleted` | id |
| `artifact.desired` | what weights should be resident, and where they come from |
| `component.desired` | what is installed and at what version |
| `cache.desired` | the artifact warm-set for this cluster |

## Demand — operator → hub

| Type | Payload |
|---|---|
| `deployment.observed` | phase, replica counts, last error |
| `replica.observed` | which devices, readiness, engine version, restart count |
| `replica.event` | started, ready, failed, OOM, preempted |
| `allocation.observed` | devices currently committed, and to what |
| `cache.observed` | artifact residency and eviction pressure |
| `component.observed` | rollout status per component |

## Operations

| Type | Direction | Payload |
|---|---|---|
| `operation.desired` | hub → op | typed: drain, cordon, reboot, device reset, cache warm, roll, upgrade. Carries target, reason, requested_by, deadline |
| `operation.observed` | op → hub | phase, progress, result, failure |

Operations are the third kind of thing — imperative, long-running, able to fail. They are not
status fields on the objects they act upon, and modelling them as such is the mistake this
registry exists to prevent.

## Resync

| Type | Direction | Payload |
|---|---|---|
| `resync.begin` | either | `scope` |
| `resync.end` | either | `count` |

Everything between is ordinary `.desired` / `.observed` frames sharing one `reply_to`. There is no
special resync encoding — a snapshot is just the whole set, sent at once.

---

## Status

**Draft.** The verbs, directions, and the full-object rule are settled. Payload field lists are
indicative and become binding as each type gets a fixture in [`fixtures/`](fixtures/). A type with
no fixture is not part of the contract, so nothing here is real until it has one.
