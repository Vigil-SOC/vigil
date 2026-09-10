{{/*
Shared `env:` and `envFrom:` block for backend/daemon/llm-worker pods.

Usage (inside a container spec):
  envFrom:
    {{- include "vigil.envFrom" . | nindent 12 }}
  env:
    {{- include "vigil.env" . | nindent 12 }}

Both helpers take the root context directly. They pull the generated ConfigMap
and Secret, plus the discrete POSTGRES_* connection parts and REDIS_URL (which
have to be assembled at render time because they embed service DNS + a secret
reference). The app builds and URL-encodes the DSN from POSTGRES_* itself, so
passwords with special characters survive intact (no pre-built DATABASE_URL).

NOTE: secret.yaml is only rendered when secrets.existingSecret is empty AND
secrets.externalSecret.enabled is false. Either way the secretRef below points
at a Secret of the same name (user-supplied, ESO-materialized, or
chart-templated).
*/}}
{{- define "vigil.envFrom" -}}
- configMapRef:
    name: {{ include "vigil.configmap.fullname" . }}
- secretRef:
    name: {{ include "vigil.secret.fullname" . }}
{{- end -}}

{{/*
State Directory. VIGIL_DIR is deliberately NOT in vigil.env: it must only be set
where the volume is actually mounted, or a workload advertises a path it cannot
write. Include all three together, or none.
*/}}
{{- define "vigil.stateEnv" -}}
- name: VIGIL_DIR
  value: {{ .Values.stateDirectory.mountPath | quote }}
{{- end -}}

{{- define "vigil.stateVolumeMount" -}}
- name: vigil-state
  mountPath: {{ .Values.stateDirectory.mountPath }}
{{- end -}}

{{- define "vigil.stateVolume" -}}
- name: vigil-state
  {{- toYaml .Values.stateDirectory.volume | nindent 2 }}
{{- end -}}

{{/*
Operator-supplied CA PEM for TLS-inspecting proxies. Include all three together
(or none) on backend, daemon, llm-worker, agent-worker, and agent-serve only —
the same rule as vigil.state*: env without the mount advertises a path the
container cannot read. Empty existingSecret and existingConfigMap skip every
helper. Prefer the Secret when both are set.
*/}}
{{- define "vigil.caBundleEnv" -}}
{{- if or .Values.caBundle.existingSecret .Values.caBundle.existingConfigMap }}
- name: SSL_CERT_FILE
  value: {{ .Values.caBundle.mountPath | quote }}
- name: REQUESTS_CA_BUNDLE
  value: {{ .Values.caBundle.mountPath | quote }}
- name: NODE_EXTRA_CA_CERTS
  value: {{ .Values.caBundle.mountPath | quote }}
{{- end }}
{{- end -}}

{{- define "vigil.caBundleVolumeMount" -}}
{{- if or .Values.caBundle.existingSecret .Values.caBundle.existingConfigMap }}
- name: vigil-ca-bundle
  mountPath: {{ .Values.caBundle.mountPath }}
  subPath: ca-bundle.pem
  readOnly: true
{{- end }}
{{- end -}}

{{- define "vigil.caBundleVolume" -}}
{{- if .Values.caBundle.existingSecret }}
- name: vigil-ca-bundle
  secret:
    secretName: {{ .Values.caBundle.existingSecret | quote }}
    items:
      - key: {{ .Values.caBundle.key | default "ca.crt" | quote }}
        path: ca-bundle.pem
{{- else if .Values.caBundle.existingConfigMap }}
- name: vigil-ca-bundle
  configMap:
    name: {{ .Values.caBundle.existingConfigMap | quote }}
    items:
      - key: {{ .Values.caBundle.key | default "ca.crt" | quote }}
        path: ca-bundle.pem
{{- end }}
{{- end -}}

{{- define "vigil.env" -}}
- name: HOME
  value: "/home/vigil"
- name: POSTGRES_HOST
  value: {{ include "vigil.postgres.host" . | quote }}
- name: POSTGRES_PORT
  value: {{ include "vigil.postgres.port" . | toString | quote }}
- name: POSTGRES_DB
  value: {{ include "vigil.postgres.database" . | quote }}
{{/* Runtime login; schema owner stays vigil.postgres.username (StatefulSet, db-init). */}}
- name: POSTGRES_USER
  value: "vigil_app"
{{- if .Values.redis.bitnami.enabled }}
{{- if .Values.redis.bitnami.auth.enabled }}
- name: REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "vigil.redis.bitnami.passwordSecret" . }}
      key: {{ include "vigil.redis.bitnami.passwordSecretKey" . }}
{{- end }}
- name: REDIS_URL
  value: {{ include "vigil.redis.url" . | quote }}
{{- else if .Values.redis.enabled }}
- name: REDIS_URL
  value: {{ include "vigil.redis.url" . | quote }}
{{- else if .Values.redis.external.url }}
- name: REDIS_URL
  value: {{ .Values.redis.external.url | quote }}
{{- else if .Values.redis.external.existingSecret }}
- name: REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.redis.external.existingSecret }}
      key: {{ .Values.redis.external.existingSecretKey | default "REDIS_URL" }}
{{- end }}
{{- end -}}

{{/*
Discrete Redis parts for the agent pods, for the reason vigil.env ships discrete
POSTGRES_*: the kubelet substitutes $(REDIS_PASSWORD) into a URL unencoded, so one
holding @ / : or # misparses. Only the agent reads these, and only when REDIS_HOST
is set (services/agent/core/db.ts::redisConfig). Nothing is emitted for an external
Redis given as a URL — there are no parts to name.
*/}}
{{- define "vigil.agentRedisEnv" -}}
{{- if .Values.redis.bitnami.enabled }}
- name: REDIS_HOST
  value: {{ .Values.redis.bitnami.fullnameOverride | default (printf "%s-redis-master" .Release.Name) | quote }}
- name: REDIS_PORT
  value: "6379"
- name: REDIS_DB
  value: {{ include "vigil.redis.database" . | quote }}
{{- else if .Values.redis.enabled }}
- name: REDIS_HOST
  value: {{ include "vigil.redis.fullname" . | quote }}
- name: REDIS_PORT
  value: {{ .Values.redis.service.port | default 6379 | toString | quote }}
- name: REDIS_DB
  value: {{ include "vigil.redis.database" . | quote }}
{{- end }}
{{- end -}}
