{{/*
Shared `env:` and `envFrom:` block for backend/daemon/llm-worker pods.

Usage (inside a container spec):
  envFrom:
    {{- include "vigil.envFrom" . | nindent 12 }}
  env:
    {{- include "vigil.env" . | nindent 12 }}

Both helpers take the root context directly. They pull the generated ConfigMap
and Secret, plus DATABASE_URL and REDIS_URL (which have to be assembled at
render time because they embed service DNS + a secret reference).

NOTE: secret.yaml is only rendered when secrets.existingSecret is empty. When
existingSecret is set, the secretRef below points at the user-supplied Secret.
*/}}
{{- define "vigil.envFrom" -}}
- configMapRef:
    name: {{ include "vigil.configmap.fullname" . }}
- secretRef:
    name: {{ include "vigil.secret.fullname" . }}
{{- end -}}

{{- define "vigil.env" -}}
- name: DATABASE_URL
  value: {{ printf "postgresql://%s:$(POSTGRES_PASSWORD)@%s:%s/%s" (include "vigil.postgres.username" .) (include "vigil.postgres.host" .) (include "vigil.postgres.port" . | toString) (include "vigil.postgres.database" .) | quote }}
{{- if .Values.redis.enabled }}
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
