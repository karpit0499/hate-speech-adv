# Technical Documentation — hate-speech-adv

Architecture, data model, security model, component reference, and operations runbook for the `hate-speech-adv` content-moderation pipeline. This document is written for an engineer who needs to understand, run, extend, or review the system.

---

## Table of contents

1. [System overview](#1-system-overview)
2. [Architecture & data flow](#2-architecture--data-flow)
3. [Infrastructure (Terraform)](#3-infrastructure-terraform)
4. [Data model](#4-data-model)
5. [Component reference](#5-component-reference)
6. [Identity & security model](#6-identity--security-model)
7. [Deployment & CI/CD](#7-deployment--cicd)
8. [Analytics layer (dbt)](#8-analytics-layer-dbt)
9. [Observability](#9-observability)
10. [Evaluation & drift tracking](#10-evaluation--drift-tracking)
11. [Configuration reference](#11-configuration-reference)
12. [Operations runbook](#12-operations-runbook)
13. [Known limitations & hardening backlog](#13-known-limitations--hardening-backlog)

---

## 1. System overview

### Purpose

Classify short text messages for content moderation into one of three labels — `hate_speech`, `offensive`, `neither` — at scale, using a decoupled, event-driven architecture that mirrors how a real moderation pipeline is built in production.

### Design goals

- **Decoupling** — ingestion must not be coupled to model latency. A spike in inbound messages should queue, not crash the classifier.
- **Reproducibility** — every cloud resource is declared as code and can be destroyed and rebuilt in minutes.
- **Least privilege** — the worker runs as a dedicated service account with exactly the roles it needs; CI deploys with no stored key.
- **Observability** — the pipeline reports its own health, and model quality is measured against a labeled set rather than assumed.

### Core project facts

| Item | Value |
|------|-------|
| GCP project | `hate-speech-adv` |
| Compute region | `europe-west3` (Frankfurt) |
| BigQuery location | `EU` (multi-region) |
| Classification model | `gemini-3.1-flash-lite` (Vertex AI, `location="global"`) |
| Embedding model | `gemini-embedding-001` (768-dim) |
| Worker service account | `classifier-worker@hate-speech-adv.iam.gserviceaccount.com` |
| Deployer service account | `github-deployer@hate-speech-adv.iam.gserviceaccount.com` |
| Source repository | `github.com/karpit0499/hate-speech-adv` |

> **Region note:** Gemini 3.x models are served only from the **global** endpoint on Vertex AI. Regional endpoints (e.g. `europe-west3`) return `404 NOT_FOUND`, so the `genai.Client(...)` in both the worker and the eval harness uses `location="global"` even though the rest of the infrastructure lives in `europe-west3`. This split is intentional and load-bearing.

---

## 2. Architecture & data flow

### High-level flow

```
POST /classify
   │
   ▼
Ingestion API (Cloud Run, public)
   │  publishes {"text": ...}
   ▼
Pub/Sub topic: incoming-messages
   │  fans out to two push subscriptions
   ├──────────────────────────────┐
   ▼                              ▼
worker-sub (push)          worker-n8n-sub (push)
   │                              │
   ▼                              ▼
worker (Cloud Run, private)   n8n (Cloud Run, public)
   │  Vertex AI classify          │  Vertex AI classify (HTTP node)
   │  Vertex AI embed             │
   ▼                              ▼
BigQuery: classifications_raw   BigQuery: classifications_n8n
   │
   ▼
dbt: stg_classifications → mart_daily_summary
   │
   ▼
Looker Studio dashboard   +   Cloud Monitoring (worker 5xx alert)
```

### Why two workers

The topic fans out to **two independent subscriptions**, each feeding a different worker that writes to a different table:

| Path | Subscription | Worker | Destination table |
|------|--------------|--------|-------------------|
| Code | `worker-sub` | Python Cloud Run service | `classifications_raw` |
| Visual | `worker-n8n-sub` | Hosted n8n workflow | `classifications_n8n` |

Because they read **separate subscriptions** and write **separate tables**, a single `POST /classify` is processed by both with **zero duplication**. The Python worker is the primary, portfolio-facing path (it reuses the classification code directly and reads cleanly in a repo); the n8n twin demonstrates the same logic in a visual workflow tool and exercises Cloud SQL + Secret Manager.

### Message contract

**Ingest request** (`POST /classify`):

```json
{ "text": "the message to classify" }
```

**Ingest response** (`202 Accepted`):

```json
{ "status": "queued", "message_id": "<pubsub-message-id>" }
```

**Pub/Sub payload** (published by ingest, consumed by workers):

```json
{ "text": "the message to classify" }
```

Pub/Sub wraps this in a **push envelope** — `{"message": {"data": "<base64>", "messageId": "...", ...}}` — which the worker base64-decodes to recover the payload.

**Model output** (structured JSON):

```json
{
  "label": "hate_speech",
  "confidence": 0.95,
  "target_groups": ["religion"],
  "rationale": "Calls for exclusion of a religious group."
}
```

> **Field-name guard.** The model occasionally emits `classification`/`reasoning` instead of `label`/`rationale`. Both the worker and the eval harness normalize this with `result.get("label") or result.get("classification")` (and the same for rationale). Keep this guard whenever you touch the parsing code.

---

## 3. Infrastructure (Terraform)

All infrastructure is declared in `infra/main.tf` and applied with `terraform apply`. The build layers resources in over several phases, but the final managed set is:

### Enabled APIs
`aiplatform`, `pubsub`, `run`, `bigquery`, `secretmanager`, `sqladmin`, `monitoring` (via a single `google_project_service` `for_each`).

### Messaging
- `google_pubsub_topic.messages` → **`incoming-messages`**
- `google_pubsub_subscription.worker` → **`worker-sub`**, converted to a **push** subscription targeting the worker's Cloud Run URL, authenticated with an OIDC token minted as the worker SA (`ack_deadline_seconds = 60`).
- `google_pubsub_subscription.worker_n8n` → **`worker-n8n-sub`**, push to the n8n webhook (no OIDC block, since n8n runs `--allow-unauthenticated`).

### Data
- `google_bigquery_dataset.moderation` → dataset **`moderation`**, location `EU`.
- `google_bigquery_table.classifications` → **`classifications_raw`** (see [data model](#4-data-model)).
- `google_bigquery_table.eval_runs` → **`eval_runs`** (drift-tracking table).

### Identity
- `google_service_account.worker` → **`classifier-worker`**.
- `google_project_iam_member.worker_roles` → binds `roles/aiplatform.user`, `roles/bigquery.dataEditor`, `roles/pubsub.subscriber` to the worker SA.
- `google_cloud_run_v2_service_iam_member.worker_invoker` → lets the worker SA invoke the private worker service (authorizes the push).
- `google_service_account_iam_member.pubsub_token_creator` → lets the Pub/Sub service agent mint OIDC tokens as the worker SA (scoped to that SA only).

### Cloud SQL + Secrets (Phase 4)
- `google_sql_database_instance.n8n` → **`n8n-db`**, Postgres 16, `db-f1-micro`.
- `google_sql_database.n8n` + `google_sql_user.n8n` + `random_password.n8n_db`.
- `google_secret_manager_secret.n8n_db_pw` / `.n8n_enc` (+ versions + `secretAccessor` bindings for the worker SA).
- `google_project_iam_member.n8n_cloudsql_client` → `roles/cloudsql.client` for the worker SA.

### Data sources
- `data.google_project.current` (project number) and `data.google_cloud_run_v2_service.worker` (deployed worker URL) — so the push subscription reads the worker's URL automatically rather than hardcoding it. **The worker service must be deployed before the `apply` that wires the push subscription.**

> **Providers required:** `hashicorp/google ~> 5.0` and `hashicorp/random`. Re-run `terraform init` after adding the `random` provider in Phase 4.

---

## 4. Data model

### `moderation.classifications_raw`

The primary output table, written by the Python worker.

| Column | Type | Notes |
|--------|------|-------|
| `message_id` | STRING | Pub/Sub message ID (falls back to a generated UUID). |
| `input_text` | STRING | The original message text. |
| `label` | STRING | One of `hate_speech`, `offensive`, `neither`. |
| `confidence` | FLOAT | Model confidence, 0.0–1.0. |
| `target_groups` | STRING | JSON-encoded array (column is STRING, so the array is serialized). |
| `rationale` | STRING | One-sentence explanation. |
| `embedding` | STRING | Legacy string column (kept for schema compatibility; usually `NULL`). |
| `embedding_vector` | ARRAY&lt;FLOAT64&gt; | 768-dim embedding, added in Phase 5 for vector search. |
| `model_version` | STRING | The model ID used (e.g. `gemini-3.1-flash-lite`). |
| `created_at` | TIMESTAMP | UTC insert time (ISO 8601). |

> **Two embedding columns coexist.** `embedding` (STRING) is the original placeholder; `embedding_vector` (ARRAY&lt;FLOAT64&gt;) is the real numeric vector used by `ML.DISTANCE`. The worker writes `None` to the first and the vector to the second. Add `embedding_vector` to the Terraform schema list so a later `apply` doesn't try to drop it.

### `moderation.classifications_n8n`

Same shape as `classifications_raw`, written by the n8n twin. Created separately (see `n8n-twin-setup.md`, Part C) so the two workers never contend for the same table.

### `moderation.eval_runs`

One row per model per evaluation run, for drift tracking.

| Column | Type | Notes |
|--------|------|-------|
| `run_at` | TIMESTAMP | When the eval ran (UTC). |
| `model` | STRING | Model ID evaluated. |
| `accuracy` | FLOAT | Overall accuracy on the labeled set. |
| `n` | INTEGER | Number of eval rows. |

### dbt-built tables (in `moderation`)
- `stg_classifications` — cleaned/typed staging view over `classifications_raw` (drops `embedding_vector`; keeps label-relevant columns; filters `label IS NOT NULL`).
- `mart_daily_summary` — counts by label and day, average confidence, hate count, and hate rate.

---

## 5. Component reference

### 5.1 Ingestion API (`ingest/`)

- **Runtime:** Flask + gunicorn on `python:3.12-slim`, deployed to Cloud Run **public** (`--allow-unauthenticated`).
- **Endpoints:** `GET /` (health check → `{"status":"ok"}`), `POST /classify` (validates `text`, publishes to `incoming-messages`, returns `202` with the message ID).
- **Identity:** runs as the Compute Engine default SA, which holds `roles/pubsub.publisher` (granted manually — see [security model](#6-identity--security-model)).
- **Failure modes:** missing `text` → `400` (rejected *before* publish, so the worker never runs — relevant when testing alerts).

### 5.2 Classification worker (`worker/`)

- **Runtime:** Flask + gunicorn on `python:3.12-slim`, deployed to Cloud Run **private** (`--no-allow-unauthenticated`), running as `classifier-worker`.
- **Trigger:** Pub/Sub **push** delivery to `POST /` (a JSON envelope).
- **Processing:**
  1. Decode the base64 payload, extract `text`.
  2. Call Vertex AI (`gemini-3.1-flash-lite`, `location="global"`, `temperature=0`, JSON response) with the shared system prompt.
  3. Apply the field-name guard for `label`/`rationale`.
  4. (Phase 5+) Call `gemini-embedding-001` with `output_dimensionality=768` to produce `embedding_vector`.
  5. Insert one row into `classifications_raw` via `insert_rows_json`.
  6. Return `204` (acks the message) or `500` (Pub/Sub retries).
- **Idempotency / retries:** a `500` tells Pub/Sub the message wasn't processed, so it redelivers. A malformed/poison payload therefore **loops** until purged or dead-lettered — see hardening backlog.

### 5.3 System prompt (shared)

The same three-label prompt is used in the worker, the n8n HTTP node, and the eval harness so all three measure the same behavior. It defines the three labels, rules ("judge the text as written", "prefer hate_speech if a protected group is targeted"), and mandates JSON-only output. Structured output is enforced with a response schema whose **top-level `type` must be `"object"`** (omitting it causes schema rejection) and an `enum` constraint on `label`.

### 5.4 n8n twin (Phase 4)

- **Hosting:** official `n8nio/n8n` image on Cloud Run, `--port 5678`, `--min-instances 1`, `--memory 1Gi`, `--no-cpu-throttling`, backed by Cloud SQL Postgres.
- **Why those flags are mandatory:** n8n runs ~50 startup migrations before serving traffic; the default CPU-only-while-serving throttle starves them (leaving a half-migrated DB), and the default 512 MiB OOMs a Node app that idles above 515 MiB. `1Gi` + un-throttled CPU is the minimum reliable config.
- **Persistence:** workflows live in Postgres; the **n8n encryption key** is pinned via Secret Manager so saved credentials survive container replacement.
- **Workflow:** Webhook → Normalize → Vertex AI (HTTP) → Build row → BigQuery → Respond, driven by a **Google Service Account API** credential (single-line PEM with literal `\n` — the field strips real newlines on paste).
- **Activation:** the workflow must be **Active** for its production webhook to accept Pub/Sub pushes; an inactive workflow silently drops them.

### 5.5 Near-duplicate detection (Phase 5)

Brute-force cosine distance over `embedding_vector` in BigQuery (`ML.DISTANCE(..., 'COSINE')`), guarded by `ARRAY_LENGTH(embedding_vector) = 768` to exclude empty vectors from early test rows (mismatched lengths raise `Array inputs are not equal in length`). The target row compared to itself returns distance `0.0` (a sanity check); paraphrases rank just above it, well below unrelated messages.

---

## 6. Identity & security model

### Service accounts

| SA | Used by | Roles |
|----|---------|-------|
| `classifier-worker` | Worker service, n8n service | `aiplatform.user`, `bigquery.dataEditor`, `pubsub.subscriber`, `cloudsql.client`, `secretmanager.secretAccessor` (on the two n8n secrets), `run.invoker` (on the worker service) |
| Compute Engine default (`<PROJECT_NUMBER>-compute@…`) | `ingest` service, `--source` builds | `run.builder`, `pubsub.publisher` **(granted manually, not in Terraform)** |
| `github-deployer` | GitHub Actions | `run.admin`, `iam.serviceAccountUser`, `cloudbuild.builds.editor`, `artifactregistry.writer`, `storage.admin` |

### Authorization chain for the Pub/Sub push

1. Pub/Sub delivers to the private worker with an **OIDC token** issued as the worker SA.
2. The Pub/Sub service agent is allowed to mint that token via `roles/iam.serviceAccountTokenCreator` **scoped to the worker SA only** (not project-wide).
3. The worker SA has `roles/run.invoker` on the worker service, so the authenticated push is accepted.

This means the worker is never publicly reachable — only Pub/Sub, acting as an explicitly authorized identity, can invoke it.

### Secrets

- Cloud SQL password and the n8n encryption key are stored in **Secret Manager** and mounted into the n8n service as env vars via `--set-secrets`. They never appear in code, Terraform state (values are `random_password` resources), or the repo.
- The n8n Google SA key (`n8n-key.json`) is **gitignored**; once pasted into n8n (stored encrypted), the local copy can be deleted.

### Keyless CI/CD (Workload Identity Federation)

CI authenticates with **no stored key**. A Workload Identity **pool** (`github`) and **OIDC provider** (`github-provider`) trust GitHub's token issuer, restricted by a **mandatory attribute condition** (`assertion.repository == 'karpit0499/hate-speech-adv'`) so only that exact repo can authenticate. `github-deployer` is impersonable by the repo via `roles/iam.workloadIdentityUser`.

> **Repo name is load-bearing.** The trust condition pins the exact `owner/name`. A repo with a different name authenticates but GCP rejects the deploy with `403`.

---

## 7. Deployment & CI/CD

### Manual deploy

Both services deploy from source (Cloud Build compiles the container):

```bash
gcloud run deploy ingest --source ./ingest --region europe-west3 \
  --set-env-vars PROJECT=hate-speech-adv --allow-unauthenticated

gcloud run deploy worker --source ./worker --region europe-west3 \
  --set-env-vars PROJECT=hate-speech-adv \
  --service-account classifier-worker@hate-speech-adv.iam.gserviceaccount.com \
  --no-allow-unauthenticated
```

### Automated deploy (`.github/workflows/deploy.yml`)

On push to `main`: `checkout → google-github-actions/auth@v3` (WIF) → `setup-gcloud@v3` → deploy `ingest` → deploy `worker`. CI is **scoped to deploying the two Cloud Run services only** — it does **not** run `terraform apply` (which needs broad permissions); Terraform stays a manual step.

- `permissions: id-token: write` is required so the job can request the OIDC token.
- `workload_identity_provider` uses the **project number**, not the project ID.
- First-run `403` at the `auth` step is almost always IAM propagation (up to ~5 min) — re-run the job, no code change needed.

---

## 8. Analytics layer (dbt)

- **Adapter:** `dbt-bigquery`, OAuth method (reuses `application-default` credentials — no keys), `location: EU`, dataset `moderation`.
- **Source:** `_sources.yml` declares `moderation.classifications_raw`.
- **Models:**
  - `stg_classifications` — typed, cleaned, `label IS NOT NULL`; deliberately excludes `embedding_vector` (768-dim vectors don't belong in the BI layer).
  - `mart_daily_summary` — `day`, `label`, `message_count`, `avg_confidence`, `hate_count`, `hate_rate`.
- **Tests (`_schema.yml`):** `not_null` on `message_id` and `label`; `accepted_values` on `label` (`hate_speech`, `offensive`, `neither`). `dbt build` runs models and tests together and fails loudly on a bad label.
- **Run from inside the project folder** (`~/dev/hate-speech-adv/hate_speech`, the one containing `dbt_project.yml`).

---

## 9. Observability

### Looker Studio

A report reads from `mart_daily_summary`: volume time series (`day` × `message_count`), label breakdown (pie/bar), a `hate_rate` scorecard, and a low-confidence review-queue table sorted by `avg_confidence` ascending. Shared "Anyone with the link → Viewer" for the README.

### Cloud Monitoring alert — worker 5xx

- **Metric:** Cloud Run Revision → Request Count, filtered to `service_name = worker` and `response_code_class = 5xx`.
- **Condition:** value above `0` over a rolling 1-minute window; email notification channel; severity Warning.
- **Testing it fires:** an empty `text` is rejected by *ingest* (400) before publish, so the worker never runs. To make the **worker** 5xx, publish a payload missing `text` directly to the topic:
  ```bash
  gcloud pubsub topics publish incoming-messages --message='{"foo":"bar"}'
  ```
  The worker throws `KeyError` → `500`, the alert emails within ~1–2 min.
- **⚠️ Stop the retry loop afterward:** a `500` makes Pub/Sub redeliver the poison message roughly every 0.6 s. Purge it once the alert arrives:
  ```bash
  gcloud pubsub subscriptions seek worker-sub --time=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  ```
  (or Console → Pub/Sub → `worker-sub` → Purge messages).

The alert watches the service **by name**, so every CI redeploy creates a new revision and the policy keeps working untouched.

---

## 10. Evaluation & drift tracking

### Harness (`eval/run_eval.py`)

Runs a labeled CSV through one or more models using the **same prompt and `location="global"` client as the deployed worker**, then prints per-model **accuracy**, a **confusion matrix**, and a `classification_report`, plus a summary table. Failures fall back to `neither` so prediction and gold arrays stay aligned.

```bash
python eval/run_eval.py gemini-3.1-flash-lite gemini-2.5-flash
```

### Ground-truth set (`eval/labeled.csv`)

~180 rows, balanced 60/60/60 across the three classes. Design principles: false-positive traps in `neither` (neutral mentions of protected groups; criticism of *ideas* not *people*); heavy coverage of the hard `offensive`/`hate_speech` boundary; templated, non-slur hate examples (abstract group references) so the file is safe in a public repo. Hand-verify 15–20 boundary rows before trusting it as ground truth.

### Drift logging (`eval_runs`)

Each run appends one row per model (`run_at`, `model`, `accuracy`, `n`) via streaming insert (`insert_rows_json`). The drift query groups accuracy by `model` and `DATE(run_at)`; a dropping accuracy for the same model after a version bump is drift caught before production. Streaming-buffer rows can take up to ~1 minute to appear.

---

## 11. Configuration reference

### Environment variables

| Service | Variable | Value / purpose |
|---------|----------|-----------------|
| ingest | `PROJECT` | `hate-speech-adv` (project for the Pub/Sub topic path) |
| worker | `PROJECT` | `hate-speech-adv` (Vertex AI + BigQuery clients) |
| n8n | `DB_TYPE` | `postgresdb` |
| n8n | `DB_POSTGRESDB_DATABASE` / `_USER` | `n8n` / `n8n` |
| n8n | `DB_POSTGRESDB_HOST` | `/cloudsql/<CONNECTION_NAME>` |
| n8n | `DB_POSTGRESDB_PASSWORD` | from Secret Manager `n8n-db-password:latest` |
| n8n | `N8N_ENCRYPTION_KEY` | from Secret Manager `n8n-encryption-key:latest` |
| n8n | `N8N_PORT` / `N8N_PROTOCOL` | `5678` / `https` (port must match `--port`) |
| n8n | `WEBHOOK_URL` | the service's own `run.app` URL (so webhook nodes emit reachable URLs) |

### Model / endpoint constants

| Constant | Value |
|----------|-------|
| Classification model | `gemini-3.1-flash-lite` |
| Vertex AI location | `global` (**required** for Gemini 3.x) |
| Temperature | `0` |
| Response format | `application/json` (structured output, top-level `type: "object"`) |
| Embedding model | `gemini-embedding-001` |
| Embedding dimensionality | `768` |

### Local dev

macOS (Apple Silicon), zsh, Python at `/usr/local/bin/python3`. Use a project venv (`.venv`) to avoid multiple-interpreter dependency issues. dbt, the test script, and the eval harness authenticate via `gcloud auth application-default login` — no key files.

---

## 12. Operations runbook

### Deploy a change
Push to `main` (CI redeploys `ingest` + `worker`). Infra changes: edit `infra/main.tf`, then `terraform plan` / `terraform apply` manually.

### Send a test message
```bash
INGEST_URL=$(gcloud run services describe ingest --region europe-west3 --format="value(status.url)")
curl -X POST "$INGEST_URL/classify" -H "Content-Type: application/json" \
  -d '{"text":"hello world"}'
```
Then query `classifications_raw` ordered by `created_at DESC`.

### Worker isn't writing rows
1. Console → Cloud Run → `worker` → **Logs** (look for `POST 500` + traceback).
2. Console → Pub/Sub → `worker-sub` → Metrics — unacked messages piling up means the push is returning non-2xx.
3. Recent IAM changes take ~5 min to propagate; a first-attempt `403` often self-resolves.

### n8n path isn't firing (only `raw` rows appear)
- Confirm the workflow is **Active** (production webhook only responds when Active).
- If an **⚠ Offline** banner blocks activation, hard-refresh the tab (Cmd+Shift+R) after confirming the container is up (`n8n ready on ::, port 5678` in logs).
- Check `worker-n8n-sub` metrics for piling unacked messages.

### Stop a poison-message retry storm
```bash
gcloud pubsub subscriptions seek worker-sub --time=$(date -u +%Y-%m-%dT%H:%M:%SZ)
```

### Reset a half-migrated n8n database
Delete the service so nothing re-locks the DB, drop and recreate the database, then redeploy **with** `--memory 1Gi --no-cpu-throttling`:
```bash
gcloud run services delete n8n --region europe-west3
gcloud sql databases delete n8n --instance=n8n-db
gcloud sql databases create n8n --instance=n8n-db
```
If the drop reports "database is being accessed by other users", terminate backends in Cloud SQL Studio (`pg_terminate_backend(...)`) and retry.

### Cost teardown (between demos)
```bash
# Full teardown
cd infra && terraform destroy
# Or just scale the only idle-cost service to zero
gcloud run services update n8n --region europe-west3 --min-instances 0
```
The ingest API, code worker, and Pub/Sub scale to zero on their own.

### Rotate the n8n SA key
Minting a fresh key doesn't invalidate existing ones:
```bash
gcloud iam service-accounts keys create n8n-key.json \
  --iam-account=classifier-worker@hate-speech-adv.iam.gserviceaccount.com
```
Re-paste the single-line PEM (literal `\n`) into the n8n credential.

---

## 13. Known limitations & hardening backlog

- **Poison messages loop forever.** A malformed payload returns `500` and Pub/Sub redelivers it indefinitely, which also spams the 5xx alert. **Fix:** wrap the worker's parse in `try/except` and ack (`204`) un-parseable payloads so only genuinely transient failures retry; attach a **dead-letter topic** to `worker-sub` (e.g. max 5 delivery attempts). This becomes important before leaving the pipeline running unattended.
- **Ingest over-privileged.** `ingest` runs as the broad Compute Engine default SA. **Fix:** a dedicated `ingest` service account with only `roles/pubsub.publisher`.
- **Manual IAM outside Terraform.** The two Compute-default grants (`run.builder`, `pubsub.publisher`) and the entire WIF setup are `gcloud`-created, not codified. **Fix:** move them into Terraform for full reproducibility.
- **No human-review UI** for low-confidence rows (the mart surfaces them, but review is manual).
- **Hate-rate alerting is not built** — only worker 5xx is monitored. A scheduled-query or log-based metric would enable a true content-spike alert.
- **Single-region assumptions.** Compute is `europe-west3`, model calls are `global`; no multi-region failover.
- **Labels are model-defined, not calibrated.** Confidence is the model's self-report, not a calibrated probability.