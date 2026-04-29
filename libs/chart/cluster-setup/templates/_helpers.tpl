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
{{- if not (or (hasPrefix "ws://" .Values.cluster.hubUrl) (hasPrefix "wss://" .Values.cluster.hubUrl)) -}}
{{- fail "cluster.hubUrl must start with ws:// or wss://" -}}
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
{{- if and .Values.serving.enabled (not .Values.serving.backend.service) -}}
{{- fail "serving.enabled is true but serving.backend.service is empty — set it to the Dynamo frontend Service name" -}}
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
