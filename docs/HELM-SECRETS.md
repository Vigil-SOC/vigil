# Vigil Helm chart — secret management patterns

The chart supports four ways to provide secrets to Vigil pods. Pick whichever
fits your existing secret-management setup; all four produce the same
Kubernetes Secret shape that backend, daemon, llm-worker, and the db-init
Job reference.

| Pattern | Secret lives | When to pick | Docs |
|---|---|---|---|
| **Plain values** | `values.yaml` (or `--set`) | Local dev, one-off demos | [HELM.md — Secrets](./HELM.md#secrets) |
| **Pre-created Secret** (`secrets.existingSecret`) | Kubernetes | You already manage secrets out-of-band | [HELM.md — Secrets](./HELM.md#secrets) |
| **ExternalSecrets Operator** (`secrets.externalSecret.enabled`) | AWS SM / Vault / GCP SM | Secrets already in an external store | [HELM.md — Secrets](./HELM.md#secrets) |
| **SealedSecrets / SOPS** | Git, encrypted | GitOps without an external secret store | **This doc** |

All patterns require the same **set of keys** in the final Kubernetes Secret.
At minimum:

- `ANTHROPIC_API_KEY` — Claude API key
- `POSTGRES_PASSWORD` — used by the DB-init Job and all app pods
- `JWT_SECRET_KEY` — required when `config.DEV_MODE=false`

Plus whichever integrations you use: `SPLUNK_PASSWORD`, `SLACK_BOT_TOKEN`,
`VIRUSTOTAL_API_KEY`, etc. See [env.example](../env.example) for the full list.

---

## Pattern 1 — Bitnami SealedSecrets

[SealedSecrets](https://github.com/bitnami-labs/sealed-secrets) lets you commit
encrypted Secret manifests to git. The controller in the cluster decrypts them
at apply time, producing a regular `Secret` resource.

### Prerequisites

```bash
# Controller (once per cluster)
helm install sealed-secrets \
  --namespace kube-system \
  --repo https://bitnami-labs.github.io/sealed-secrets sealed-secrets

# CLI (once per dev machine)
brew install kubeseal   # or download from github.com/bitnami-labs/sealed-secrets/releases
```

### Workflow

1. Write a plain Secret manifest with all the keys you need:

   ```yaml
   # secret.yaml
   apiVersion: v1
   kind: Secret
   metadata:
     name: vigil-secrets
     namespace: vigil
   type: Opaque
   stringData:
     ANTHROPIC_API_KEY: sk-ant-...
     POSTGRES_PASSWORD: "a-strong-password"
     JWT_SECRET_KEY: "generated-with-secrets.token_urlsafe-64"
     SLACK_BOT_TOKEN: xoxb-...
   ```

   **Do not commit this file.**

2. Seal it:

   ```bash
   kubeseal -o yaml < secret.yaml > vigil-sealed-secret.yaml
   ```

   The output is safe to commit — only the controller's private key can
   decrypt it.

3. Apply the sealed manifest and install the chart:

   ```bash
   kubectl apply -f vigil-sealed-secret.yaml
   # The controller creates the vigil-secrets Secret automatically.

   helm install vigil ./helm/vigil \
     -n vigil --create-namespace \
     --set secrets.existingSecret=vigil-secrets
   ```

4. To rotate: re-run step 1 with new values, re-seal, re-apply. The chart
   does not need to be upgraded — the app pods pick up the new Secret on the
   next restart (the chart annotates backend/daemon pods with a Secret
   checksum so they restart automatically on `helm upgrade`; for out-of-band
   Secret changes, trigger a rollout manually: `kubectl rollout restart -n vigil deploy`).

See [docs/examples/sealed-secret.yaml](./examples/sealed-secret.yaml) for a
reference template.

---

## Pattern 2 — Mozilla SOPS

[SOPS](https://github.com/getsops/sops) encrypts file contents with a KMS,
age, or PGP key. Unlike SealedSecrets, there's no in-cluster component —
you decrypt at deploy time and pipe the plaintext into `kubectl apply`.

### Prerequisites

```bash
brew install sops age

# Generate an age key (one per team or per environment)
age-keygen -o ~/.sops/vigil.txt
# Commit the PUBLIC half to your repo in .sops.yaml
```

Create `.sops.yaml` at the repo root:

```yaml
creation_rules:
  - path_regex: secrets/.*\.yaml$
    age: >-
      age1your-public-key-here
```

### Workflow

1. Write the plain Secret manifest:

   ```yaml
   # secrets/vigil-secrets.yaml
   apiVersion: v1
   kind: Secret
   metadata:
     name: vigil-secrets
     namespace: vigil
   type: Opaque
   stringData:
     ANTHROPIC_API_KEY: sk-ant-...
     POSTGRES_PASSWORD: "a-strong-password"
     JWT_SECRET_KEY: "..."
   ```

2. Encrypt in place:

   ```bash
   sops -e -i secrets/vigil-secrets.yaml
   ```

   Commit the encrypted file.

3. Deploy:

   ```bash
   # Decrypt and apply in one pipe
   export SOPS_AGE_KEY_FILE=~/.sops/vigil.txt
   sops -d secrets/vigil-secrets.yaml | kubectl apply -f -

   helm install vigil ./helm/vigil \
     -n vigil --create-namespace \
     --set secrets.existingSecret=vigil-secrets
   ```

4. Rotation: edit the encrypted file directly with `sops secrets/vigil-secrets.yaml`
   (opens in your `$EDITOR` with decrypted plaintext; saves back encrypted).

See [docs/examples/sops-config.yaml](./examples/sops-config.yaml) for a
reference `.sops.yaml`.

### SOPS + Helm (alternative: helm-secrets plugin)

If you want `helm install` to handle decryption itself, install the
[helm-secrets](https://github.com/jkroepke/helm-secrets) plugin and commit a
sops-encrypted values file:

```bash
helm plugin install https://github.com/jkroepke/helm-secrets

# Encrypted values file
sops -e -i helm/vigil/values-prod-secrets.yaml

# Install
helm secrets install vigil ./helm/vigil \
  -n vigil --create-namespace \
  -f helm/vigil/values-prod-secrets.yaml
```

This lets you use `secrets.anthropicApiKey: ...` directly (not `existingSecret`)
because the values file itself is encrypted at rest.

---

## Picking between them

- **ExternalSecrets Operator** (pattern 3 in the main doc) is best when
  you already have AWS Secrets Manager, Vault, or GCP Secret Manager as the
  source of truth. Secrets rotate in the external store; the cluster pulls
  fresh values on the refresh interval.

- **SealedSecrets** (pattern 1 here) is best when secrets must live in git
  (GitOps) and you want zero external dependencies beyond the cluster itself.

- **SOPS** (pattern 2 here) is best when you already standardize on
  encrypting config files and don't want a cluster-side decryption controller.

All three work cleanly with Vigil's `secrets.existingSecret`. Never mix —
pick one and stick with it for the full Vigil install.
