# 0041: Async media jobs run on gateway workers and land in object storage

**Status:** accepted · 2026-11-27

## Context

Video generation takes minutes. Dynamo serves `POST /v1/videos` synchronously, so until now the
caller held a connection for the whole generation and `GET /v1/videos/{id}` answered 404. P4.1
decided that async jobs live in the plane database, that the gateway stays stateless, and that
outputs are delivered as signed URLs from object storage with a per-project retention.

## Architecture check (before building)

- Who runs the long upstream call: a worker loop inside every gateway replica. The gateway that
  receives `POST /v1/videos` submits a job row to the plane and answers 202. Any replica's worker
  claims queued rows with a lease (`FOR UPDATE SKIP LOCKED`), heartbeats it, and runs the call
  through its own internal-zone pipeline, so routing, modality checks, metering and the tunnel are
  the same code as a synchronous request. The plane never dials an engine.
- Why not the plane dispatching: the plane has no path into clusters for inference (that is the
  gateway's tunnel), and pushing to a gateway replica needs replica addresses and delivery retries.
  Pull with a lease needs neither, and a replica that dies mid-job loses only its lease.
- Bytes: the worker spools the upstream response to a temp file and PUTs it to S3-compatible
  storage at `media/{project}/{job}` with its own credentials. The plane stores only the key.
  `GET .../content` is a 302 to a URL the gateway presigns (SigV4 query auth, 1h). The plane holds
  the same credentials only to delete expired objects.
- No new state in the gateway: lease, attempts, cancellation and the output key are all on the row.

## Decision

- **`media_job` rows in the plane**, owned by the endpoint's project and the caller's tenant.
  Status `queued|running|succeeded|failed|cancelled`, `lease_owner`/`lease_expires_at`,
  `attempts` (a lease that expires requeues the job until `MEDIA_JOB_MAX_ATTEMPTS`, then it fails),
  a per-tenant unique `idempotency_key`, and `retention_until` set at completion from the project's
  `media_retention_days` (default 7).
- **The gateway talks to the plane as its service principal** on `/v1/gateway/media-jobs/*`:
  create, read, cancel (all tenant-scoped), claim, heartbeat, complete. Heartbeat and complete
  carry the claim's `attempts` as a lease token, since every slot of a replica shares one worker
  id; a stale claim is answered 409. A worker heartbeats once more before its upload, so a lost
  lease leaves no object behind. Tenants never reach the
  plane; they call the gateway with their API key, and a job is only visible to the tenant that
  created it.
- **API.** `POST /v1/videos` is always async when the gateway has object storage configured and
  returns an OpenAI video object (`id`, `object: "video"`, `status`, `progress`, `model`,
  `created_at`, `completed_at`, `expires_at`, `seconds`, `size`, `error`). `GET /v1/videos/{id}`,
  `GET /v1/videos/{id}/content` (302) and `DELETE /v1/videos/{id}` (cancels, or expires a finished
  one so the slow pass removes it). Other media paths become jobs with `Prefer: respond-async` or
  `"background": true`, answered 202 with the same object as `object: "media.job"`, read under
  `/v1/media/jobs/{id}`. Without storage configured, video stays synchronous as before.
- **Quotas reserve at submit and settle at completion.** The submitting replica charges the
  requested `video_seconds` up front and keeps it; the worker that runs the job does not admit
  again and charges only the difference between what was generated and what was reserved.
- **Metering is one log line per finished job** (`media_job.finished`, units and upstream seconds),
  plus the existing `wingsmith_gateway_generated_total` counters from the shared pipeline. The plane
  stores `upstream_seconds` and `units` on the row.
- **Retention.** The reactor's slow pass deletes the object, then the row, for every job past
  `retention_until`. A failed delete keeps the row, so nothing is orphaned silently.

## Consequences

- Object storage is required for async. A response is spooled to local disk once per running job.
- A worker stream-decodes a JSON response's `data[0].b64_json` into a second spool file, so the
  video never sits in memory; it takes twice its size on disk while it uploads.
- Quota reservation and settlement can hit different replicas' buckets; quotas are already
  per replica (ADR 0036), so a fleet total is still approximate.
- Webhooks, queue-depth autoscaling and spot placement are P4.4.
