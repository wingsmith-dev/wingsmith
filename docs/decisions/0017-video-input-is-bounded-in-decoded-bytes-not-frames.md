# 0017: Video input is bounded in decoded bytes, not in frames

**Status:** accepted · 2026-09-13

All six bounds below are implemented. Bounds 1, 5 and 6 live in this repository. Bounds 2, 3 and 4
are a patch to the worker's video loader, carried in a worker image layered on the vendor runtime
image; that layer is maintained outside this repository, and the bounds are recorded here because
the plane and the gateway are built around them. Rolled onto a Kimi K3 deployment, the six answered
every clip the stock image refused or killed a worker on, left the prefill role's KV allocation unchanged, and took
prefill restarts under video traffic to zero. What that roll did *not* establish is concurrent video:
one clip at a time was sent, and a prefill worker that has served full-grid video holds ~19.5 GiB per
GPU of encoder reserve, leaving 3.5-4 GiB free. That is the open edge.

Bound 5 was verified against a real defeat attempt: a request carrying
`media_io_kwargs: {"video": {"num_frames": 4}}` is answered at the same prompt token count as one
without it, so the field no longer reaches `VideoMediaIO.merge_kwargs` and cannot wipe the startup
`fps` default.

## Context

A `video_url` request could exhaust a worker's host memory. vLLM's video loader preallocates one
array for the whole clip at native resolution (`np.empty((frames, h, w, 3))`,
`vllm/multimodal/video.py:429`), and the runtime image stamped `DYN_MM_VIDEO_NUM_FRAMES=-1`, which
means every frame. Computed: five minutes of 1080p30 is 52 GiB, twenty minutes 209 GiB, twenty
minutes of 4K 834 GiB. The recovery and pyav paths build a list and `np.stack`, so those double.
`VLLM_MEDIA_LOADING_THREAD_COUNT` is 8, so that is the multiplier. The clip arrives as a URL, so the
gateway's 32 MiB body cap never applies, and `VLLM_VIDEO_FETCH_TIMEOUT` at 30s is the only
incidental bound.

`-1` is not a neutral default. vLLM ships `num_frames=32` because unbounded frames are a published
DoS class (CVE-2026-34755 / GHSA-pq5c-rjhq-qp7p, hardened in PR #38636) and `-1` is the documented
opt-out of that mitigation. The stamp is ours, so the exposure is ours.

Frames are the wrong unit to fix it with. The quantity that exhausts a host is
`frames × height × width × 3`, and a frame cap bounds one factor of a product: 256 frames of 8K is
still 150 GiB. Nothing in the stack bounds the other factor usefully: `--media-io-kwargs` has no
resolution key, `max_duration` is dead on this path (`video.py:1010`, "ignored here"; K3 registers no
video processor so it gets the default `opencv` backend), and no decode backend anywhere downscales
while decoding. `VLLM_MAX_IMAGE_PIXELS` does guard video frames in this image
(`_check_frame_pixel_limit`, ahead of upstream main), but it raises instead of resizing, and no
provider rejects a clip for being 4K.

There is no vendor number to match. Moonshot publishes a 100 MiB per-file cap and a 1080p
*recommendation* that is explicitly not an error; no duration, no fps, no frame count, no pixel
budget. Across providers the pattern is consistent: bytes and duration are 4xx, pixels are a silent
downscale (Anthropic, Qwen, Bedrock Nova all resize; Nova flattens every clip to 672×672 without
saying so).

## Decision

**Bound decoded bytes per clip.** K3's own geometry makes that bound flat in duration:
`per_frame_patch_limit(n) = min(16384, 655360 / n)`, so a clip decoded at the size the model will
shrink it to anyway costs

    n × min(16384, 655360/n) × 14² × 3  ≤  655360 × 588  =  367.5 MiB

for any duration and any source resolution (see the correction below for the measured worst case).
The target size is a pure function of the frame count, which comes from container metadata, so it is
derivable before a frame is decoded, and it is lossless, because `in_patch_limit_video` shrinks
those frames regardless. Without it, a ten-minute clip is decoded at 1920×1080 and MoonViT is handed
218×123 frames.

**Downscale on pixels, reject on bytes**, following every provider that documents either.

**The admitted length is twenty minutes.** It matches `qwen-vl-max` and roughly what Moonshot's own
100 MiB cap admits at 1080p, which makes 100 MiB the only published number there is to defend.

| # | Bound | Value | Layer | On exceed |
|---|---|---|---|---|
| 1 | `fps` in `--media-io-kwargs` | 8, K3's own `sample_fps` | the workload's own `engine_args` | samples fewer frames |
| 2 | decode at the derived per-frame size | `min(16384, 655360/n)` patches | the worker image's video loader | silent resize |
| 3 | per-clip decoded bytes | 1 GiB | the worker image's video loader | 400 naming duration and resolution |
| 4 | URL fetch bytes | 100 MiB | the worker image's video loader | 400 |
| 5 | client `media_io_kwargs` | stripped | [`libs/gateway/`](../../libs/gateway/internal/serving/policy.go) | silently dropped |
| 6 | `serving.max_videos_per_request` | 1 | plane, already typed | 400 |

**Bound 1 has one route only.**
[`dynamo-recipes.json`](../../libs/plane/app/integrations/data/dynamo-recipes.json) is a pinned
vendor capture the plane adopts a single flag from, so a flag reaches a worker only through that
workload's own `engine_args`. It is hand-typed there today; its durable home is a typed serving field
with a `SERVING_FLAGS` row.

Bound 1 is free and lossless on its own: the runtime layer downsamples to 8 fps anyway, and
`_clip_facts` already derives the delivered rate from `frames_indices` for exactly this case. It cuts
30 fps source by 3.75×. It does **not** make twenty minutes safe: 9,600 frames at native 1080p is
still ~60 GiB, so bound 2 is what unlocks the stated length.

Bound 5 is not optional. `media_io_kwargs` is a per-request API field, so without it bound 1 is one
request away from being defeated.

## Consequences

The gateway cannot bound an `https://` clip's duration or bytes, and this does not overturn that:
"a remote URL is never touched. Fetching an address a caller supplied is an egress decision."
Bounds 3 and 4 therefore live in the worker, where the fetch happens.

Twenty minutes needs the encoder admission gate raised. It 400s above ~58 s at 1080p by default:
`input_processor.py:457` rejects an item over `mm_encoder_cache_size`, and the gate is the runtime's
own 40-frame dummy's declared maximum, not a memory limit. Re-profiling the dummy at the worst
admissible clip covers every clip the geometry can produce.

Beyond what 655,360 patches can resolve, a longer clip genuinely gives the model less detail per
second. At twenty minutes that is roughly 0.16 fps of real temporal resolution. That is documented,
not hidden, which is where this diverges from Nova.

Upstream should carry bounds 3 and 4. vLLM guards audio by both filesize and decode duration and
images by pixels; video has neither, and `multimodal/media/audio.py:83` is a ready template;
reject from metadata, then count during decode in case the metadata lies.

## Corrections after measurement

**The decision stands; three of its original figures did not.** The `367.5 MiB` ceiling is an
algebraic peak that assumes an exact resize: `navit_resize` floors in pixels, so real clips measure
379.5 and 375.7 MiB with a worst case of **723.4 MiB**. The pad count originally quoted for the gate
(57,600) was the twenty-minute clip's cost, not the ceiling; the worst admissible clip is 9,567
frames of 27x7167 at **227,240 pads**. Bound 4's 100 MiB fetch cap **does not cover `data:` URLs**,
which are bounded only by the gateway's 32 MiB body cap and only when they cross the gateway. The
shape of the bound (flat in duration and resolution) is unaffected.

**The 227,240-pad ceiling describes a single resize.** The runtime once performed two on narrow
sources, which made the gate conservative by about 1.64x against real requests instead of tight;
with that fixed the figure describes the runtime again, and the declared number never moved. Bound
3's 1 GiB is also unreachable while bound 2 is in force (the worst admissible decoded clip is
717.5 MiB), so **nothing on the mp4 path refuses on size**: only malformed input, bound 4 and bound
6 refuse. That is the intended shape, not a hole.

**The raised gate costs zero KV, measured.** The 227,240-pad gate and the stock 44,640 allocate the
same KV to the token (47.02 GiB, 3,617,511 tokens, 3.45x concurrency) because every K3 serving
configuration carries `--skip-mm-profiling`, so the dummy's shape sets the admission ceiling and never
enters the memory profile. "Pre-allocated encoder cache size" pre-allocates nothing; it is a scheduler
token budget. **The twenty-minute cap therefore has no capacity argument against it and must not be
cut to reclaim KV**: a five-minute cap lowers the gate 48% and buys zero. What the raise does spend
is headroom (~21 GiB per GPU of encoder allocator reserve out of the 5% left by
`--gpu-memory-utilization 0.95`), so that flag, not the frame cap, is the lever if video ever OOMs.
