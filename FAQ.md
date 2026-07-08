# `hate-speech-adv` — FAQ

A living FAQ for this event-driven hate-speech moderation pipeline on Google Cloud. Questions are ordered from **basic** (deploy, integrate) to **advanced** (scaling, feedback loops), so it works as both an operations runbook and an onboarding reference for anyone reading the repo.

**Quick facts**

| Thing | Value |
|-------|-------|
| GCP project ID | `hate-speech-adv` |
| Region | `europe-west3` (Frankfurt) |
| Worker service account | `classifier-worker@hate-speech-adv.iam.gserviceaccount.com` |
| Classifier model | `gemini-3.1-flash-lite` (Vertex AI, `location="global"`) |
| Embedding model | `gemini-embedding-001` (768 dims) |

> Model IDs change frequently — verify the current ID in Google's Vertex AI docs before rebuilding.

---

## Deployment & setup

### 1. How do I deploy the whole thing from scratch?

Two layers, in this order:

1. **Infrastructure (Terraform):** from `infra/`, run `terraform init` then `terraform apply`. This creates the Pub/Sub topic + subscriptions, BigQuery dataset and tables, the `classifier-worker` service account and its IAM roles, the Cloud SQL instance for n8n, and Secret Manager entries. Terraform is the source of truth for everything that isn't application code.
2. **Application code (Cloud Run):** the ingest API and the Python worker are container images deployed to Cloud Run. Once the repo is on GitHub, a push to `main` triggers the GitHub Actions workflow (`deploy.yml`), which authenticates via Workload Identity Federation and runs `gcloud run deploy` for both services. For a manual first deploy, `gcloud run deploy ingest --source ./ingest --region europe-west3` (and the equivalent for the worker) works too.

The mental model: **Terraform provisions the plumbing, GitHub Actions ships the code that runs inside it.**

### 2. Where is everything deployed?

Everything lives in the `hate-speech-adv` GCP project, region `europe-west3`, with one deliberate exception:

- **Cloud Run** hosts three services: the ingest API, the Python worker, and a hosted n8n instance.
- **Pub/Sub** is the message bus: topic `incoming-messages` with two subscriptions — `worker-sub` (Python worker) and `worker-n8n-sub` (n8n worker).
- **BigQuery** dataset `moderation` holds `classifications_raw` (Python), `classifications_n8n` (n8n), and `eval_runs` (drift tracking).
- **Cloud SQL** instance `n8n-db` backs n8n's workflow storage.
- **Vertex AI** serves the Gemini classifier — but at `location="global"`, not `europe-west3` (see Q17).

The **exception**: the Gemini 3.x model itself is called through the `global` endpoint. Your data (BigQuery, Cloud SQL, the queue) still resides in `europe-west3`; only the model inference request routes globally.

### 3. How do I ship a code change to just the worker or the ingest API?

Push to `main`. The GitHub Actions workflow rebuilds and redeploys both Cloud Run services. If you want to deploy one service by hand without a push:

```bash
gcloud run deploy worker --source ./worker --region europe-west3
```

Cloud Run builds the container, rolls out a new revision, and shifts traffic to it automatically. The previous revision stays around, so a bad deploy is one `gcloud run services update-traffic` away from rollback.

### 4. How do I tear it all down and rebuild later to avoid ongoing cost?

The two ongoing cost risks are Cloud SQL (`n8n-db`) and any min-instance Cloud Run service. When the pipeline isn't actively running:

```bash
cd infra
terraform destroy   # type 'yes' — removes the whole stack
```

To rebuild, `terraform apply` recreates the infrastructure in minutes, then push to `main` (or manually deploy) to redeploy the Cloud Run services. Because everything is code, rebuilds are reproducible.

> Caveat: `terraform destroy` wipes the n8n Cloud SQL database, so any workflows built in the *cloud* n8n UI are gone. Export important workflows first, or keep the canonical copy in a local n8n and re-import.

### 5. What does it cost to run, and how do I keep it near zero?

At portfolio-scale volume it runs at essentially **€0** on the free tier + always-free allowances. Gemini Flash-Lite calls, Pub/Sub, and BigQuery at this volume are negligible. The only real drip is **Cloud SQL** and any **min-instance Cloud Run** service (n8n needs `--min-instances=1` and `--no-cpu-throttling` to stay warm) — single-digit €/month if left running.

Keep costs down by: setting a **€5 budget alert** with 50/90/100% notifications, running `terraform destroy` between runs, and not leaving n8n min-instances warm when idle. If you only need the Python path, you can skip deploying n8n entirely.

---

## Integrating with a business & chatbot

### 6. How do I integrate this with an existing business architecture?

Treat the ingest API as a **drop-in moderation endpoint**. Any system that produces user-generated text — a support inbox, a forum, a comment stream, a chat product — makes an HTTP POST to the ingest API's `/classify` endpoint. The pipeline decouples them from the model: they fire a message and move on, and the classification lands in BigQuery independently.

The clean integration boundary is Pub/Sub. Business systems only ever talk to the ingest API (a thin HTTP front door); everything downstream — model, storage, analytics — can change without those systems noticing. If a business system needs the *result* rather than fire-and-forget, see Q9.

### 7. How do I connect it to a chatbot?

The chatbot calls the ingest API on each inbound (or outbound) message before it's shown:

1. User sends a message to the chatbot.
2. The chatbot POSTs `{"text": "<message>"}` to the ingest API's `/classify`.
3. For a **blocking gate** (don't display until judged), use a synchronous variant — see Q9 — so you get a label back before rendering.
4. For **passive logging/monitoring** (analyze in the background, don't block the UX), the fire-and-forget flow is perfect: post and forget, review flagged content later in BigQuery/Looker.

Because n8n is already in the stack, the *fastest* chatbot integration is often an n8n workflow: a webhook node receives the chatbot message, calls the classifier, and branches on the label (allow / flag / block).

### 8. What's the API contract — how does another app call the classifier?

The ingest API exposes `POST /classify`:

```bash
curl -X POST https://<ingest-url>/classify \
  -H "Content-Type: application/json" \
  -d '{"text": "the message to classify"}'
```

It responds `202 Accepted` with `{"status": "queued"}` — it has *accepted* the message for processing, not classified it yet. The classification result appears in BigQuery `moderation.classifications_raw` once the worker processes the queue. The result schema is: `label` (`hate_speech` / `offensive` / `neither`), `confidence` (0–1), `target_groups` (array), `rationale` (one sentence), plus `embedding`, `model_version`, and timestamps.

### 9. Can I get the label back immediately instead of fire-and-forget?

Yes, but it's a different flow. The default architecture is **asynchronous** (202 → queue → worker → BigQuery), which is great for throughput and resilience but doesn't hand the caller a verdict. For a synchronous "tell me the label now" path there are two options:

- **Direct synchronous endpoint:** add a route that calls Vertex AI inline and returns the JSON verdict in the HTTP response, bypassing Pub/Sub. Simplest for a chatbot gate; you trade away the queue's buffering/retry benefits.
- **Async + poll/callback:** keep the queue, but have the worker write results keyed by a `message_id` the caller receives up front, then the caller polls or receives a webhook when the verdict is ready.

For a real-time chatbot moderation gate, the direct synchronous endpoint is usually the right call. Keep the async pipeline for bulk/passive analysis.

### 10. How do I add a new downstream consumer without touching the worker?

Add another **Pub/Sub subscription** to the `incoming-messages` topic. That's the point of the fan-out design — the existing `worker-sub` and `worker-n8n-sub` already prove it: two independent consumers reading the same topic. A new consumer (say, a real-time Slack alerter for `hate_speech`) gets its own subscription and processes messages independently. The producer (ingest API) and existing workers don't change at all.

---

## Operations & monitoring

### 11. How do I know it's working / how do I monitor it?

Three layers:

- **End-to-end smoke test:** POST a known message to the ingest API, then query `classifications_raw` for the row. If the row appears with a sensible label, the whole chain works.
- **Cloud Monitoring:** an alerting policy watches the worker's 5xx errors by service name and emails when they spike. This is the "pipeline is unhealthy" signal.
- **Logs:** Cloud Run per-service logs (`gcloud run services logs read worker --region europe-west3`) show each classification and any Vertex AI errors.

For business-level monitoring (volume, hate-rate, low-confidence queue) the dbt marts feed a Looker Studio dashboard.

### 12. What happens when the worker fails or the model errors out?

Pub/Sub handles delivery guarantees. If the worker returns a non-2xx (e.g., a 5xx because Vertex AI hiccupped), Pub/Sub **retries** the message. One thing worth knowing: a 5xx triggers aggressive retries — roughly **every 0.6 seconds** — so a persistent error floods fast.

Practical consequences:
- Transient model errors self-heal on retry.
- A *persistent* bug means a message is redelivered relentlessly. After deliberately testing an error path, **purge the subscription** so the poison message stops looping: `gcloud pubsub subscriptions seek worker-sub --time=$(date -u +%Y-%m-%dT%H:%M:%SZ)`.
- For production hardening, attach a **dead-letter topic** so messages that fail N times get parked instead of retried forever (see Q29).

### 13. How do I see the classification results / query the data?

Query BigQuery directly:

```sql
SELECT input_text, label, confidence, target_groups, rationale, created_at
FROM `hate-speech-adv.moderation.classifications_raw`
ORDER BY created_at DESC
LIMIT 50;
```

`classifications_n8n` holds the n8n-path results if that worker was run. **Gotcha:** freshly inserted rows sit in BigQuery's **streaming buffer** for ~90 minutes and can't be `DELETE`d during that window — so if a cleanup `DELETE` "does nothing," it's not broken, the rows just aren't durable yet.

### 14. How do I set up or change alerts?

Alerts live in **Cloud Monitoring** (alerting policies). The existing policy fires on worker 5xx count by service name. To add or change one — e.g., alert when the daily hate-rate crosses a threshold — create a policy on the relevant metric (a log-based metric or a scheduled query over the dbt mart) with an email notification channel. Keep the definitions reproducible by managing notification channels and policies in Terraform where practical, so alerts survive a `destroy`/`apply` cycle.

### 15. How do I handle a traffic spike?

The architecture absorbs spikes by design. The ingest API just publishes to Pub/Sub and returns immediately, so a flood of requests fills the **queue** rather than overwhelming the model. The worker drains the queue at its own pace; Cloud Run scales worker instances up under load and back down when it clears. The model is never hit faster than the workers pull. This "queue as shock absorber" property is the headline benefit of event-driven design.

---

## Model & ML

### 16. Which model does it use and why?

Classification uses **`gemini-3.1-flash-lite`** via Vertex AI. Flash-Lite is the cheapest, fastest Gemini tier — ideal for high-volume, low-latency, well-scoped classification where heavy reasoning isn't needed. The prompt is a tight three-label schema (`hate_speech` / `offensive` / `neither`) with temperature `0` and structured JSON output enforced, so every response is deterministic and parseable. Embeddings use **`gemini-embedding-001`** at 768 dimensions for near-duplicate detection.

### 17. Why does the Gemini model need `location="global"` when everything else is in `europe-west3`?

This is a Gemini 3.x routing requirement. Calling `gemini-3.1-flash-lite` against a **regional** Vertex AI endpoint (like `europe-west3`) returns a **404** — the 3.x models are only exposed through the `global` endpoint. So the SDK client is configured with `location="global"` specifically for the model call.

Important distinction: **this is about the inference endpoint, not data residency.** All actual data — BigQuery, Cloud SQL, Pub/Sub — stays in `europe-west3`. Only the model request routes globally.

### 18. How do I change or upgrade the classification model?

Model IDs change often, so first verify the current ID in Google's Vertex AI docs. Then update the model string in the worker's Vertex AI call and redeploy. Two things to re-check on any model change:

- **Location routing:** newer models may (like 3.x) require `location="global"` rather than a regional endpoint.
- **Regression:** run the evaluation harness (Q19) against the new model before trusting it — a "better" model can quietly get worse on your specific boundary cases.

Because the model ID is a single config point in the worker, swapping models is a one-line change plus a redeploy plus an eval run.

### 19. How do I evaluate model quality and detect drift?

The evaluation harness proves the classifier works rather than assuming it. It runs a **180-row balanced eval set** (`eval_set.csv` / `labeled.csv`) — 60 examples each of `neither`, `offensive`, `hate_speech`, including false-positive traps and hard boundary cases — through the model and computes **accuracy + a confusion matrix**. `run_eval.py` logs each run to BigQuery's `eval_runs` table via `log_to_bigquery()`, so metrics are tracked over time and **drift** (the model silently getting worse after a version change or data shift) is visible.

The eval-set design matters more than the harness: the hard `offensive`/`hate_speech` boundary is the real test of classifier quality, and the false-positive traps in `neither` (neutral mentions of protected groups, criticism of *ideas* vs. *people*) are what separate a serious classifier from a keyword filter. Hand-check 15–20 rows before trusting the labels as ground truth.

### 20. How does the embeddings / near-duplicate detection work?

After classification, the worker also embeds the message with `gemini-embedding-001` (768 dims) and stores the vector in the `embedding` column. To flag coordinated or repeated abuse ("brigading"), a new message's vector is compared against recent flagged ones using **BigQuery vector search** (`VECTOR_SEARCH` / `ML.DISTANCE`). Near-identical *meaning* (not just identical text) surfaces as a small distance — so paraphrased spam or copy-paste harassment campaigns get caught even when the exact wording differs. It's semantic dedup, not string matching.

---

## Data & analytics

### 21. How does data flow from raw ingestion to analytics tables?

```
ingest API → Pub/Sub (incoming-messages) → worker → BigQuery classifications_raw
                                                          │
                                                      dbt staging (stg_*)
                                                          │
                                                      dbt marts (mart_daily_summary)
                                                          │
                                                   Looker Studio / alerts
```

Raw model output lands in `classifications_raw` (append-only, one row per message). dbt then transforms raw rows into **clean, typed, tested staging models** and rolls those up into **marts** (e.g., daily counts by label, avg confidence, hate-rate). The dashboard and business alerts read the marts, never the raw table — the classic raw → staging → mart layering of analytics engineering.

### 22. What does dbt do here and how do I run it?

dbt is the transformation + testing layer between raw BigQuery rows and analytics-ready tables. It builds `stg_classifications` (cleaned/typed) → `mart_daily_summary` (aggregates), and enforces **data tests** — `not_null` on key columns, `accepted_values` on `label` (so an unexpected label value fails the build loudly). To run it:

```bash
python3 -m venv .venv && source .venv/bin/activate   # venv first, then activate
dbt debug     # verify the BigQuery connection before anything else
dbt build     # run models + tests together
```

Running `dbt debug` before `dbt build` catches auth/dataset misconfiguration before a wasted run.

### 23. How do I add a new field to the classification schema (e.g., a "severity" score)?

Three coordinated changes:

1. **BigQuery schema:** add the column to the `classifications_raw` table definition in Terraform and `terraform apply` (BigQuery supports adding nullable columns without a rebuild).
2. **Worker:** update the prompt to produce the new field and the insert statement to write it.
3. **dbt:** surface the field in `stg_classifications` and any mart that needs it; add a test if it has constraints.

Do them in that order (schema → producer → transforms) so nothing writes to a column that doesn't exist yet.

---

## Security & IAM

### 24. How is authentication handled — where are the API keys?

There are no long-lived API keys for the model. The worker authenticates to Vertex AI and BigQuery using its **service account** (`classifier-worker@hate-speech-adv.iam.gserviceaccount.com`), which is granted only the roles it needs — `aiplatform.user`, `bigquery.dataEditor`, `pubsub.subscriber`. This is **least-privilege via workload identity**: the code assumes an identity rather than carrying a secret.

### 25. How does GitHub deploy without storing a GCP key?

**Workload Identity Federation (WIF).** Instead of exporting a service-account JSON key into GitHub secrets (a long-lived credential that can leak), GitHub Actions presents its own OIDC token, and GCP is configured to *trust* tokens from the specific repo. The trust condition is bound to the exact repo path — which is why the repo name has to match exactly, or the trust check fails. No key is ever stored; GitHub gets a short-lived token at deploy time.

### 26. How are secrets managed?

Via **Secret Manager**. The n8n encryption key lives in the `n8n-encryption-key` secret, and the Cloud SQL password is stored there too — both referenced by Cloud Run env vars at deploy time rather than hardcoded. Secrets are provisioned through Terraform, so they're versioned config, not values pasted into a console. A quirk worth remembering: n8n's private-key *field* strips real newlines, so paste the `\n`-escaped single-line form of the key, not the multi-line PEM.

### 27. Is the ingest API secured, and how would I lock it down?

In this build the ingest API is deployed `--allow-unauthenticated` so it's trivial to test with `curl` — meaning anyone with the URL can post to it. That's fine for a demo but not for production. To lock it down: require authentication on the Cloud Run service (remove `--allow-unauthenticated` and require an IAM-authenticated caller or an API gateway with keys), add request validation and rate limiting at the edge, and put it behind a load balancer with Cloud Armor if it's internet-facing.

---

## Architecture & scaling

### 28. Why event-driven instead of a simple synchronous API? What are the trade-offs?

**Why event-driven:** decoupling. The ingest API, the model worker, and storage all scale and fail independently. Traffic spikes fill the queue instead of crashing the model (Q15); a worker crash means messages wait and retry rather than getting lost (Q12); and you can add consumers without touching producers (Q10).

**Trade-offs:** the caller doesn't get an immediate answer (202, not a verdict — Q9), there are more moving parts to operate and reason about, and you inherit distributed-systems concerns like retries, ordering, and duplicate delivery. For a *simple* real-time gate, a synchronous call is genuinely simpler and better. The honest assessment: this pipeline is **deliberately over-engineered** to demonstrate the pattern — for passive moderation analytics at scale it's the right shape; for a single blocking check it'd be overkill.

### 29. How would I scale this to real production volume and multi-region?

The core (Pub/Sub + Cloud Run + BigQuery) already scales horizontally with little change. To harden for production, add:

- **Dead-letter topic** on the subscriptions so messages that fail repeatedly get parked for inspection instead of retrying forever (Q12).
- **Explicit worker concurrency and max-instance limits** to control model QPS and cost under load.
- **Batching** classification requests to the model to cut per-request overhead at high volume.
- **Multi-region**: replicate ingest + workers across regions and front them with a global load balancer; BigQuery and the model endpoint handle scale centrally.
- **Backpressure / quota handling** so that hitting a Vertex AI rate limit degrades gracefully (retry with backoff) rather than error-storming.

### 30. How would I add a human-review UI and a feedback / auto-retraining loop?

The roadmap for a full MLOps lifecycle:

1. **Review queue:** surface low-confidence and borderline `offensive`/`hate_speech` rows (a dbt mart already isolates the low-confidence queue) into a lightweight UI where a human confirms or corrects the label.
2. **Feedback capture:** write human verdicts back to a `labels_human` table — this becomes gold-standard data.
3. **Continuous evaluation:** feed those human labels into the eval harness (Q19) so drift is measured against *real* corrections, not just the static 180-row set.
4. **Auto-retraining / prompt-tuning:** when human-vs-model disagreement crosses a threshold, trigger a re-evaluation of models/prompts (or fine-tune, if moving off a hosted model). The `eval_runs` drift table is already the hook for detecting when that's needed.

---

## Project summary, in one sentence

> An event-driven moderation pipeline on GCP — Pub/Sub ingestion, Vertex AI classification with embeddings-based duplicate detection, BigQuery + dbt for the data layer, Looker Studio and Cloud Monitoring for observability — all provisioned with Terraform and deployed via GitHub Actions with keyless auth, benchmarked against a labeled eval set.

This covers infrastructure-as-code, event-driven design, applied GenAI, data engineering, MLOps, and CI/CD.
