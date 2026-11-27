# Custom workers

A workload can run your own serving image instead of NVIDIA Dynamo by declaring
`spec.runtime.kind: container`. The platform pulls the weights as for any workload, mounts them
read-only, and routes requests to your container's `http` port. ADR 0031 has the reasoning.

Today this is the MVP: the worker answers the OpenAI chat API (`protocol: openai-chat`), runs one
pod per replica on one node, and serves a pinned artifact (`hf://repo@revision` or a cached path).

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
    readinessProbe:            # optional; defaults to httpGet /health on port http
      httpGet: {path: /v1/models, port: http}
    metrics: {port: http, path: /metrics}
  serving:
    artifact: qwen3-8b
    shape: {deviceClass: l40s-48, deviceCount: 1}
  replicas: 1
```

Apply it with `wings apply -f my-worker.yaml`, or create the same thing with
`wings workload create my-worker --artifact qwen3-8b --class l40s-48 --runtime container
--image ghcr.io/acme/worker@sha256:... --arg --model --arg '$(WS_MODEL_PATH)'`.

## What the platform provides

| Variable | Value |
|---|---|
| `WS_MODEL_PATH` | the artifact's directory, mounted read-only |
| `WS_SERVED_MODEL_NAME` | the name requests use; answer `/v1/models` with it |
| `WS_PORT` | the `http` port's number |
| `HF_HUB_OFFLINE` | `1`: the weights are already local |

`args` and `command` may reference these as `$(NAME)`. Declared `env` cannot override them.

## Rules

- The image is pinned by digest. Tags need `CONTAINER_IMAGE_TAGS_ALLOWED=true` on the plane, and the
  image must match the operator's allowed image prefixes when those are set.
- One port is named `http`. Probes are Kubernetes probes without `exec`.
- The pod runs as non-root with every capability dropped, no privilege escalation, no host
  network, no hostPath and no service-account token.
- Disaggregated topology, router settings, engine args and overlays are Dynamo settings and are
  refused. `runtime.kind` cannot change after create.
- Clusters whose operator found no Dynamo run container workloads only.
