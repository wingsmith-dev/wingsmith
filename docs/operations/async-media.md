# Async media jobs

Video generation, and any other media request that asks for it, runs as a background job
(ADR 0041). The gateway queues the job in the plane and answers at once; a worker loop in one of
the gateway replicas runs it against the cluster and writes the output to S3-compatible object
storage. Callers fetch the output through a signed URL.

## Turning it on

Async media is on when the gateway has object storage. Give the gateway and the plane the same
bucket (the plane only deletes expired objects):

| Variable | Gateway | Plane | Meaning |
|---|---|---|---|
| `WINGSMITH_MEDIA_S3_ENDPOINT` | yes | yes | S3 endpoint, such as `https://s3.eu-west-1.amazonaws.com` or a MinIO URL. Path-style requests. |
| `WINGSMITH_MEDIA_S3_BUCKET` | yes | yes | Bucket. Outputs go to `media/{project}/{job}`. |
| `WINGSMITH_MEDIA_S3_REGION` | yes | yes | Signing region, default `us-east-1`. |
| `WINGSMITH_MEDIA_S3_ACCESS_KEY_ID`, `WINGSMITH_MEDIA_S3_SECRET_ACCESS_KEY` | yes | yes | Credentials, from a Secret. The gateway needs put and get, the plane delete. |
| `WINGSMITH_MEDIA_SIGNED_URL_TTL` | yes | | How long a content URL is valid, default `1h`. |
| `WINGSMITH_GATEWAY_MEDIA_JOB_CONCURRENCY` | yes | | Jobs one replica runs at once, default 2. |
| `WINGSMITH_MEDIA_JOB_LEASE_SECONDS` | | yes | Lease a worker holds between heartbeats, default 60. |
| `WINGSMITH_MEDIA_JOB_MAX_ATTEMPTS` | | yes | Claims before a job whose worker keeps disappearing fails, default 3. |

Without `WINGSMITH_MEDIA_S3_ENDPOINT` and `WINGSMITH_MEDIA_S3_BUCKET`, `POST /v1/videos` stays
synchronous and the read paths below answer 404.

## API

Every call uses the tenant's API key on the gateway, the same as any other inference request. A
job belongs to the endpoint's project and is visible only to the tenant that created it.

Video follows OpenAI's Videos API:

```sh
curl -s https://acme.example.com/v1/videos -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: clip-42' \
  -d '{"model": "wan-video", "prompt": "a cat on a skateboard", "seconds": "4", "size": "832x480"}'
# {"id": "mjb_...", "object": "video", "status": "queued", "progress": 0, ...}

curl -s https://acme.example.com/v1/videos/mjb_... -H "Authorization: Bearer $KEY"
# status: queued -> in_progress -> completed | failed

curl -sL https://acme.example.com/v1/videos/mjb_.../content -H "Authorization: Bearer $KEY" -o cat.mp4
# 302 to a signed object storage URL, valid for 1h

curl -s -X DELETE https://acme.example.com/v1/videos/mjb_... -H "Authorization: Bearer $KEY"
```

`DELETE` cancels a queued or running job and removes a finished one's output on the next
retention pass. A cancelled job reads as `failed` with `error.code: cancelled`. Listing jobs is not
supported.

Any other media path (`/v1/images/generations`, `/v1/audio/speech`) becomes a job with
`Prefer: respond-async` or `"background": true` in the body. It answers `202` with
`{"object": "media.job", ...}` and a `Location: /v1/media/jobs/{id}`; read it and its `/content`
the same way. Multipart requests (image edits, transcriptions) cannot run in the background.

`Idempotency-Key` makes a retried submission return the first job instead of a second one.

## Retention

A finished job's output is kept for its project's `media_retention_days` (default 7), then the
reactor deletes the object and the row. If the delete fails, the row is kept and retried on the
next pass.

## Quotas and metering

A video's requested `seconds` (and a speech request's characters) are charged against the
endpoint's `unit_limits` when the job is submitted; the worker that runs it charges only the
difference between what was generated and what was reserved. Each finished job writes one
`media_job.finished` log line with its tenant, project, units and upstream seconds, and the usual
`wingsmith_gateway_generated_total` counters move as for a synchronous request.
