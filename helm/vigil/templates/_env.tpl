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

{{- define "vigil.env" -}}
- name: POSTGRES_HOST
  value: {{ include "vigil.postgres.host" . | quote }}
- name: POSTGRES_PORT
  value: {{ include "vigil.postgres.port" . | toString | quote }}
- name: POSTGRES_DB
  value: {{ include "vigil.postgres.database" . | quote }}
- name: POSTGRES_USER
  value: {{ include "vigil.postgres.username" . | quote }}
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
