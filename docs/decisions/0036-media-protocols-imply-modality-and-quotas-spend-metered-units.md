# 0036: Media protocols imply a modality, and media quotas spend the metered units

**Status:** accepted · 2026-11-27

## Context

The P4.1 design pass settled the media API: OpenAI images, audio speech and transcriptions, and
videos; container protocols for engines Dynamo does not run (diffusers, SGLang-diffusion, Whisper,
TTS servers); billing as images times size tier, video seconds and audio seconds or characters;
quotas on the same counters. vLLM-Omni under Dynamo has no audio output we have verified, and the
gateway did not read serving quotas at all.

## Decision

- **A container protocol is one OpenAI API family and implies one output modality**:
  `openai-chat` text, `openai-images` image, `openai-audio-speech` speech,
  `openai-audio-transcriptions` transcription, `openai-videos` video. The plane stores the implied
  modality on the workload, so the routing table and the gateway's path check need no new field,
  and refuses a declared modality that disagrees. `speech` and `transcription` exist only on
  container workloads; omni stays image or video.
- **Readiness defaults by protocol**: `/v1/models` for `openai-chat`, whose servers list the
  model, `/health` for the media protocols, whose servers often do not.
- **The gateway bounds each media path** with a body cap (1 MiB for prompts and speech input, 25 MiB
  plus form overhead for audio uploads, 50 MiB for image edits and video) and a whole-request
  upstream deadline (2 min images and speech, 5 min transcription, 10 min video). Text paths keep
  the listener's bounds: a generation legitimately streams for minutes.
- **Metering units** are what this hop can state: `image_units` = images x ceil(W*H / 1024^2) (one
  tier when no size was stated), `video_seconds` as requested, `speech_characters` from the
  request `input`, `speech_seconds` from the worker's `X-Audio-Duration`, `transcription_seconds`
  from the response's `duration` or `usage.seconds` (or the header). A unit nothing stated is
  absent, never zero.
- **Media quotas are `unit_limits` on the existing ServingQuota** (units a minute, keyed by those
  unit names), carried on the routing table's `limits`, honouring `enforced` (off counts only) and
  `limit_scope` (`divided` gives each replica `limit / replicas`). A unit known from the request
  is charged before the forward and refused when the budget cannot cover it; a unit only the
  response states is charged after, so the budget may go negative and the endpoint is refused
  until it refills. A failed request is refunded.

## Consequences

- Per replica, like every other gateway limit: R replicas under `per_replica` admit up to R times
  the budget. No shared counter.
- An image request that omits `size` is charged one tier; the engine may have produced more.
- The request and token rates on ServingQuota are still not enforced; only `unit_limits` are.
- Async video jobs, object-storage outputs and retention are P4.3.
