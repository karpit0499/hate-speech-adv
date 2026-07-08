# hate-speech-adv: Event-Driven Content Moderation Pipeline on GCP

An event-driven **content-moderation pipeline** built end to end on GCP. A message is POSTed to a public API, queued on Pub/Sub, classified by **Vertex AI (Gemini Flash-Lite)**, embedded for near-duplicate detection, and written to **BigQuery**, then transformed with **dbt**, visualized in **Looker Studio**, and watched by **Cloud Monitoring**. All infrastructure is **Terraform**-provisioned and deployed by **GitHub Actions with keyless Workload Identity Federation**.

> Built as a portfolio project to demonstrate production-style cloud, data, and MLOps engineering, provisioned entirely as code, deployed with no stored credentials, and benchmarked against a labeled evaluation set.

---

## Architecture

```
        POST /classify
             │
             ▼
   ┌──────────────────┐      ┌──────────────┐      ┌────────────────────┐
   │ Ingestion API    │ ───► │  Pub/Sub     │ ───► │  Worker (Cloud Run  │
   │ (Cloud Run)      │      │  topic       │      │  + parallel n8n)    │
   └──────────────────┘      └──────────────┘      │  → Vertex AI Gemini │
                                                    │  → embeddings       │
                                                    └─────────┬──────────┘
                                                              ▼
                                                    ┌────────────────────┐
                                                    │  BigQuery (raw)     │
                                                    └─────────┬──────────┘
                                                              ▼
                                                    ┌────────────────────┐
                                                    │  dbt transforms     │
                                                    └─────────┬──────────┘
                                                              ▼
                                          Looker Studio dashboard + Cloud Monitoring alerts

  Everything provisioned by Terraform · deployed by GitHub Actions (keyless WIF) · secrets in Secret Manager
```

One `POST /classify` fans out to **two independent workers** reading separate subscriptions — a pure-Python Cloud Run service (`worker-sub` → `classifications_raw`) and a hosted n8n workflow (`worker-n8n-sub` → `classifications_n8n`) — so the same stream is processed by both a code pipeline and a visual one, with no duplication.

---

## What it does

- **Classifies** each message into one of three labels — `hate_speech`, `offensive`, or `neither` — with a confidence score, target groups, and a one-sentence rationale, using a structured-output JSON schema so results are always parseable.
- **Decouples** ingestion from processing via Pub/Sub, so traffic spikes never hit the model directly and failed messages can be retried.
- **Detects near-duplicates** by embedding each message (768-dim vectors) and ranking cosine distance in BigQuery — a lightweight brigading / coordinated-abuse signal.
- **Shapes analytics** with a tested dbt layer (staging + daily-summary mart) feeding a Looker Studio dashboard.
- **Monitors itself** with a Cloud Monitoring alert on worker error rate.
- **Benchmarks models** with an evaluation harness that prints accuracy + a confusion matrix per model and logs runs to BigQuery for drift tracking.

---

## Tech stack: and why each service

| Layer | Service | Why it's here |
|-------|---------|---------------|
| Infrastructure as Code | **Terraform** | Reproducible, reviewable, tear-down-in-one-command infrastructure. |
| Ingestion & compute | **Cloud Run** | Scale-to-zero serverless containers; no servers to manage. |
| Messaging | **Pub/Sub** | Decoupled, spike-tolerant ingestion with retry semantics. |
| Classification | **Vertex AI (Gemini `gemini-3.1-flash-lite`)** | Managed Gemini with proper IAM instead of a loose API key. |
| Embeddings | **Vertex AI (`gemini-embedding-001`, 768-dim)** | Semantic fingerprints for near-duplicate detection. |
| Storage & analytics | **BigQuery** | Serverless warehouse; native vector distance functions. |
| Transformation | **dbt** | Version-controlled, tested SQL models — the analytics-engineering layer. |
| Managed database | **Cloud SQL (Postgres 16)** | Persistent store for the hosted n8n workflow twin. |
| Secrets | **Secret Manager** | DB password + n8n encryption key, never in code or state. |
| BI | **Looker Studio** | Live dashboard reading from the dbt mart. |
| Observability | **Cloud Monitoring** | Alerting on worker 5xx error rate. |
| CI/CD | **GitHub Actions + Workload Identity Federation** | Push-to-deploy with **no stored service-account key**. |

---

## Repository layout

```
hate-speech-adv/
├── infra/                     # Terraform — all GCP resources
│   └── main.tf
├── ingest/                    # Ingestion API (Cloud Run)
│   ├── main.py                #   Flask: POST /classify → publish to Pub/Sub
│   ├── requirements.txt
│   └── Dockerfile
├── worker/                    # Classification worker (Cloud Run, private)
│   ├── main.py                #   Pub/Sub push → Vertex AI → embeddings → BigQuery
│   ├── requirements.txt
│   └── Dockerfile
├── hate_speech/               # dbt project
│   └── models/
│       ├── _sources.yml
│       ├── stg_classifications.sql
│       ├── mart_daily_summary.sql
│       └── _schema.yml        #   not_null + accepted_values tests
├── eval/                      # Evaluation harness
│   ├── labeled.csv            #   ~180-row balanced ground-truth set
│   └── run_eval.py            #   accuracy + confusion matrix, logs to BigQuery
└── .github/workflows/
    └── deploy.yml             # Keyless deploy of ingest + worker on push to main
```

---

## Quick start

> **Prerequisites:** `gcloud` CLI, Terraform, a GCP project with billing enabled, and `gcloud auth application-default login` completed.

**1. Provision the infrastructure**

```bash
cd infra
terraform init
terraform plan      # review what will be created
terraform apply     # type 'yes'
```

**2. Deploy the services**

```bash
# Ingestion API (public)
gcloud run deploy ingest --source ./ingest --region europe-west3 \
  --set-env-vars PROJECT=hate-speech-adv --allow-unauthenticated

# Worker (private — only Pub/Sub can invoke it)
gcloud run deploy worker --source ./worker --region europe-west3 \
  --set-env-vars PROJECT=hate-speech-adv \
  --service-account classifier-worker@hate-speech-adv.iam.gserviceaccount.com \
  --no-allow-unauthenticated
```

After the worker is deployed, a second `terraform apply` wires the Pub/Sub push subscription to it (it reads the deployed URL automatically).

**3. Send a message**

```bash
INGEST_URL=$(gcloud run services describe ingest --region europe-west3 --format="value(status.url)")

curl -X POST "$INGEST_URL/classify" \
  -H "Content-Type: application/json" \
  -d '{"text":"People of that religion are all criminals and should be banned."}'
```

Within a few seconds a labeled row lands in `hate-speech-adv.moderation.classifications_raw`:

```sql
SELECT message_id, label, confidence, rationale, created_at
FROM `hate-speech-adv.moderation.classifications_raw`
ORDER BY created_at DESC LIMIT 5;
```

**4. Ship changes** — once CI/CD is set up, a push to `main` redeploys both services automatically with no stored credentials.

---

## Evaluation results

Benchmarked against a hand-verified, balanced 180-row eval set (60 `hate_speech` / 60 `offensive` / 60 `neither`) using the same prompt and client as the deployed worker.

| Model | Accuracy |
|-------|----------|
| `gemini-3.1-flash-lite` | 96% |
| `gemini-2.5-flash` | 100% |
| `ollama:llama3` (previous local setup) | 88% |

Run it yourself:

```bash
python eval/run_eval.py gemini-3.1-flash-lite gemini-2.5-flash
```

The harness prints a per-model confusion matrix and a classification report, then logs each run to `moderation.eval_runs` so accuracy can be tracked over time for drift.

> Replace the placeholders above with your real numbers and drop in a screenshot of the confusion matrix — it's the strongest single piece of evidence in this repo.

---

## Cost

Built on the GCP **$300 free trial** plus always-free tiers, this runs at essentially **€0** for portfolio-scale volume. The only components that cost anything **while idle** are:

- **Cloud SQL** (the n8n twin's Postgres instance), and
- any Cloud Run service kept warm with `--min-instances=1` (the n8n service).

Both are single-digit €/month. The ingestion API, the code worker, and Pub/Sub all scale to zero on their own and cost nothing idle. Run `terraform destroy` — or scale n8n to zero (`gcloud run services update n8n --region europe-west3 --min-instances 0`) — between demos, and rebuild in minutes.

---

## Known manual steps (not yet in Terraform)

For honesty and reproducibility, these grants were made by hand outside Terraform:

- Two IAM roles on the **Compute Engine default service account** (`<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`), which is the identity that runs `--source` builds and the `ingest` service: `roles/run.builder` (source builds) and `roles/pubsub.publisher` (so `ingest` can publish).
- The **Workload Identity Federation** pool, provider, and `github-deployer` service account for CI/CD (Phase 8) are created via `gcloud`, not Terraform.

---

## What I'd do next

- **Harden the worker:** wrap message parsing in `try/except` and ack un-parseable payloads (return 204) so only genuinely transient failures emit 5xx and retry; attach a **dead-letter topic** to `worker-sub` so poison messages get parked instead of looping forever.
- **Dedicated ingest identity:** give `ingest` its own service account with only `roles/pubsub.publisher` instead of leaning on the broad Compute Engine default SA.
- **Human-review UI** for low-confidence classifications.
- **Auto-retraining / prompt-tuning loop** fed by reviewed data.
- **Scheduled hate-rate alerting** via a log-based or scheduled-query metric, beyond the current 5xx alert.

---

## Documentation

- **[Technical documentation](./TECHNICAL_DOCUMENTATION.md)** — architecture, data model, IAM/security model, component reference, and an operations runbook.

---

## License

MIT — see `LICENSE`.
