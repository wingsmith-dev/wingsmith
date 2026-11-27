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

## Engine presets: vLLM and SGLang

To run a stock engine without Dynamo, name a `preset` instead of an image. The preset is a template
for `kind: container`: the plane fills in the rest and the cluster or host sees an ordinary container
worker.

```yaml
apiVersion: plane.wingsmith.dev/v1alpha1
kind: Workload
metadata:
  name: qwen-vllm
  namespace: core
spec:
  runtime:
    kind: container
    preset: vllm            # or sglang
    version: "0.34.0"       # optional; defaults to the newest pinned version
    args: ["--max-model-len", "32768"]   # appended to the preset's own flags
  serving:
    artifact: qwen3-8b
    shape: {deviceClass: h100-80, deviceCount: 2}
  replicas: 1
```

or `wings workload create qwen-vllm --artifact qwen3-8b --class h100-80 --devices 2 --preset vllm
--version 0.34.0 --arg --max-model-len --arg 32768`.

| | `vllm` | `sglang` |
|---|---|---|
| Image | `docker.io/vllm/vllm-openai@sha256:...` | `docker.io/lmsysorg/sglang@sha256:...` |
| Command | `vllm serve $(WS_MODEL_PATH)` | `python3 -m sglang.launch_server --model-path $(WS_MODEL_PATH)` |
| Serves as | `--served-model-name $(WS_SERVED_MODEL_NAME) --host 0.0.0.0 --port $(WS_PORT)` | same, plus `--enable-metrics` |
| Tensor parallel | `--tensor-parallel-size <deviceCount>` | `--tp-size <deviceCount>` |

Both get port `http` 8000, readiness `GET /v1/models` (`openai-chat`) or `GET /health` (any other
protocol), a startup probe on `/health` that allows 30 minutes to load, and metrics on
`http:/metrics`. Declared `ports`, `env`, probes and `metrics` win over these; `image` and `command`
are refused, since the preset owns them. `args` are appended after the preset's own flags and may
not repeat the ones it sets (`--port`, `--host`, `--model`, `--model-path`, `--served-model-name`
and the tensor-parallel flags); any other flag takes effect for both engines.

`spec.runtime` is kept as you declared it (with `version` filled in on write, so a newer default
never moves a stored workload). The worker that runs is in `status.runtime` on the manifest and in
`serving.runtime.resolved` on `GET /v1/workloads/{id}`. The same expansion runs on Kubernetes
clusters with or without Dynamo and on Linux hosts.

### The image table

Images come from the plane setting `ENGINE_PRESET_IMAGES`, a comma-separated list of
`preset@version=repo@sha256:digest`; the first entry of each preset is its default version. An entry
without a digest, or a version not in the table, refuses the workload with a message saying so. The
operator's `allowedPrefixes` (or the host's `allowed_image_prefixes`) must include
`docker.io/vllm/vllm-openai` and `docker.io/lmsysorg/sglang`.

To add a release, resolve its digest anonymously and prepend the entry (keep old entries: a stored
workload keeps its version, and its digest must not change under it). Prepending also makes the
new entry the default for workloads created from then on; existing workloads stay on the version
stored with them:

```sh
repo=vllm/vllm-openai tag=v0.35.0
token=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:$repo:pull" | jq -r .token)
curl -sI -H "Authorization: Bearer $token" \
  -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json" \
  "https://registry-1.docker.io/v2/$repo/manifests/$tag" | grep -i docker-content-digest
```

Use the index (multi-arch) digest, as above, so the same entry pulls on amd64 and arm64 hosts.

## Worker metrics

A worker that declares `metrics` is scraped like this:

- **Kubernetes.** Its pods carry `prometheus.io/scrape: "true"`, `prometheus.io/port` and
  `prometheus.io/path`, and the chart's metrics agent scrapes them (job `container-workers`, keyed by
  the `wingsmith.dev/runtime=container` label) and remote-writes `vllm:*` and `sglang:*` series to
  the plane's store with a `wingsmith_deployment_id` label. With the Prometheus Operator, set
  `operator.workerMetrics.podMonitor: true` and the operator also renders a `PodMonitor` per
  worker; the chart turns it on only when the cluster serves `monitoring.coreos.com/v1/PodMonitor`.
- **Linux hosts.** The agent lists each replica's loopback metrics endpoint for HTTP service
  discovery; see linux-host.md, Worker metrics.

The console's engine panels and engine-metric autoscaling read Dynamo's series and do not cover
container workers yet; gateway-side latency, TTFT and token metrics do.

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
