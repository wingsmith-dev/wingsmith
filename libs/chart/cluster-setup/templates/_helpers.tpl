{{/*
An image reference from {repository, tag, digest}: the digest when one is set,
which pins the exact build whatever the tag now points at, and otherwise the tag,
falling back to the chart's appVersion.
*/}}
{{- define "cluster-setup.image" -}}
{{- $image := .image -}}
{{- if $image.digest -}}
{{- if not (hasPrefix "sha256:" $image.digest) -}}
{{- fail (printf "image digest %q must start with sha256:" $image.digest) -}}
{{- end -}}
{{- printf "%s@%s" $image.repository $image.digest -}}
{{- else -}}
{{- printf "%s:%s" $image.repository ($image.tag | default .appVersion) -}}
{{- end -}}
{{- end -}}

{{- define "cluster-setup.operatorImage" -}}
{{- include "cluster-setup.image" (dict "image" .Values.operator.image "appVersion" .Chart.AppVersion) -}}
{{- end -}}

{{- define "cluster-setup.servingAgentImage" -}}
{{- include "cluster-setup.image" (dict "image" .Values.agent.image "appVersion" .Chart.AppVersion) -}}
{{- end -}}

{{/*
The gateway address the agent dials. Every reader goes through here instead of
touching .Values.agent.gateway.address, so the check that it is still a map
cannot be outrun: a values.yaml carrying the old flat string reaches whichever
template is rendered first, and without this that is an unattributable
"can't evaluate field address in type interface {}" from inside NOTES.txt.
*/}}
{{- define "cluster-setup.gatewayAddress" -}}
{{- if kindIs "string" .Values.agent.gateway -}}
{{- fail "agent.gateway is a map, not a string: move the address one level down, to agent.gateway.address. It groups the gateway-dialling settings the agent reads (serverName, caFile, connsPerGateway) under the thing they are all about instead of spreading them across agent.tunnelThis and agent.tunnelThat. Nothing else changed: still one address as host:port, still not a list, still no scheme." -}}
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
the Service name (`<release>-dcgm-exporter`) while the exporter's chart labels its
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
Selector labels for the metrics agent. Its own, not the operator's: the agent is
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
name the separate basicAuth Secret. With `bearerToken.existingSecret` they name
that Secret and its token key.
*/}}
{{- define "cluster-setup.remoteWriteSecret" -}}
{{- if .Values.monitoring.remoteWrite.bearerToken.existingSecret -}}
{{- .Values.monitoring.remoteWrite.bearerToken.existingSecret -}}
{{- else if .Values.monitoring.remoteWrite.basicAuth.useEnrolmentToken -}}
{{- .Values.enrolmentToken.existingSecret -}}
{{- else -}}
{{- .Values.monitoring.remoteWrite.basicAuth.existingSecret -}}
{{- end -}}
{{- end -}}

{{- define "cluster-setup.remoteWritePasswordKey" -}}
{{- if .Values.monitoring.remoteWrite.bearerToken.existingSecret -}}
{{- .Values.monitoring.remoteWrite.bearerToken.key -}}
{{- else if .Values.monitoring.remoteWrite.basicAuth.useEnrolmentToken -}}
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
string ("cordon, benchmark") or a YAML list. Unknown kinds are refused instead
of passed on, because the RBAC below is derived from this list and a typo
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
{{- fail (printf "cluster.allowedOperations names %q: the operations are cordon, drain and benchmark" $kind) -}}
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
{{- fail "cluster.id is required: it must match the name of a cluster already declared in the plane" -}}
{{- end -}}
{{- if and (not .Values.enrolmentToken.value) (not .Values.enrolmentToken.existing) -}}
{{- if and .Values.monitoring.enabled .Values.monitoring.remoteWrite.basicAuth.useEnrolmentToken -}}
{{- fail "enrolmentToken.value is required, and with monitoring.remoteWrite.basicAuth.useEnrolmentToken it is needed twice over: the operator reads the token from a Secret this chart creates, so without it that pod starts into CreateContainerConfigError, and the metrics agent presents the same token to the central store's write ingress, whose htpasswd the plane filled in when it minted it, so a missing one also means an agent that scrapes fine and is rejected with 401 on every send, visible only in its own logs. Mint the token on the register screen and install from the values.yaml it renders. Set enrolmentToken.existing=true if you created that Secret yourself, or useEnrolmentToken=false with an explicit basicAuth username and password for a store that is not hangar's." -}}
{{- else -}}
{{- fail "enrolmentToken.value is required: mint the cluster's token on the register screen and install from the values.yaml it renders. The operator Deployment reads the token from a Secret this chart creates, so without it the pod starts into CreateContainerConfigError. Set enrolmentToken.existing=true only if you created that Secret yourself." -}}
{{- end -}}
{{- end -}}
{{- if not (hasPrefix "wss://" .Values.cluster.hubUrl) -}}
{{- if not (hasPrefix "ws://" .Values.cluster.hubUrl) -}}
{{- fail "cluster.hubUrl must start with wss://; it is the plane's hub endpoint, e.g. wss://plane.example.com/v1/hub/sync" -}}
{{- else if not .Values.hub.allowInsecure -}}
{{- fail "cluster.hubUrl is ws://, which sends this cluster's enrolment token in the clear. Use the hub's wss:// address, with hub.caBundle if its certificate is from a private CA. hub.allowInsecure=true accepts ws:// for a local development plane only." -}}
{{- end -}}
{{- end -}}
{{- if .Values.imagePullSecret.create -}}
{{- if not .Values.imagePullSecret.name -}}
{{- fail "imagePullSecret.create is true but imagePullSecret.name is empty: the chart would build a Secret with no name, and nothing would reference it. Name it, or set create=false when the images need no credential." -}}
{{- end -}}
{{- if not (and .Values.imagePullSecret.username .Values.imagePullSecret.token) -}}
{{- fail "imagePullSecret.create is true but username or token is empty: that renders a Secret carrying the base64 of \":\", which the kubelet accepts and the registry then refuses, so the failure is an ImagePullBackOff on the operator instead of a refusal here. A GHCR token needs read:packages and nothing else." -}}
{{- end -}}
{{- end -}}
{{- if .Values.agent.enabled -}}
{{- if .Values.cluster.readOnly -}}
{{- fail "agent.enabled is true but cluster.readOnly is also true: a read-only cluster cannot serve. Serving a model means the operator applying what the plane declared, and read-only reconciles nothing, so the agent would come up, hold healthy tunnels to every gateway, advertise this cluster as reachable, and answer every inference request with a 502 for a frontend that was never deployed. That is worse than a refusal: it is an install that looks clean and never works, and the gateway would route real traffic to it. Set cluster.readOnly=false once this cluster is managed, or leave agent.enabled=false until it is." -}}
{{- end -}}
{{- $gateway := include "cluster-setup.gatewayAddress" . -}}
{{- if not $gateway -}}
{{- fail "agent.enabled is true but agent.gateway.address is empty: set it to the gateway's public tunnel address, as host:port. There is no default: an agent with nowhere to dial installs cleanly and carries nothing, and the gateway sees this cluster as unreachable with no reason attached. One address, not a list of replicas: how many replicas there are is reported in the handshake, so the agent finds them all from this one name." -}}
{{- end -}}
{{- if contains "," $gateway -}}
{{- fail "agent.gateway.address holds a comma-separated list, but it takes one address. Earlier values named every gateway replica; the agent now discovers them from the handshake, so give it the gateway's single public tunnel address and delete the rest." -}}
{{- end -}}
{{- if regexMatch "^[a-zA-Z][a-zA-Z0-9+.-]*://" $gateway -}}
{{- fail "agent.gateway.address is an address and not a URL: give it as host:port, with no scheme. TLS is not optional on this dial and is assumed, so a scheme carries nothing the agent can act on; the value is passed to net.Dial, where a scheme becomes part of the hostname and fails to resolve." -}}
{{- end -}}
{{- end -}}
{{- if not (has .Values.frontend.mode (list "per-model" "shared" "none")) -}}
{{- fail (printf "frontend.mode is %q: it takes per-model, shared, or none. per-model renders a frontend into each model's own graph on that model's own image (ADR 0013, and the default). shared renders the one cluster-wide frontend this chart installs, which every model is then served through and every model must therefore be servable by. none renders neither, which is what the first install of a new cluster needs and what a frontend installed outside this chart wants beside agent.frontendEndpoint." .Values.frontend.mode) -}}
{{- end -}}
{{- if hasKey .Values.frontend "enabled" -}}
{{- fail "frontend.enabled was removed: use frontend.mode. It and operator.frontend.enabled were two booleans in two blocks for one choice, and nothing stopped them contradicting each other. frontend.enabled=true is now frontend.mode=shared, frontend.enabled=false on a first install is frontend.mode=none, and the topology a cluster ends on is frontend.mode=per-model, which is the default. The per-model knobs moved with it, from operator.frontend.* to frontend.perModel.*; the shared frontend's image, env and resources are frontend.shared.*; replicas, imagePullSecrets and anthropicApi are shared by both modes and stayed at frontend.*." -}}
{{- end -}}
{{- if and (hasKey .Values.operator "frontend") -}}
{{- fail "operator.frontend was removed: its fields are frontend.perModel.* now, and the switch that turned it on is frontend.mode=per-model (the default). Left where it was, it read as a second frontend that composed with the cluster-wide one, when the two are alternatives: a cluster rendering a frontend per model has no use for a shared one, and running both leaves a second way in that nothing routes to. Move image, globalDynamoNamespace and resources under frontend.perModel; replicas, imagePullSecrets and anthropicApi are common to both modes and live at frontend.*." -}}
{{- end -}}
{{- if and .Values.agent.enabled (eq .Values.frontend.mode "none") (not .Values.agent.frontendEndpoint) -}}
{{- fail "agent.enabled is true but frontend.mode is none, so this cluster has no frontend to forward to: set frontend.mode=per-model (or shared), or agent.frontendEndpoint if the frontend is something this chart did not install. The agent proxies every inference request to a Dynamo frontend it finds by label, so without one it comes up healthy, holds tunnels to every gateway, advertises this cluster as reachable, and answers real traffic with a 502 that names a Service nobody deployed. Note the ordering on a cluster being installed for the FIRST time: a frontend is a DynamoGraphDeployment in either topology, and that CRD is applied by the Dynamo operator at runtime, so no frontend can go on in the first install, and this refusal means the agent cannot either. Install once with frontend.mode=none and agent.enabled=false, which gives you the control path and an enrolled cluster, then upgrade with the mode you want and the agent on. Changing the mode later on a cluster already running models is not a configuration change: Dynamo holds a graph's component set immutable after creation, so each existing graph has to be deleted and rendered again, and each model reloads its weights when it is." -}}
{{- end -}}
{{- if and .Values.serving.enabled (not .Values.serving.backend.service) -}}
{{- fail "serving.enabled is true but serving.backend.service is empty: set it to the Dynamo frontend Service name" -}}
{{- end -}}
{{- if and .Values.serving.enabled (not .Values.serving.middlewares) (not .Values.serving.allowUnauthenticated) -}}
{{- fail "serving.enabled is true but serving.middlewares is empty: the route would put an inference frontend, which authenticates nobody, on a public hostname. Name a Traefik Middleware that authenticates callers (serving.middlewares: [{name: ..., namespace: ...}]), or set serving.allowUnauthenticated=true if that is really intended." -}}
{{- end -}}
{{- if and .Values.serving.enabled .Values.networkPolicy.enabled (not .Values.networkPolicy.frontendFrom) -}}
{{- fail "serving.enabled is true with networkPolicy.enabled, but networkPolicy.frontendFrom is empty: the frontend policy would drop every request the route forwards. Add the ingress controller's namespace, e.g. networkPolicy.frontendFrom: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: traefik}}}]." -}}
{{- end -}}
{{- if and .Values.networkPolicy.enabled .Values.networkPolicy.servingEgress.enabled (not .Values.networkPolicy.servingEgress.apiServer) -}}
{{- fail "networkPolicy.servingEgress.enabled is true but networkPolicy.servingEgress.apiServer is empty. Dynamo's frontends and workers discover each other through the Kubernetes API, so a policy that drops it leaves every model unreachable. List the API server's endpoint CIDRs (kubectl get endpoints kubernetes), e.g. apiServer: [\"10.0.0.10/32\"]." -}}
{{- end -}}
{{- $fabric := .Values.fabric.type | default "" | toString -}}
{{- if not (has $fabric (list "" "efa" "rdma")) -}}
{{- fail (printf "fabric.type is %q: it takes efa (AWS Elastic Fabric Adapter), rdma (any other RDMA device plugin), or empty for none." $fabric) -}}
{{- end -}}
{{- if and (not $fabric) (gt (int .Values.fabric.devices) 0) -}}
{{- fail "fabric.devices is set but fabric.type is empty. Name the fabric: fabric.type=efa for AWS Elastic Fabric Adapter (what fabric.devices alone used to mean), or fabric.type=rdma with fabric.resource for another RDMA device plugin." -}}
{{- end -}}
{{- if and $fabric (lt (int .Values.fabric.devices) 1) -}}
{{- fail (printf "fabric.type is %s but fabric.devices is 0, so worker pods would ask for no interfaces. Set fabric.devices to the interfaces one worker pod uses, or clear fabric.type." $fabric) -}}
{{- end -}}
{{- if and (eq $fabric "rdma") (not .Values.fabric.resource) -}}
{{- fail "fabric.type is rdma but fabric.resource is empty. Name the device plugin's extended resource (rdma/hca, rdma/ib or your vendor's); read it off a GPU node's .status.allocatable." -}}
{{- end -}}
{{- if .Values.monitoring.enabled -}}
{{- if not .Values.monitoring.remoteWrite.url -}}
{{- fail "monitoring.enabled is true but monitoring.remoteWrite.url is empty: set it to the central store's write endpoint (https://metrics.example.com/api/v1/write). An agent with no remote_write scrapes the cluster and discards every sample, which looks like a healthy install and produces empty console panels." -}}
{{- end -}}
{{- if not .Values.cluster.region -}}
{{- fail "monitoring.enabled is true but cluster.region is empty: it becomes the `region` external label on every series this cluster writes. Left blank the series land in the central store unattributed and cannot be told apart from another region's in a fleet-wide breakdown." -}}
{{- end -}}
{{- if index .Values "dcgm-exporter" "serviceMonitor" "enabled" -}}
{{- fail "monitoring.enabled and dcgm-exporter.serviceMonitor.enabled are both true: pick one. This chart installs no prometheus-operator, so the ServiceMonitor either fails to apply for a missing CRD or, where an operator does exist, adds a second scraper writing the same DCGM series twice. The metrics agent already discovers the exporter's pods by label." -}}
{{- end -}}
{{- if and .Values.monitoring.remoteWrite.basicAuth.useEnrolmentToken .Values.monitoring.remoteWrite.basicAuth.existing -}}
{{- fail "monitoring.remoteWrite.basicAuth has both useEnrolmentToken and existing set: pick one. useEnrolmentToken makes the agent read the enrolment Secret, so the Secret you created would be mounted by nothing and ignored in silence. Set useEnrolmentToken=false to use it." -}}
{{- end -}}
{{- if and .Values.monitoring.remoteWrite.bearerToken.existingSecret .Values.monitoring.remoteWrite.basicAuth.useEnrolmentToken -}}
{{- fail "monitoring.remoteWrite.bearerToken.existingSecret is set while basicAuth.useEnrolmentToken is true. Pick one: the bearer token replaces basic auth, so set basicAuth.useEnrolmentToken=false to send it, or clear bearerToken.existingSecret to keep presenting the enrolment token." -}}
{{- end -}}
{{- if and .Values.monitoring.remoteWrite.bearerToken.existingSecret (or .Values.monitoring.remoteWrite.basicAuth.existing .Values.monitoring.remoteWrite.basicAuth.username .Values.monitoring.remoteWrite.basicAuth.password) -}}
{{- fail "monitoring.remoteWrite.bearerToken.existingSecret is set alongside a basicAuth credential (existing, username or password). Pick one: the bearer token replaces basic auth, so the agent would send only the token and the basic-auth credential would be ignored in silence. Clear basicAuth.existing, username and password to send the token, or clear bearerToken.existingSecret to use basic auth." -}}
{{- end -}}
{{- if and (not .Values.monitoring.remoteWrite.bearerToken.existingSecret) (not .Values.monitoring.remoteWrite.basicAuth.useEnrolmentToken) (not .Values.monitoring.remoteWrite.basicAuth.existing) (or (not .Values.monitoring.remoteWrite.basicAuth.username) (not .Values.monitoring.remoteWrite.basicAuth.password)) -}}
{{- fail "monitoring.remoteWrite.basicAuth.useEnrolmentToken is false, so the credential must be supplied: without a username and password the write ingress rejects the agent with 401 on every send, which surfaces only in the agent's own logs. Set both, or basicAuth.existing=true if you created the Secret yourself, or drop useEnrolmentToken back to true and let the enrolment token be the credential." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The operator's own state in the release namespace: the leader-election lease,
the local copy of declared state and the stream cursor. Written read-only or
not; a read-only operator still stores what the plane declares so that leaving
read-only starts from a complete view.
*/}}
{{- define "cluster-setup.operatorStateRules" -}}
- apiGroups: [""]
  resources: [events]
  verbs: [create, patch]
- apiGroups: [coordination.k8s.io]
  resources: [leases]
  verbs: [create, get, list, patch, update, watch]
- apiGroups: [hangar.dev]
  resources: [syncedresources]
  verbs: [create, delete, get, list, patch, update, watch]
- apiGroups: [hangar.dev]
  resources: [syncedresources/status]
  verbs: [get, patch, update]
- apiGroups: [hangar.dev]
  resources: [streamcheckpoints]
  verbs: [create, get, list, patch, update, watch]
{{- end -}}

{{/*
What the operator renders into the workload namespace: graph deployments and
prefetch Jobs when it reconciles, benchmark Jobs, their logs and the frontend
Service when benchmarks are allowed. Empty on a read-only cluster with no
benchmark.
*/}}
{{- define "cluster-setup.operatorWorkloadRules" -}}
{{- $ops := splitList "," (include "cluster-setup.operations" .) -}}
{{- $reconcile := not .Values.cluster.readOnly -}}
{{- $benchmark := has "benchmark" $ops -}}
{{- if or $reconcile $benchmark }}
- apiGroups: [""]
  resources: [events]
  verbs: [create, patch]
- apiGroups: [batch]
  resources: [jobs]
  verbs: [create, delete, get, list, watch]
- apiGroups: [batch]
  resources: [jobs/status]
  verbs: [get]
{{- end }}
{{- if $reconcile }}
- apiGroups: [nvidia.com]
  resources: [dynamographdeployments]
  verbs: [create, delete, get, list, patch, update, watch]
- apiGroups: [nvidia.com]
  resources: [dynamographdeployments/status]
  verbs: [get]
# Not an object this operator renders. It is where the Dynamo operator writes
# down that it could not do what a graph deployment asked, and the graph's own
# condition only names the child instead of repeating why. `delete` is here for
# one narrow reason: Dynamo creates one of these per spec hash and never collects
# the one it superseded, so an edited recipe leaves a crash-looping worker
# holding a full replica's worth of accelerators and the replacement can never
# schedule. See supersede.go in the operator for the guards on which one is
# removed.
- apiGroups: [nvidia.com]
  resources: [dynamocomponentdeployments]
  verbs: [delete, get, list, watch]
{{- end }}
{{- if $benchmark }}
- apiGroups: [""]
  resources: [services]
  verbs: [get, list, watch]
- apiGroups: [""]
  resources: [pods/log]
  verbs: [get]
{{- end }}
{{- end -}}

{{/* Binds the operator's Role in one namespace. Takes (list $ namespace). */}}
{{- define "cluster-setup.operatorRoleBinding" -}}
{{- $ := index . 0 -}}
{{- $ns := index . 1 }}
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: hangar-operator
  namespace: {{ $ns }}
  labels:
    {{- include "cluster-setup.labels" $ | nindent 4 }}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: hangar-operator
subjects:
  - kind: ServiceAccount
    name: hangar-operator
    namespace: {{ $.Release.Namespace }}
{{- end -}}
