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
{{- end -}}
