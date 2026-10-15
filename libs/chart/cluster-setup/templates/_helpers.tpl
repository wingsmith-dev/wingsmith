{{/* Operator image; tag falls back to the chart's appVersion. */}}
{{- define "cluster-setup.operatorImage" -}}
{{- $tag := .Values.operator.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" .Values.operator.image.repository $tag -}}
{{- end -}}

{{/* Serving agent image; tag falls back to the chart's appVersion. */}}
{{- define "cluster-setup.servingAgentImage" -}}
{{- $tag := .Values.agent.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" .Values.agent.image.repository $tag -}}
{{- end -}}

{{/*
The gateway address the agent dials. Every reader goes through here rather than
touching .Values.agent.gateway.address, so the check that it is still a map
cannot be outrun: a values.yaml carrying the old flat string reaches whichever
template is rendered first, and without this that is an unattributable
"can't evaluate field address in type interface {}" from inside NOTES.txt.
*/}}
{{- define "cluster-setup.gatewayAddress" -}}
{{- if kindIs "string" .Values.agent.gateway -}}
{{- fail "agent.gateway is a map, not a string — move the address one level down, to agent.gateway.address. It groups the gateway-dialling settings the agent reads (serverName, caFile, connsPerGateway) under the thing they are all about instead of spreading them across agent.tunnelThis and agent.tunnelThat. Nothing else changed: still one address as host:port, still not a list, still no scheme." -}}
{{- end -}}
{{- .Values.agent.gateway.address -}}
{{- end -}}

{{/*
Selector labels for the serving agent. Its own, like the metrics agent's and
for the same reason: sharing the operator's selector would make one Deployment
adopt the other's pods.
*/}}
{{- define "cluster-setup.servingAgentSelectorLabels" -}}
app.kubernetes.io/name: hangar-agent
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
DCGM endpoint: explicit value wins; otherwise derived from the bundled exporter
(service <release>-dcgm-exporter) when it is enabled; empty disables the scrape.
*/}}
{{- define "cluster-setup.dcgmEndpoint" -}}
{{- if .Values.dcgm.endpoint -}}
{{- .Values.dcgm.endpoint -}}
{{- else if (index .Values "dcgm-exporter" "enabled") -}}
{{- printf "http://%s-dcgm-exporter.%s.svc.cluster.local:9400/metrics" .Release.Name .Release.Namespace -}}
{{- end -}}
{{- end -}}

{{/*
DCGM pod selector: how the operator finds the individual exporter pods behind that
Service, so it scrapes every node instead of whichever one the ClusterIP happens to
pick. It must be set, because the operator's own default derives the selector from
the Service name — `<release>-dcgm-exporter` — while the exporter's chart labels its
pods with its own name, `dcgm-exporter`. Nothing matches, the operator falls back to
one GET against the Service, and the fleet's devices appear one random node at a
time. Explicit value wins; otherwise the labels the bundled subchart actually sets.
*/}}
{{- define "cluster-setup.dcgmSelector" -}}
{{- if .Values.dcgm.selector -}}
{{- .Values.dcgm.selector -}}
{{- else if (index .Values "dcgm-exporter" "enabled") -}}
{{- printf "app.kubernetes.io/name=dcgm-exporter,app.kubernetes.io/instance=%s" .Release.Name -}}
{{- end -}}
{{- end -}}

{{/*
Selector labels for the metrics agent. Its own, not the operator's — the agent is
a separate workload and sharing a selector would make one Deployment adopt the
other's pods.
*/}}
{{- define "cluster-setup.agentSelectorLabels" -}}
app.kubernetes.io/name: hangar-metrics-agent
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The Secret the metrics agent mounts for its remote-write password, and the key in
it that holds it. Under `useEnrolmentToken` (the default) both name the enrolment
Secret this chart already creates, so the token is written in one place and the
agent's credential cannot drift from the one the plane verifies; otherwise they
name the separate basicAuth Secret.
*/}}
{{- define "cluster-setup.remoteWriteSecret" -}}
{{- if .Values.monitoring.remoteWrite.basicAuth.useEnrolmentToken -}}
{{- .Values.enrolmentToken.existingSecret -}}
{{- else -}}
{{- .Values.monitoring.remoteWrite.basicAuth.existingSecret -}}
{{- end -}}
{{- end -}}

{{- define "cluster-setup.remoteWritePasswordKey" -}}
{{- if .Values.monitoring.remoteWrite.basicAuth.useEnrolmentToken -}}
{{- .Values.enrolmentToken.secretKey -}}
{{- else -}}
{{- .Values.monitoring.remoteWrite.basicAuth.passwordKey -}}
{{- end -}}
{{- end -}}

{{/*
A value that changes when the enrolment token does, for the pod annotation that
rolls the operator and agent on a rotation. Not a digest of the token: a
Deployment's annotations are readable by anything with `get deployments`, a
wider audience than `get secrets`. derivePassword is a scrypt-stretched
derivation keyed by the cluster id, so the annotation is neither the plane's
lookup key nor cheap to test a guessed token against. Empty when the token is
not in values (a Secret created out of band), which rolls nothing.
*/}}
{{- define "cluster-setup.enrolmentTokenRollKey" -}}
{{- with .Values.enrolmentToken.value -}}
{{- derivePassword 1 "maximum" . (toString $.Values.cluster.id) "enrolment-token.hangar.dev" | sha256sum | trunc 16 -}}
{{- end -}}
{{- end -}}

{{/*
`cluster.allowedOperations` as a clean comma-separated list, from either a
string ("cordon, benchmark") or a YAML list. Unknown kinds are refused rather
than passed on, because the RBAC below is derived from this list and a typo
would otherwise grant nothing and fail only when the plane asks.
*/}}
{{- define "cluster-setup.allowedOperations" -}}
{{- $raw := .Values.cluster.allowedOperations | default list -}}
{{- if kindIs "string" $raw -}}{{- $raw = splitList "," $raw -}}{{- end -}}
{{- $out := list -}}
{{- range $raw -}}
{{- $kind := trim (toString .) -}}
{{- if $kind -}}
{{- if not (has $kind (list "cordon" "drain" "benchmark")) -}}
{{- fail (printf "cluster.allowedOperations names %q — the operations are cordon, drain and benchmark" $kind) -}}
{{- end -}}
{{- $out = append $out $kind -}}
{{- end -}}
{{- end -}}
{{- join "," (uniq $out) -}}
{{- end -}}

{{/*
The operations this operator will perform, which is what its RBAC is cut to:
every kind on a managed cluster, and only the allowed ones on a read-only one.
*/}}
{{- define "cluster-setup.operations" -}}
{{- if .Values.cluster.readOnly -}}
{{- include "cluster-setup.allowedOperations" . -}}
{{- else -}}
cordon,drain,benchmark
{{- end -}}
{{- end -}}

{{/* The namespace the operator renders workloads into. */}}
{{- define "cluster-setup.workloadNamespace" -}}
{{- required "workloadNamespace.name is required" .Values.workloadNamespace.name -}}
{{- end -}}

{{/* Common labels on hangar-owned objects. */}}
{{- define "cluster-setup.labels" -}}
app.kubernetes.io/name: hangar-operator
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: hangar
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/* Fail early on values that have no safe default. */}}
{{- define "cluster-setup.validate" -}}
{{- if not .Values.cluster.id -}}
{{- fail "cluster.id is required — it must match the name of a cluster already declared in the plane" -}}
{{- end -}}
{{- if and (not .Values.enrolmentToken.value) (not .Values.enrolmentToken.existing) -}}
{{- if and .Values.monitoring.enabled .Values.monitoring.remoteWrite.basicAuth.useEnrolmentToken -}}
{{- fail "enrolmentToken.value is required, and with monitoring.remoteWrite.basicAuth.useEnrolmentToken it is needed twice over: the operator reads the token from a Secret this chart creates, so without it that pod starts into CreateContainerConfigError, and the metrics agent presents the same token to the central store's write ingress — whose htpasswd the plane filled in when it minted it — so a missing one also means an agent that scrapes fine and is rejected with 401 on every send, visible only in its own logs. Mint the token on the register screen and install from the values.yaml it renders. Set enrolmentToken.existing=true if you created that Secret yourself, or useEnrolmentToken=false with an explicit basicAuth username and password for a store that is not hangar's." -}}
{{- else -}}
{{- fail "enrolmentToken.value is required: mint the cluster's token on the register screen and install from the values.yaml it renders. The operator Deployment reads the token from a Secret this chart creates, so without it the pod starts into CreateContainerConfigError. Set enrolmentToken.existing=true only if you created that Secret yourself." -}}
{{- end -}}
{{- end -}}
{{- if not (hasPrefix "wss://" .Values.cluster.hubUrl) -}}
{{- if not (hasPrefix "ws://" .Values.cluster.hubUrl) -}}
{{- fail "cluster.hubUrl must start with wss:// — it is the plane's hub endpoint, e.g. wss://plane.example.com/v1/hub/sync" -}}
{{- else if not .Values.hub.allowInsecure -}}
{{- fail "cluster.hubUrl is ws://, which sends this cluster's enrolment token in the clear. Use the hub's wss:// address, with hub.caBundle if its certificate is from a private CA. hub.allowInsecure=true accepts ws:// for a local development plane only." -}}
{{- end -}}
{{- end -}}
{{- if .Values.imagePullSecret.create -}}
{{- if not .Values.imagePullSecret.name -}}
{{- fail "imagePullSecret.create is true but imagePullSecret.name is empty — the chart would build a Secret with no name, and nothing would reference it. Name it, or set create=false when the images need no credential." -}}
{{- end -}}
{{- if not (and .Values.imagePullSecret.username .Values.imagePullSecret.token) -}}
{{- fail "imagePullSecret.create is true but username or token is empty — that renders a Secret carrying the base64 of \":\", which the kubelet accepts and the registry then refuses, so the failure is an ImagePullBackOff on the operator rather than a refusal here. A GHCR token needs read:packages and nothing else." -}}
{{- end -}}
{{- end -}}
{{- if .Values.agent.enabled -}}
{{- if .Values.cluster.readOnly -}}
{{- fail "agent.enabled is true but cluster.readOnly is also true — a read-only cluster cannot serve. Serving a model means the operator applying what the plane declared, and read-only reconciles nothing, so the agent would come up, hold healthy tunnels to every gateway, advertise this cluster as reachable, and answer every inference request with a 502 for a frontend that was never deployed. That is worse than a refusal: it is an install that looks clean and never works, and the gateway would route real traffic to it. Set cluster.readOnly=false once this cluster is managed, or leave agent.enabled=false until it is." -}}
{{- end -}}
{{- $gateway := include "cluster-setup.gatewayAddress" . -}}
{{- if not $gateway -}}
{{- fail "agent.enabled is true but agent.gateway.address is empty — set it to the gateway's public tunnel address, as host:port. There is no default: an agent with nowhere to dial installs cleanly and carries nothing, and the gateway sees this cluster as unreachable with no reason attached. One address, not a list of replicas: how many replicas there are is reported in the handshake, so the agent finds them all from this one name (infra/80-gateway.yaml)." -}}
{{- end -}}
{{- if contains "," $gateway -}}
{{- fail "agent.gateway.address holds a comma-separated list, but it takes one address. Earlier values named every gateway replica; the agent now discovers them from the handshake, so give it the gateway's single public tunnel address and delete the rest." -}}
{{- end -}}
{{- if regexMatch "^[a-zA-Z][a-zA-Z0-9+.-]*://" $gateway -}}
{{- fail "agent.gateway.address is an address and not a URL — give it as host:port, with no scheme. TLS is not optional on this dial and is assumed, so a scheme carries nothing the agent can act on; the value is passed to net.Dial, where a scheme becomes part of the hostname and fails to resolve." -}}
{{- end -}}
{{- end -}}
{{- if not (has .Values.frontend.mode (list "per-model" "shared" "none")) -}}
{{- fail (printf "frontend.mode is %q — it takes per-model, shared, or none. per-model renders a frontend into each model's own graph on that model's own image (ADR 0013, and the default). shared renders the one cluster-wide frontend this chart installs, which every model is then served through and every model must therefore be servable by. none renders neither, which is what the first install of a new cluster needs and what a frontend installed outside this chart wants beside agent.frontendEndpoint." .Values.frontend.mode) -}}
{{- end -}}
{{- if hasKey .Values.frontend "enabled" -}}
{{- fail "frontend.enabled was removed — use frontend.mode. It and operator.frontend.enabled were two booleans in two blocks for one choice, and nothing stopped them contradicting each other. frontend.enabled=true is now frontend.mode=shared, frontend.enabled=false on a first install is frontend.mode=none, and the topology a cluster ends on is frontend.mode=per-model, which is the default. The per-model knobs moved with it, from operator.frontend.* to frontend.perModel.*; the shared frontend's image, env and resources are frontend.shared.*; replicas, imagePullSecrets and anthropicApi are shared by both modes and stayed at frontend.*." -}}
{{- end -}}
{{- if and (hasKey .Values.operator "frontend") -}}
{{- fail "operator.frontend was removed — its fields are frontend.perModel.* now, and the switch that turned it on is frontend.mode=per-model (the default). Left where it was, it read as a second frontend that composed with the cluster-wide one, when the two are alternatives: a cluster rendering a frontend per model has no use for a shared one, and running both leaves a second way in that nothing routes to. Move image, globalDynamoNamespace and resources under frontend.perModel; replicas, imagePullSecrets and anthropicApi are common to both modes and live at frontend.*." -}}
{{- end -}}
{{- if and .Values.agent.enabled (eq .Values.frontend.mode "none") (not .Values.agent.frontendEndpoint) -}}
{{- fail "agent.enabled is true but frontend.mode is none, so this cluster has no frontend to forward to — set frontend.mode=per-model (or shared), or agent.frontendEndpoint if the frontend is something this chart did not install. The agent proxies every inference request to a Dynamo frontend it finds by label, so without one it comes up healthy, holds tunnels to every gateway, advertises this cluster as reachable, and answers real traffic with a 502 that names a Service nobody deployed. Note the ordering on a cluster being installed for the FIRST time: a frontend is a DynamoGraphDeployment in either topology, and that CRD is applied by the Dynamo operator at runtime, so no frontend can go on in the first install — and this refusal means the agent cannot either. Install once with frontend.mode=none and agent.enabled=false, which gives you the control path and an enrolled cluster, then upgrade with the mode you want and the agent on. Changing the mode later on a cluster already running models is not a configuration change: Dynamo holds a graph's component set immutable after creation, so each existing graph has to be deleted and rendered again, and each model reloads its weights when it is." -}}
{{- end -}}
{{- if and .Values.serving.enabled (not .Values.serving.backend.service) -}}
{{- fail "serving.enabled is true but serving.backend.service is empty — set it to the Dynamo frontend Service name" -}}
{{- end -}}
{{- if and .Values.serving.enabled (not .Values.serving.middlewares) (not .Values.serving.allowUnauthenticated) -}}
{{- fail "serving.enabled is true but serving.middlewares is empty — the route would put an inference frontend, which authenticates nobody, on a public hostname. Name a Traefik Middleware that authenticates callers (serving.middlewares: [{name: ..., namespace: ...}]), or set serving.allowUnauthenticated=true if that is really intended." -}}
{{- end -}}
{{- if and .Values.serving.enabled .Values.networkPolicy.enabled (not .Values.networkPolicy.frontendFrom) -}}
{{- fail "serving.enabled is true with networkPolicy.enabled, but networkPolicy.frontendFrom is empty — the frontend policy would drop every request the route forwards. Add the ingress controller's namespace, e.g. networkPolicy.frontendFrom: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: traefik}}}]." -}}
{{- end -}}
{{- if .Values.monitoring.enabled -}}
{{- if not .Values.monitoring.remoteWrite.url -}}
{{- fail "monitoring.enabled is true but monitoring.remoteWrite.url is empty — set it to the central store's write endpoint (https://metrics.example.com/api/v1/write). An agent with no remote_write scrapes the cluster and discards every sample, which looks like a healthy install and produces empty console panels." -}}
{{- end -}}
{{- if not .Values.cluster.region -}}
{{- fail "monitoring.enabled is true but cluster.region is empty — it becomes the `region` external label on every series this cluster writes. Left blank the series land in the central store unattributed and cannot be told apart from another region's in a fleet-wide breakdown." -}}
{{- end -}}
{{- if index .Values "dcgm-exporter" "serviceMonitor" "enabled" -}}
{{- fail "monitoring.enabled and dcgm-exporter.serviceMonitor.enabled are both true — pick one. This chart installs no prometheus-operator, so the ServiceMonitor either fails to apply for a missing CRD or, where an operator does exist, adds a second scraper writing the same DCGM series twice. The metrics agent already discovers the exporter's pods by label." -}}
{{- end -}}
{{- if and .Values.monitoring.remoteWrite.basicAuth.useEnrolmentToken .Values.monitoring.remoteWrite.basicAuth.existing -}}
{{- fail "monitoring.remoteWrite.basicAuth has both useEnrolmentToken and existing set — pick one. useEnrolmentToken makes the agent read the enrolment Secret, so the Secret you created would be mounted by nothing and ignored in silence. Set useEnrolmentToken=false to use it." -}}
{{- end -}}
{{- if and (not .Values.monitoring.remoteWrite.basicAuth.useEnrolmentToken) (not .Values.monitoring.remoteWrite.basicAuth.existing) (or (not .Values.monitoring.remoteWrite.basicAuth.username) (not .Values.monitoring.remoteWrite.basicAuth.password)) -}}
{{- fail "monitoring.remoteWrite.basicAuth.useEnrolmentToken is false, so the credential must be supplied: without a username and password the write ingress rejects the agent with 401 on every send, which surfaces only in the agent's own logs. Set both, or basicAuth.existing=true if you created the Secret yourself — or drop useEnrolmentToken back to true and let the enrolment token be the credential." -}}
{{- end -}}
{{- end -}}
{{- end -}}
