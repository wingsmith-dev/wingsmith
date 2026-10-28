# 0013: One frontend per model, routed by a forwarded deployment id

**Status:** accepted · 2026-09-02, the object named in points 2 and 4 amended by ADR 0014

**Supersedes:** an earlier recommendation to group frontends by runtime image and let the agent
build a model→frontend table by polling each frontend's `/v1/models`. The failure that prompted it
stands; only its routing argument and its recommendation are replaced.

## Context

A cluster ran exactly one OpenAI-compatible Dynamo frontend and served every model through it. The
frontend is a binary, and it must be able to tokenize and register every model it discovers. So the
frontend image was a fleet-wide lowest common denominator: every model on a cluster had to be
servable by one runtime build.

That constraint breaks the first time a model needs a frontend fix the shared build does not have.
Kimi K3 is the case: loaded on a cluster and healthy on every worker, it made the cluster frontend
fail registration in a loop on an unsupported tiktoken type, and `GET /v1/models` returned an empty
list. Not just K3's entry: the list. The obvious answer (move the whole cluster's frontend onto a
K3-specific build) is the same constraint pointed the other way: the next model on that cluster must
now be servable by a build made for K3.

The earlier recommendation rejected a frontend per deployment on the grounds that nothing could
choose between them: a request names its model in the body, where no ingress and no Service can
route on it. That premise about ingresses is true and the conclusion drawn from it is not. The
gateway already parses the body, resolves the model to an endpoint, picks one upstream from the
routing table, and rewrites the model string. It does not merely know which cluster: it has already
chosen a **deployment**.

## Decision

1. **The operator renders a `Frontend` component into the graph it already renders per deployment.**
   One frontend per model. It is no longer `cluster-setup`'s singleton, and a model's runtime is no
   longer anybody else's constraint.
2. **The frontend's image is the recipe's `frontend_image`, then the cluster's configured default,
   then the recipe's engine image.** A separate field from `engine_image` because the two genuinely
   differ: the K3 fix is compiled into the frontend binary, and its worker correctly stays on
   NVIDIA's base image. The plane never substitutes one for the other: an image it invented is
   indistinguishable on arrival from one somebody pinned.
3. **The gateway forwards the deployment id it already resolved, as `X-Wingsmith-Deployment-Id`.** The
   constant lives in [`libs/packages/tunnel`](../../libs/packages/tunnel/headers.go), because neither
   end owns the wire. The gateway sets it unconditionally and deletes it when there is none:
   `ReverseProxy` clones inbound headers verbatim, so anything less lets a caller pick which model's
   frontend answers them.
4. **The agent maps that id to a Service by the `wingsmith.dev/deployment-id` label.** Dynamo copies a
   component's pod-template labels onto the Service it derives: verified on a live cluster, where the
   worker Service carries the deployment, recipe and project the operator set on the worker's pods. A
   frontend Service with no such label is the cluster's shared frontend and is the fallback.
5. **Not `/v1/models` polling.** Two deployments of one model on one cluster (a canary, a ramp)
   advertise the same string, so a probe cannot tell them apart and would silently collapse a
   weighted rollout the gateway had already split, while the metrics kept attributing traffic to the
   deployment the gateway picked. It is also the exact signal that broke above: a frontend whose model
   list is empty is the thing to detect, not the thing to route on.

## Consequences

- **Cluster selection stays global and device selection local.** The global decision (which
  cluster, which deployment) stays the plane's, and it is not re-decided in the cluster. The
  frontend set is a cluster-local fact resolved cluster-locally. The plane's routing table gains
  nothing and learns nothing about frontends.
- **The frontend is declared state, not an operation.** It is rendered from a recipe like everything
  else the operator renders.
- **A frontend is not a replica.** `observeRollout` selects worker pods on the component label. Two
  frontend pods going ready in seconds would otherwise report a deployment `running` with its weights
  still loading, and that count is what the plane writes as `admission_phase` and the gateway reads as
  readiness.
- **A frontend that is not serving degrades its deployment.** Under one frontend per model it is the
  only way in, so a complete set of ready workers behind a frontend that never started is not
  `running`. It costs nothing to say so: the gateway orders candidates by readiness and never removes
  the last one.
- **A recipe whose `overlay.dynamo` rewrites `spec.components` is refused, not rendered.**
  Lists replace, so such an overlay silently deletes the frontend and leaves a deployment reachable by
  nothing, with every worker healthy. There is no signal for that failure short of a refusal.
- **Pod count grows with models, not with image diversity.** Two frontend pods per deployment where a
  cluster ran two in total. Next to the GPU nodes the workers run on, the frontend tier is the cheap
  half of the trade.
- **The blast radius is not yet contained, and this is the open edge.** Workers and frontends are all
  still pinned to the one global Dynamo namespace, so every frontend still discovers every worker, and
  a model one of them cannot tokenize still empties that frontend's list. Narrowing the namespace is
  a change to the worker's pod spec and therefore a restart of every worker on the cluster. Until
  that narrowing is shown to isolate frontends on a live cluster, the feature is safe on a cluster
  serving one model and is not safe on a cluster serving several. It ships off, per cluster, for that
  reason.

> **Amended 2026-09-02.** Recipe is deleted the same day (ADR 0014), so read point 2 as **the
> workload's `frontend_image`, then the cluster's configured default, then its engine image**: the
> field and its three-step precedence are unchanged, only the object holding them is. The consequence
> about `overlay.dynamo` is likewise a property of the serving overlay wherever it lives, and the
> labels the worker Service was verified to carry were the labels of the day.
