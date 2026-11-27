# 0037: A durable, ordered change log for declared objects

**Status:** accepted · 2026-11-27

## Context

Watching Workloads, Endpoints and Artifacts (P1.10, `wings get -w`, GitOps) needs an ordered,
resumable record of what changed. The plane had a `NOTIFY` per write, which is lost when nobody is
listening, and a `revision` derived from `updated_at` (or the spec generation), which two writes in
one microsecond or a clock step can move backwards. Writers also committed from many places
(services, the reactor, ingestion, adopt, operations), so there was no single point to hang a log
on.

## Decision

- **One write path.** Every writer of the three tables calls `record_change(db, obj, op)` before
  the transaction commits (`app.db.changes`). Deletes record an explicit `DELETED` tombstone, also
  for rows a cascade removes. Services commit through `changes.commit`, which only flushes inside a
  `unit_of_work`; the outermost unit commits once (or rolls back for `dryRun=All`) and cluster
  wakeups queued with `after_commit` run after it. The manifest PUT uses this instead of patching
  `commit` and `_announce`. A test hook fails any commit made from app code that wrote one of the
  three tables without a matching recorded change.
- **Transactional outbox, not triggers or logical decoding.** `change_log` (global bigserial `seq`,
  kind, project, name, uid, op `ADDED|MODIFIED|DELETED`, generation, created_at) holds hints, never
  bodies. A `before_commit` hook writes it as the last statements of the transaction after
  `pg_advisory_xact_lock` on one constant key, so seqs become visible in commit order.
- **`metadata.resourceVersion` is the object's last seq**, stored in `resource_version` in the same
  transaction. `If-Match` compares against it; `metadata.generation` stays the spec counter. Status
  and rollup writes (ingestion's `started_at`, placement re-expansion) move it, as in Kubernetes,
  so a client holding an old RV gets a 409 more often than before. A replayed apply that changes
  nothing writes nothing and keeps the RV.
- **`NOTIFY wingsmith_changes`** carries the highest seq, delivered on commit. A wakeup only.
- **Retention.** The reactor's slow pass deletes rows older than
  `WINGSMITH_CHANGE_LOG_RETENTION_SECONDS` (24h) and raises `change_log_state.compacted_through`.
  `GET /v1/changes?since=<rv>&limit=` returns hints after `since` in order; `since` at or below
  `compacted_through` is 410 Gone, and the client relists.
- **Scope**: Workload, Endpoint, Artifact. Deployments and operations stay on `NOTIFY` and the hub.

## Consequences

- The advisory lock serialises the end of every logging commit across the plane. It is held only
  for the insert and the commit itself; a per-kind sequence remains open if it becomes a hotspot.
- Rows that existed before the migration have `resource_version = 0` until their next write.
- Full watch streaming, and list responses carrying the RV they were read at
  (`changes.current`), are P1.10.
