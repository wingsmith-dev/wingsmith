{{/* Operator image; tag falls back to the chart's appVersion. */}}
{{- define "cluster-setup.operatorImage" -}}
{{- $tag := .Values.operator.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" .Values.operator.image.repository $tag -}}
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
{{- if not (or (hasPrefix "ws://" .Values.cluster.hubUrl) (hasPrefix "wss://" .Values.cluster.hubUrl)) -}}
{{- fail "cluster.hubUrl must start with ws:// or wss://" -}}
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
{{- if and (not .Values.monitoring.remoteWrite.basicAuth.existing) (or (not .Values.monitoring.remoteWrite.basicAuth.username) (not .Values.monitoring.remoteWrite.basicAuth.password)) -}}
{{- fail "monitoring.remoteWrite.basicAuth needs a username and password: the central store's write ingress is behind basicAuth and rejects the agent with 401 on every send otherwise, which surfaces only in the agent's own logs. Set monitoring.remoteWrite.basicAuth.existing=true if you created the Secret yourself." -}}
{{- end -}}
{{- end -}}
{{- end -}}
