# operator

Go. One instance per cluster. Dials out to the hub; nothing ever connects inward.

**Two loops, and the split matters.**

- **Sync loop** — stream ↔ CRDs. Applies desired state from the plane into custom resources, ships
  observed state back up.
- **Reconcile loop** — CRDs → cluster objects.

The CRDs are the durable local copy of desired state. That is what makes the cluster survive a
plane outage: if the operator held desired state only in memory from the stream, a restart during
a partition would mean the cluster forgets what it is supposed to be running — which would forfeit
the entire reason for choosing an outbound-agent architecture.

Nothing here yet.
