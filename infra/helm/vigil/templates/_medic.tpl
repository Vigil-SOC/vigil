{{/*
Medic (System Watcher) helpers. Medic and its gateway carry their OWN label set,
never the chart's selector pair (vigil.selectorLabels): the chart's catch-all
`-deny-all` policy selects that pair and allows all egress, and policies add up,
so the standard labels would hand Medic unrestricted egress the moment
networkPolicies.enabled is turned on (C3 §4.4, "the label trap"). commonLabels
stay off the pod templates for the same reason.
*/}}

{{- define "vigil.medic.fullname" -}}
{{- printf "%s-medic" (include "vigil.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "vigil.medicGateway.fullname" -}}
{{- printf "%s-medic-gateway" (include "vigil.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Usage: include "vigil.medic.selectorLabels" (dict "context" . "component" "medic") */}}
{{- define "vigil.medic.selectorLabels" -}}
app.kubernetes.io/name: {{ printf "%s-%s" (include "vigil.name" .context) .component | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/instance: {{ .context.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "vigil.medic.labels" -}}
helm.sh/chart: {{ include "vigil.chart" .context }}
{{ include "vigil.medic.selectorLabels" . }}
app.kubernetes.io/version: {{ .context.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .context.Release.Service }}
app.kubernetes.io/part-of: vigil
{{- end -}}

{{/* Usage: include "vigil.medic.image" (dict "context" . "image" .Values.medic.image "suffix" "medic") */}}
{{- define "vigil.medic.image" -}}
{{- $ctx := .context -}}
{{- $repo := .image.repository | default (printf "%s/%s-%s" $ctx.Values.global.imageRegistry $ctx.Values.global.imageNamespace .suffix) -}}
{{- printf "%s:%s" $repo (.image.tag | default ($ctx.Chart.AppVersion | toString)) -}}
{{- end -}}

{{- define "vigil.medic.pullPolicy" -}}
{{- .image.pullPolicy | default .context.Values.global.imagePullPolicy | default "IfNotPresent" -}}
{{- end -}}

{{/* Medic's own API port (X2) and the gateway's two listeners (S5-3). */}}
{{- define "vigil.medic.apiPort" -}}8470{{- end -}}
{{- define "vigil.medicGateway.outPort" -}}8471{{- end -}}
{{- define "vigil.medicGateway.inPort" -}}8470{{- end -}}

{{/*
The Kubernetes API server's CIDRs for Medic's egress policy (C3 §4.4), as a JSON
list. medic.kubeApi.cidrs wins; otherwise the `kubernetes` EndpointSlice (then
Endpoints) is looked up. `helm template` and GitOps tools can't look anything
up, so there the value is required. A rule on the `kubernetes` Service address
would silently not match on plugins that apply policy after the Service is
translated to its endpoint, hence the endpoint addresses.
*/}}
{{- define "vigil.medic.kubeApiCidrs" -}}
{{- $cidrs := list -}}
{{- range .Values.medic.kubeApi.cidrs -}}
{{- $cidrs = append $cidrs (toString .) -}}
{{- end -}}
{{- if not $cidrs -}}
  {{- $slice := lookup "discovery.k8s.io/v1" "EndpointSlice" "default" "kubernetes" -}}
  {{- range ($slice.endpoints | default list) -}}
    {{- range .addresses -}}
      {{- $cidrs = append $cidrs (printf "%s/%s" . (ternary "128" "32" (contains ":" .))) -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- if not $cidrs -}}
  {{- $ep := lookup "v1" "Endpoints" "default" "kubernetes" -}}
  {{- range ($ep.subsets | default list) -}}
    {{- range .addresses -}}
      {{- $cidrs = append $cidrs (printf "%s/%s" .ip (ternary "128" "32" (contains ":" .ip))) -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- if not $cidrs -}}
  {{- fail "medic.kubeApi.cidrs is required: Medic's egress policy needs the Kubernetes API server's address, and it couldn't be looked up (helm template, Argo CD and Flux can't). Set it from `kubectl get endpointslice kubernetes -n default`, e.g. --set medic.kubeApi.cidrs[0]=10.0.0.1/32" -}}
{{- end -}}
{{- range $cidrs -}}
  {{- if not (or (regexMatch "^([0-9]{1,3}\\.){3}[0-9]{1,3}/([1-9]|[12][0-9]|3[0-2])$" .) (regexMatch "^[0-9a-fA-F:]+/([1-9]|[1-9][0-9]|1[01][0-9]|12[0-8])$" .)) -}}
    {{- fail (printf "medic.kubeApi.cidrs: %q isn't a CIDR with a non-zero prefix (the API server's address, not \"anywhere\")" .) -}}
  {{- end -}}
{{- end -}}
{{- toJson $cidrs -}}
{{- end -}}

{{/* The DNS egress rule both Medic pods share. */}}
{{- define "vigil.medic.dnsEgress" -}}
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: {{ .Values.medic.dns.namespace }}
      podSelector:
        matchLabels:
          {{- toYaml .Values.medic.dns.podLabels | nindent 10 }}
  ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
{{- end -}}

{{/* Fail early, with the key to set, on what a Medic install can't do without. */}}
{{- define "vigil.medic.required" -}}
{{- $_ := required "medic.gateway.viewer.username is required when medic.enabled: the Viewer account the gateway logs in as" .Values.medic.gateway.viewer.username -}}
{{- $_ := required "medic.gateway.viewer.passwordSecret.name is required when medic.enabled: an existing Secret holding the Viewer password (never put the password in values)" .Values.medic.gateway.viewer.passwordSecret.name -}}
{{- end -}}
