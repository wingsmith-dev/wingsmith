# Custom workers

A workload can run your own serving image instead of NVIDIA Dynamo by declaring
`spec.runtime.kind: container`. The platform pulls the weights as for any workload, mounts them
read-only, and routes requests to your container's `http` port. ADR 0031 has the reasoning.

The worker answers one OpenAI API family, named by `protocol`, runs one pod per replica on one node,
and serves a pinned artifact (`hf://repo@revision` or a cached path).

## Protocols

The protocol decides what the worker emits (`serving.outputModality`), which paths the gateway
sends it, and the default readiness probe. Declaring a different `outputModality` is refused.

| `protocol` | Emits | Gateway paths | Default readiness |
|---|---|---|---|
| `openai-chat` (default) | text | chat, completions, embeddings, messages, responses | `GET /v1/models` |
| `openai-images` | image | `/v1/images/generations`, `/v1/images/edits`, chat | `GET /health` |
| `openai-audio-speech` | speech | `/v1/audio/speech` | `GET /health` |
| `openai-audio-transcriptions` | transcription | `/v1/audio/transcriptions`, `/v1/audio/translations` | `GET /health` |
| `openai-videos` | video | `/v1/videos` | `GET /health` |

Audio is served only this way (diffusers, SGLang-diffusion, Whisper or a TTS server behind one of
these protocols); the omni engine under Dynamo emits image or video. A speech worker may set
`X-Audio-Duration` (seconds) on its response so the gateway can meter `speech_seconds`; a
transcription worker's `duration` (verbose_json) or `usage.seconds` is read for
`transcription_seconds`. The gateway's media limits and quotas are in
`libs/gateway/README.md`.

## A minimal manifest

```yaml
apiVersion: plane.wingsmith.dev/v1alpha1
kind: Workload
metadata:
  name: my-worker
  namespace: core
spec:
  runtime:
    kind: container
    image: ghcr.io/acme/worker@sha256:<64 hex>
    args: ["--model", "$(WS_MODEL_PATH)", "--served-model-name", "$(WS_SERVED_MODEL_NAME)",
           "--port", "$(WS_PORT)"]
    ports:
      - name: http
        containerPort: 8000
    protocol: openai-chat
    readinessProbe:            # optional; defaults by protocol (see Protocols)
      httpGet: {path: /v1/models, port: http}
    metrics: {port: http, path: /metrics}
  serving:
    artifact: qwen3-8b
    shape: {deviceClass: l40s-48, deviceCount: 1}
  replicas: 1
```

Apply it with `wings apply -f my-worker.yaml`, or create the same thing with
`wings workload create my-worker --artifact qwen3-8b --class l40s-48 --runtime container
--image ghcr.io/acme/worker@sha256:... --arg --model --arg '$(WS_MODEL_PATH)'`. Add
`--protocol openai-audio-speech` (or another protocol) for a media worker.

## What the platform provides

| Variable | Value |
|---|---|
| `WS_MODEL_PATH` | the artifact's directory, mounted read-only |
| `WS_SERVED_MODEL_NAME` | the name requests use; answer `/v1/models` with it |
| `WS_PORT` | the `http` port's number |
| `HF_HUB_OFFLINE` | `1`: the weights are already local |

`args` and `command` may reference these as `$(NAME)`. Declared `env` cannot override them.

## Rules

- The image is pinned by digest. Tags need `CONTAINER_IMAGE_TAGS_ALLOWED=true` on the plane and
  `operator.images.containerTagsAllowed: true` (`WINGSMITH_CONTAINER_IMAGE_TAGS_ALLOWED`) on the
  cluster; either one off refuses them.
- The operator must have `operator.images.allowedPrefixes` (`WINGSMITH_ALLOWED_IMAGE_PREFIXES`) set
  and the image must fall under it. With the list empty, every container workload is refused.
- One port is named `http`. Probes are Kubernetes probes without `exec`.
- The pod runs as non-root with every capability dropped, no privilege escalation, no host
  network, no hostPath and no service-account token.
- Disaggregated topology, router settings, engine args and overlays are Dynamo settings and are
  refused. `runtime.kind` cannot change after create.
- Clusters whose operator found no Dynamo run container workloads only.
