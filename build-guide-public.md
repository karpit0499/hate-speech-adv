# Phases 0–10 — Step-by-Step Build Guide

A production-style, event-driven **content-moderation pipeline on Google Cloud** (`hate-speech-adv`), built end to end. You POST a message to a public API; it lands on Pub/Sub; a worker classifies it with Vertex AI (Gemini Flash-Lite), embeds it, and writes a labeled row to BigQuery; dbt shapes the analytics layer; Looker Studio and Cloud Monitoring watch it; everything is provisioned by Terraform and shipped by keyless GitHub Actions.

This is a detailed, every-command walkthrough — each step is spelled out, and the gotchas that commonly trip people up are written down right where you'll hit them.

> **Before you start — substitute your own values.** This guide uses concrete names so the commands are copy-pasteable, but a few are specific to an account and must be swapped for yours:
> - **`hate-speech-adv`** — the example GCP project ID (and repo name). **GCP project IDs are globally unique**, so this exact ID is taken — pick your own and use it everywhere the guide says `hate-speech-adv` (including inside SQL table paths like `` `hate-speech-adv.moderation...` ``). The *repo* name only needs to be unique within your GitHub account, so you can keep it.
> - **`YOUR_PROJECT_NUMBER`** — your numeric GCP project number (`gcloud projects describe <PROJECT_ID> --format="value(projectNumber)"`).
> - **`YOUR_GITHUB_USERNAME`** — your GitHub account/owner, used in the repo path and the CI/CD trust condition.
>
> Region (`europe-west3`), dataset (`moderation`), and resource names are safe to keep as-is or rename to taste. Shell commands assume **macOS with zsh** (Homebrew installs, `~/.zshrc`, Keychain notes) — adapt paths and package managers if you're on Linux or WSL.

> **How to use this guide.** Don't do it all in one sitting. **Phases 0–4 are the Core Path** — finish those and you already have a real, cloud-hosted, event-driven classifier. **Phases 5–9 are Stretch**; add them one at a time depending on the jobs you're targeting (data roles → 6+7; ML/AI → 5+9; platform/DevOps → 8). Every phase ends with a ✅ checkpoint, so you always stop with something working. **Phase 10** turns the whole thing into a portfolio artifact.

## What you'll build

```
        POST /classify
             │
             ▼
   ┌──────────────────┐      ┌──────────────┐      ┌────────────────────┐
   │ Ingestion API    │ ───► │  Pub/Sub     │ ───► │  Worker (Cloud Run  │
   │ (Cloud Run)      │      │  topic       │      │  + n8n twin)        │
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

## What you'll learn (and why it matters)

| Skill | Where you learn it | Job relevance |
|-------|--------------------|---------------|
| Infrastructure as Code (Terraform) | Phase 1 | Very high — DevOps/platform standard |
| Vertex AI + IAM/service accounts | Phase 2 | High — proper GCP ML |
| Event-driven architecture (Pub/Sub) | Phase 3 | High — data engineering |
| Serverless & containers (Cloud Run, Docker) | Phase 3–4 | High |
| Managed databases (Cloud SQL) + Secret Manager | Phase 4 | Medium–high |
| Embeddings & vector search | Phase 5 | High — GenAI |
| Analytics engineering (dbt + BigQuery) | Phase 6 | High — data roles |
| BI & observability (Looker Studio, Cloud Monitoring) | Phase 7 | Medium–high |
| CI/CD (GitHub Actions, keyless auth) | Phase 8 | High |
| Model evaluation & drift monitoring | Phase 9 | High — MLOps |

> **Model note:** classification uses a **Flash-Lite** tier model — this guide uses **`gemini-3.1-flash-lite`**, called with **`location="global"`** on Vertex AI (regional endpoints 404 for Gemini 3.x). Embeddings use **`gemini-embedding-001`**. Model IDs and the `-preview`/stable suffix churn often — verify the current ID in Google's docs before you build, and swap it in everywhere it appears.

## Where Phases 0–2 leave you (quick reference)

Everything from Phase 3 onward assumes this state — these are the exact names the rest of the guide uses (verify anytime with `gcloud pubsub topics list` / `gcloud pubsub subscriptions list`):

- Project: **`hate-speech-adv`**, region **`europe-west3`** (BigQuery dataset in multi-region **`EU`**)
- Pub/Sub topic **`incoming-messages`**, subscription **`worker-sub`**
- BigQuery: dataset **`moderation`**, table **`classifications_raw`**
- Service account: **`classifier-worker@hate-speech-adv.iam.gserviceaccount.com`**
- Model: **`gemini-3.1-flash-lite`** called with **`location="global"`** via the **`google-genai`** SDK
- Terraform lives in `~/dev/hate-speech-adv/infra/`

> **Local Python tip:** For any local scripts in these phases (dbt, test scripts, the eval harness), make a virtual environment instead of fighting the multiple-Python-installs problem. (This guide uses `/usr/local/bin/python3` — the python.org installer's path on macOS. If yours lives elsewhere, `which python3` tells you what to use instead; once the venv is active it doesn't matter.)
> ```bash
> cd ~/dev/hate-speech-adv
> /usr/local/bin/python3 -m venv .venv
> source .venv/bin/activate
> ```
> Everything you `pip install` while that's active stays isolated and uses the right interpreter.

---

# CORE PATH

# Phase 0 — Foundations & local tools (~1 hr)

**Goal:** Install the four local tools you'll drive the whole build from, create the GCP project, and — critically — make sure billing is *actually* on and a budget alert is guarding you before you provision anything. You do a lot from the command line in this project; getting these four installed once means every later phase "just works."

> **Term — "project":** in GCP a *project* is the container that holds every resource and the billing link. Everything you create in this guide lives inside `hate-speech-adv`.

## Step 0.1 — Install the four tools

1. **Google Cloud CLI (`gcloud`)** — your remote control for GCP. Install from `cloud.google.com/sdk`.
2. **Terraform** — describes cloud infrastructure as code. On a Mac the cleanest install is Homebrew:
   ```bash
   brew tap hashicorp/tap
   brew install hashicorp/tap/terraform
   terraform -version      # any recent 1.x is fine
   ```
3. **Docker** — only needed if you run the n8n twin locally in Phase 4; Cloud Run builds happen server-side.
4. **git** + a **GitHub account** — you'll need it for Phase 8's CI/CD.

> **The `gcloud: command not found` gotcha.** After installing, a new terminal sometimes can't find `gcloud` because the SDK's `bin/` isn't on the `PATH`. The fix: keep the SDK at a stable path (e.g. **`~/google-cloud-sdk`**) and add its init lines to your shell rc file so *every* new shell picks it up. For zsh (the macOS default), append to **`~/.zshrc`**:
> ```bash
> echo 'source "$HOME/google-cloud-sdk/path.zsh.inc"' >> ~/.zshrc
> echo 'source "$HOME/google-cloud-sdk/completion.zsh.inc"' >> ~/.zshrc
> source ~/.zshrc          # reload the current shell without reopening it
> gcloud --version         # should now print versions instead of "command not found"
> ```

## Step 0.2 — Authenticate

Two logins — the first is *you* using `gcloud`; the second lets local tools (Terraform, the Python SDKs, dbt) borrow your credentials so you never paste a key file:

```bash
gcloud auth login                        # opens a browser; sign in as yourself
gcloud auth application-default login    # the credentials Terraform/SDKs/dbt reuse locally
```

> **Why both?** `gcloud auth login` authorizes the CLI. `application-default login` writes an *Application Default Credentials* file that any Google client library on your machine reads automatically — it's the reason the Phase 2 test script, dbt (Phase 6), and the eval harness (Phase 9) all work with **no key files**. Keyless local dev is the same instinct you'll formalize with Workload Identity Federation in Phase 8.

## Step 0.3 — Create the project and link billing

```bash
gcloud projects create hate-speech-adv --name="Hate Speech Advanced"
gcloud config set project hate-speech-adv
```

> **Reminder:** project IDs are globally unique across all of GCP — `gcloud projects create` will fail if the ID is taken. Use the ID you picked (per the note at the top of this guide) here and in every later command.

Now link billing. Get your billing account ID and link it:

```bash
gcloud billing accounts list        # look for OPEN: True, copy that ACCOUNT_ID
gcloud billing projects link hate-speech-adv --billing-account=XXXXXX-XXXXXX-XXXXXX
```

> **The closed-account trap (silent, and easy to hit).** `link` returns *no error* even when you point it at a **closed** billing account: nothing errors, but nothing will ever provision either. That's why `gcloud billing accounts list` above tells you to copy an account showing **`OPEN: True`**. Phase 1, Step 1.1 re-verifies this with `gcloud billing projects describe hate-speech-adv` (you want `billingEnabled: true`) precisely because of this gotcha — if you're unsure now, jump ahead and run that check.

## Step 0.4 — Set a budget alert (your safety net)

Do this *before* you build anything. It won't stop spend, but it emails you the moment you drift toward a limit — and on the free trial you'll basically never see it fire.

1. Console → **Billing → Budgets & alerts → Create budget** (pin the project in the URL if the picker defaults elsewhere: `...console.cloud.google.com/billing?project=hate-speech-adv`).
2. Scope it to `hate-speech-adv`, set a small amount (a **€5** budget is plenty for portfolio scale), and add **50 / 90 / 100 %** email thresholds.
3. Save.

## Step 0.5 — Confirm the project is selected

```bash
gcloud config get-value project      # must print: hate-speech-adv
```

If it prints anything else, `gcloud config set project hate-speech-adv` again — running later commands against the wrong project is the single most common "why didn't my resource appear?" cause in this whole build.

✅ **Checkpoint 0:** `gcloud config get-value project` prints `hate-speech-adv`, billing shows `billingEnabled: true`, and a budget alert exists. You're ready to provision.

---

# Phase 1 — Provision everything with Terraform (~2 hrs)

**Goal:** Instead of clicking around the console, declare all your infrastructure in a text file and let Terraform create it in one command. This is one of the most valuable habits in the whole guide — and "I provision my cloud infra as code" is a strong interview line.

> **Term — Infrastructure as Code (IaC):** defining your servers, databases, and permissions in code files (versioned in git) rather than by hand. Reproducible, reviewable, and deletable in one command (`terraform destroy`).

## Step 1.1 — Create the `infra/` folder (and confirm billing is actually on)

Put `infra/` at the **root of a new git repo for this project** — Phase 10 wants everything in one repo, so this is the natural home:

```bash
mkdir -p ~/dev/hate-speech-adv/infra
cd ~/dev/hate-speech-adv/infra
```

**Before you apply, confirm billing is truly enabled** — this is the gotcha that bites people. It's possible to "successfully" link a *closed* billing account: the link command returns no error, but nothing will actually provision. Check:

```bash
gcloud billing projects describe hate-speech-adv
```

You want `billingEnabled: true`. If it says `false`, you're linked to a closed account. Find your open one and relink:

```bash
gcloud billing accounts list          # look for OPEN: True, copy its ACCOUNT_ID
gcloud billing projects link hate-speech-adv --billing-account=YOUR-OPEN-ACCOUNT-ID
gcloud billing projects describe hate-speech-adv   # verify billingEnabled: true now
```

Relinking overwrites the previous association — there's no separate unlink step.

## Step 1.2 — Write `main.tf`

Create `~/dev/hate-speech-adv/infra/main.tf`. This is the foundational version; Phase 2 adds IAM roles to it and Phase 3 adds the push-subscription wiring.

```hcl
terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.0" }
  }
}

provider "google" {
  project = "hate-speech-adv"
  region  = "europe-west3"   # Frankfurt
}

# Turn on the APIs we need
resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform.googleapis.com",     # Vertex AI
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "bigquery.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
    "monitoring.googleapis.com",
  ])
  service = each.value
}

# Pub/Sub topic + subscription (the message queue)
resource "google_pubsub_topic" "messages" { name = "incoming-messages" }
resource "google_pubsub_subscription" "worker" {
  name  = "worker-sub"
  topic = google_pubsub_topic.messages.name
}

# BigQuery dataset + raw table
resource "google_bigquery_dataset" "moderation" {
  dataset_id = "moderation"
  location   = "EU"
}
resource "google_bigquery_table" "classifications" {
  dataset_id          = google_bigquery_dataset.moderation.dataset_id
  table_id            = "classifications_raw"
  deletion_protection = false
  schema = jsonencode([
    { name = "message_id",    type = "STRING" },
    { name = "input_text",    type = "STRING" },
    { name = "label",         type = "STRING" },
    { name = "confidence",    type = "FLOAT" },
    { name = "target_groups", type = "STRING" },
    { name = "rationale",     type = "STRING" },
    { name = "embedding",     type = "STRING" },
    { name = "model_version", type = "STRING" },
    { name = "created_at",    type = "TIMESTAMP" },
  ])
}

# A service account for the worker to use (least-privilege identity)
resource "google_service_account" "worker" {
  account_id   = "classifier-worker"
  display_name = "Classifier Worker"
}
```

> **Note on names:** the Pub/Sub *topic* is `incoming-messages` and the *subscription* is `worker-sub` — those are the real deployed names the rest of this guide uses. (Terraform's internal resource labels `"messages"` and `"worker"` are just how the HCL refers to them; the actual GCP names are the `name = ` values.)

## Step 1.3 — Apply it

```bash
cd ~/dev/hate-speech-adv/infra
terraform init      # downloads the Google provider
terraform plan      # shows what it WILL create — read this
terraform apply     # type 'yes' to create it all
```

> **Tip:** `terraform destroy` tears the whole thing down when you're done experimenting — great for keeping costs at zero.

## Step 1.4 — Verify

Fastest check — ask Terraform what it's managing:

```bash
terraform state list
```

You should see the topic, subscription, dataset, table, service account, and the API services.

**Prefer the console?** Here's where each lives — but **check the project picker at the top first**: it defaults to whatever project you last viewed (often "My First Project"), and looking at the wrong project is the #1 reason resources "don't appear." Switch it to **hate-speech-adv**, or use a direct URL with `?project=hate-speech-adv` pinned on the end.

- **Pub/Sub:** nav menu → **Pub/Sub** → **Topics** → you should see `incoming-messages`; click it to see `worker-sub`.
- **BigQuery:** nav menu → **BigQuery** → Explorer → expand project → `moderation` → `classifications_raw`.
- **Service account:** **IAM & Admin** → **Service Accounts** → `classifier-worker`.

✅ **Checkpoint 1:** `terraform state list` (or the console) shows the Pub/Sub topic, subscription, BigQuery dataset + table, and the service account — all created from code.

---

# Phase 2 — Classification via Vertex AI (~1.5 hrs)

**Goal:** Call Gemini through **Vertex AI** (the enterprise path with proper IAM), not a loose API key, and confirm it returns clean, parseable JSON for a test message.

> **Term — service account:** a non-human identity your code uses to authenticate. You grant it only the permissions it needs (least privilege) — the opposite of pasting an all-powerful API key into your code.

## Step 2.1 — Grant the worker its Vertex AI + BigQuery permissions

Add this block to `main.tf` (below the service-account resource) and re-apply. It gives the worker exactly three roles: call Vertex AI, write to BigQuery, and read the Pub/Sub subscription.

```hcl
resource "google_project_iam_member" "worker_roles" {
  for_each = toset([
    "roles/aiplatform.user",
    "roles/bigquery.dataEditor",
    "roles/pubsub.subscriber",
  ])
  project = "hate-speech-adv"
  role    = each.value
  member  = "serviceAccount:${google_service_account.worker.email}"
}
```

```bash
cd ~/dev/hate-speech-adv/infra
terraform apply    # 'yes' — should show 3 IAM members to add
```

## Step 2.2 — The classification prompt + structured output

You can try the prompt interactively in **Vertex AI Studio** first if you like. Heads-up: the console was redesigned — it's now under **"Agent Platform / Studio"** and the old "Create prompt" button is gone. Use the **`+ New`** button (or the **Prompt Gallery**) to open a fresh prompt.

Paste this as the **system instruction**:

```
You are a content-moderation classifier. Classify the USER TEXT into exactly one label.

LABELS:
- "hate_speech": attacks or dehumanizes people based on a protected attribute
  (race, ethnicity, religion, national origin, gender, sexual orientation, disability).
  Includes slurs, calls for exclusion/violence, or claims a group is inferior.
- "offensive": rude, insulting, or profane, but NOT targeting a protected group.
- "neither": neutral, positive, or not offensive.

RULES:
- Judge the text as written; don't invent context.
- If it targets a protected group, prefer "hate_speech".
- "confidence" is 0.0-1.0. "target_groups" is an array (empty [] if none).
- "rationale" is ONE short sentence.
OUTPUT: return ONLY valid JSON matching:
{"label": string, "confidence": number, "target_groups": string[], "rationale": string}
```

**Enable structured output** so you always get parseable JSON. When you paste the response schema, it **must have a top-level `type: "object"`** — omitting it gets the schema rejected. Use exactly this:

```json
{
  "type": "object",
  "properties": {
    "label":         { "type": "string", "enum": ["hate_speech", "offensive", "neither"] },
    "confidence":    { "type": "number" },
    "target_groups": { "type": "array", "items": { "type": "string" } },
    "rationale":     { "type": "string" }
  },
  "required": ["label", "confidence", "target_groups", "rationale"]
}
```

Set **temperature `0`**; on Gemini 3.x set `thinking_level` to `minimal`/`low`.

## Step 2.3 — Test it from code

Install the SDK into the **correct interpreter**. If you have multiple Pythons on your Mac (common), scope the install to the one you'll run, or use the venv from the top of this guide:

```bash
/usr/local/bin/python3 -m pip install google-genai
```

Create `~/dev/hate-speech-adv/test_classify.py`:

```python
from google import genai
from google.genai import types

PROJECT = "hate-speech-adv"

# IMPORTANT: location="global", NOT europe-west3 — Gemini 3.x models are not served
# from regional endpoints and will 404 if you use the region here.
client = genai.Client(vertexai=True, project=PROJECT, location="global")

SYSTEM_PROMPT = """You are a content-moderation classifier. Classify the USER TEXT into exactly one label.

LABELS:
- "hate_speech": attacks or dehumanizes people based on a protected attribute
  (race, ethnicity, religion, national origin, gender, sexual orientation, disability).
  Includes slurs, calls for exclusion/violence, or claims a group is inferior.
- "offensive": rude, insulting, or profane, but NOT targeting a protected group.
- "neither": neutral, positive, or not offensive.

RULES:
- Judge the text as written; don't invent context.
- If it targets a protected group, prefer "hate_speech".
- "confidence" is 0.0-1.0. "target_groups" is an array (empty [] if none).
- "rationale" is ONE short sentence.
OUTPUT: return ONLY valid JSON matching:
{"label": string, "confidence": number, "target_groups": string[], "rationale": string}"""

resp = client.models.generate_content(
    model="gemini-3.1-flash-lite",          # stable ID, no -preview suffix
    contents="People of that religion are all criminals and should be banned.",
    config=types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0,
        response_mime_type="application/json",
    ),
)
print(resp.text)
```

Run it:

```bash
/usr/local/bin/python3 ~/dev/hate-speech-adv/test_classify.py
```

You should get clean JSON with `label: "hate_speech"` and a high confidence.

> **Two gotchas this step exists to catch:**
> - **`ModuleNotFoundError: google`** → the SDK went into a *different* Python than the one you ran. Fix with the scoped `/usr/local/bin/python3 -m pip install google-genai`, or activate the venv and `pip install` there.
> - **`404 NOT_FOUND`** → you used a regional endpoint. `gemini-3.1-flash-lite` requires `location="global"` in `genai.Client()`, even though your other infrastructure lives in `europe-west3`.

> **Carry-forward note for Phase 3:** the model sometimes returns the field names `classification` / `reasoning` instead of `label` / `rationale`. Your Phase 1 BigQuery schema uses `label` / `rationale`, so the worker guards against this with `r.get("label") or r.get("classification")`. Keep that guard in mind when you build the worker.

✅ **Checkpoint 2:** the test script prints valid JSON with a sensible label and high confidence.

---

# Phase 3 — Event-driven ingestion (Pub/Sub + Cloud Run)

**Goal:** A public API takes a POST, drops the text onto Pub/Sub, and a separate worker picks it up, classifies it with Vertex AI, and writes a row to BigQuery.

```
POST /classify  →  ingest (Cloud Run)  →  topic "messages"
                                              │  (push subscription "worker")
                                              ▼
                          worker (Cloud Run) → Vertex AI → BigQuery
```

We're building the worker as a **small Python Cloud Run service** rather than n8n. Reason: it reuses your working Phase 2 code directly, needs nothing running 24/7, and reads better in a portfolio. (n8n still gets its moment in Phase 4.)

## Step 3.1 — Build the ingestion API

Create the folder and three files.

```bash
mkdir -p ~/dev/hate-speech-adv/ingest
cd ~/dev/hate-speech-adv/ingest
```

**`main.py`**
```python
import os, json
from flask import Flask, request
from google.cloud import pubsub_v1

app = Flask(__name__)
publisher = pubsub_v1.PublisherClient()
TOPIC = publisher.topic_path(os.environ["PROJECT"], "incoming-messages")

@app.route("/", methods=["GET"])
def health():
    return {"status": "ok"}, 200

@app.route("/classify", methods=["POST"])
def classify():
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not text:
        return {"error": "missing 'text'"}, 400
    msg_id = publisher.publish(TOPIC, json.dumps({"text": text}).encode("utf-8")).result()
    return {"status": "queued", "message_id": msg_id}, 202
```

**`requirements.txt`**
```
flask==3.0.3
google-cloud-pubsub==2.23.0
gunicorn==22.0.0
```

**`Dockerfile`**
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 main:app
```

## Step 3.2 — Deploy the ingestion API

```bash
gcloud run deploy ingest --source . --region europe-west3 \
  --set-env-vars PROJECT=hate-speech-adv --allow-unauthenticated
```

The first run asks to create an Artifact Registry repo — say yes. When it finishes it prints a **Service URL**. Test it:

```bash
INGEST_URL="https://ingest-xxxxx-ey.a.run.app"   # paste your real URL
curl -X POST "$INGEST_URL/classify" \
  -H "Content-Type: application/json" \
  -d '{"text":"hello world"}'
```

You should get `{"status":"queued","message_id":"..."}`. The message is now sitting on the `incoming-messages` topic.

**✅ Checkpoint 3a:** In the console → Pub/Sub → topic `incoming-messages`, the "Messages" metric ticks up when you POST.

## Step 3.3 — Build the worker

The worker receives Pub/Sub **push** deliveries (Pub/Sub POSTs each message to the worker's URL as a JSON envelope).

```bash
mkdir -p ~/dev/hate-speech-adv/worker
cd ~/dev/hate-speech-adv/worker
```

**`main.py`**
```python
import os, json, base64, uuid
from datetime import datetime, timezone
from flask import Flask, request
from google import genai
from google.genai import types
from google.cloud import bigquery

app = Flask(__name__)

PROJECT = os.environ["PROJECT"]
MODEL   = "gemini-3.1-flash-lite"
TABLE   = f"{PROJECT}.moderation.classifications_raw"

# location="global" is REQUIRED for Gemini 3.x — regional endpoints 404
genai_client = genai.Client(vertexai=True, project=PROJECT, location="global")
bq = bigquery.Client(project=PROJECT)

SYSTEM_PROMPT = """You are a content-moderation classifier. Classify the USER TEXT into exactly one label.

LABELS:
- "hate_speech": attacks or dehumanizes people based on a protected attribute
  (race, ethnicity, religion, national origin, gender, sexual orientation, disability).
  Includes slurs, calls for exclusion/violence, or claims a group is inferior.
- "offensive": rude, insulting, or profane, but NOT targeting a protected group.
- "neither": neutral, positive, or not offensive.

RULES:
- Judge the text as written; don't invent context.
- If it targets a protected group, prefer "hate_speech".
- "confidence" is 0.0-1.0. "target_groups" is an array (empty [] if none).
- "rationale" is ONE short sentence.
OUTPUT: return ONLY valid JSON matching:
{"label": string, "confidence": number, "target_groups": string[], "rationale": string}"""

@app.route("/", methods=["POST"])
def receive():
    envelope = request.get_json(silent=True)
    if not envelope or "message" not in envelope:
        return ("bad Pub/Sub envelope", 400)

    msg = envelope["message"]
    payload = json.loads(base64.b64decode(msg["data"]).decode("utf-8"))
    text = payload["text"]

    resp = genai_client.models.generate_content(
        model=MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0,
            response_mime_type="application/json",
        ),
    )
    result = json.loads(resp.text)

    # --- field-name mismatch guard ---
    # The model sometimes emits classification/reasoning instead of label/rationale.
    label     = result.get("label")      or result.get("classification")
    rationale = result.get("rationale")  or result.get("reasoning")
    confidence    = result.get("confidence")
    target_groups = result.get("target_groups", [])

    row = {
        "message_id":    msg.get("messageId", str(uuid.uuid4())),
        "input_text":    text,
        "label":         label,
        "confidence":    confidence,
        "target_groups": json.dumps(target_groups),   # column is STRING
        "rationale":     rationale,
        "embedding":     None,
        "model_version": MODEL,
        "created_at":    datetime.now(timezone.utc).isoformat(),
    }

    errors = bq.insert_rows_json(TABLE, [row])
    if errors:
        print("BQ insert errors:", errors)
        return ("insert failed", 500)   # non-2xx → Pub/Sub retries

    return ("", 204)   # 2xx acks the message
```

**`requirements.txt`**
```
flask==3.0.3
google-genai==1.0.0
google-cloud-bigquery==3.25.0
gunicorn==22.0.0
```

**`Dockerfile`** — same as the ingest one (copy it over).

## Step 3.4 — Deploy the worker (private, running as the worker SA)

```bash
cd ~/dev/hate-speech-adv/worker
gcloud run deploy worker --source . --region europe-west3 \
  --set-env-vars PROJECT=hate-speech-adv \
  --service-account classifier-worker@hate-speech-adv.iam.gserviceaccount.com \
  --no-allow-unauthenticated
```

`--no-allow-unauthenticated` means only callers you explicitly permit (Pub/Sub) can hit it. Copy its **Service URL** — you need it next.

## Step 3.5 — Wire the push subscription (Terraform)

This is the fiddliest part of the whole project: three IAM pieces plus converting the `worker-sub` subscription from pull to push. Take it slowly.

**Deploy the `worker` service first (Step 3.4) before applying this** — the config reads the deployed service's URL automatically via a data source, so it must already exist.

In `infra/main.tf`, add these two data sources near the top (they look up the project number and the worker's URL so you don't hardcode anything):

```hcl
data "google_project" "current" {}

data "google_cloud_run_v2_service" "worker" {
  name     = "worker"
  location = "europe-west3"
}
```

**Replace** your existing `google_pubsub_subscription "worker"` block with the push version, and add the two IAM resources below it:

```hcl
# worker subscription, now PUSH to the Cloud Run worker
resource "google_pubsub_subscription" "worker" {
  name                 = "worker-sub"
  topic                = google_pubsub_topic.messages.name
  ack_deadline_seconds = 60

  push_config {
    push_endpoint = data.google_cloud_run_v2_service.worker.uri
    oidc_token {
      service_account_email = google_service_account.worker.email
    }
  }
}

# 1) Let the worker SA invoke the private worker service (authorizes the push)
resource "google_cloud_run_v2_service_iam_member" "worker_invoker" {
  name     = "worker"
  location = "europe-west3"
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.worker.email}"
}

# 2) Let the Pub/Sub service agent mint OIDC tokens as the worker SA
#    (scoped to just this SA, not project-wide)
resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}
```

Apply it — no variables needed, the data sources fill everything in:
```bash
cd ~/dev/hate-speech-adv/infra
terraform apply
```

The plan should **modify** `worker-sub` and **create** the two IAM members, with 0 destroyed. Verify in the console: Pub/Sub → Subscriptions → `worker-sub` → Delivery type reads **Push**.

> **Expect a short delay.** IAM changes take up to ~5 minutes to propagate. If the first test 403s or messages pile up unacked, wait a few minutes and retry before debugging.

## Step 3.6 — Test end to end

```bash
curl -X POST "$INGEST_URL/classify" \
  -H "Content-Type: application/json" \
  -d '{"text":"People of that religion are all criminals and should be banned."}'
```

Then in the console → BigQuery → `moderation.classifications_raw` → **Preview** (or run):
```sql
SELECT message_id, label, confidence, rationale, created_at
FROM `hate-speech-adv.moderation.classifications_raw`
ORDER BY created_at DESC LIMIT 5;
```

**✅ Checkpoint 3 (Core Path complete):** POST a message → a labeled row appears in BigQuery within a few seconds. If it doesn't, check worker logs: console → Cloud Run → `worker` → Logs.

> **Two grants you made by hand (not in Terraform).** Getting Phase 3 working required two `gcloud` IAM grants on the **Compute Engine default service account** (`YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com` — same project number as in the intro note), because that's the identity your `ingest` service runs as *and* the identity Cloud Build uses:
> ```bash
> # lets the source build run
> gcloud projects add-iam-policy-binding hate-speech-adv \
>   --member="serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
>   --role="roles/run.builder"
> # lets the ingest service publish to Pub/Sub
> gcloud projects add-iam-policy-binding hate-speech-adv \
>   --member="serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
>   --role="roles/pubsub.publisher"
> ```
> These live outside Terraform, so note them in your Phase 10 README. **Cleaner alternative for later:** give `ingest` its own dedicated service account (like the worker has) with only `roles/pubsub.publisher`, instead of leaning on the broad default compute SA. Not required now — just a portfolio-polish item.

---

# Phase 4 — Persistent n8n on Cloud Run + Cloud SQL

You already have a working code worker, so this phase is **optional** — but it's genuinely worth doing because it teaches **Cloud SQL (managed Postgres)** and **Secret Manager**, both of which show up constantly in job postings. Think of hosted n8n as a second, visual workflow tool in your portfolio (great for demos), running alongside the code pipeline.

> **Cost note:** This is the one phase with real ongoing cost. A small Cloud SQL instance + a Cloud Run service kept warm (`--min-instances=1`, `--memory 1Gi`, `--no-cpu-throttling` — all required for n8n to run reliably; see Step 4.4) runs a few € / month. It's still small money, but it's the one thing that costs while idle, so run `terraform destroy` or scale the n8n service to 0 (`gcloud run services update n8n --region europe-west3 --min-instances 0`) when you're not actively demoing.

> **Build order for this phase (read first).** You build the workflow **before** any cloud hosting, then relocate it to the cloud, then feed it from Pub/Sub:
> 1. **Step 4.1** — build & test the n8n workflow **locally** ← *this is where you actually build the workflow*
> 2. **Steps 4.2–4.3** — provision Cloud SQL + Secret Manager (Terraform)
> 3. **Step 4.4** — deploy n8n to Cloud Run and import your workflow
> 4. **Step 4.5** — wire a second Pub/Sub subscription so it runs in parallel with the code worker

## Step 4.1 — Build & test the workflow locally

**This is the step where you build the n8n workflow.** Do it first, locally, so you have a working and tested workflow to import into the hosted instance — instead of debugging workflow logic and Cloud SQL setup at the same time.

> **Note:** the detailed node-by-node n8n workflow build lives in a separate companion document (`n8n-twin-setup.md`) so this guide stays focused on the core cloud pipeline. If you're not publishing or following that companion, you can skip Phase 4 entirely — the Phase 3 code worker is the primary path, and everything in Phases 5–10 works without the n8n twin.

The companion guide's Parts A–E, in order:
- **Part A** — run the fresh, isolated local container (host port 6001).
- **Part B** — add the Google service-account credential in n8n.
- **Part C** — create the separate `classifications_n8n` BigQuery table.
- **Part D** — build the 6-node workflow: Webhook → Normalize → Vertex AI (HTTP) → Build row → BigQuery → Respond.
- **Part E** — test locally by curling the webhook until a labeled row lands in `classifications_n8n`.

**Do not continue past this step until Part E's checkpoint passes.** Everything below is just moving this exact working workflow into the cloud — no workflow logic changes.

## Step 4.2 — Cloud SQL Postgres (Terraform)

Add to `infra/main.tf`:
```hcl
resource "google_sql_database_instance" "n8n" {
  name             = "n8n-db"
  database_version = "POSTGRES_16"
  region           = "europe-west3"
  settings {
    tier = "db-f1-micro"   # smallest / cheapest
    ip_configuration { ipv4_enabled = true }
  }
  deletion_protection = false
}

resource "google_sql_database" "n8n" {
  name     = "n8n"
  instance = google_sql_database_instance.n8n.name
}

resource "random_password" "n8n_db" {
  length  = 24
  special = false
}

resource "google_sql_user" "n8n" {
  name     = "n8n"
  instance = google_sql_database_instance.n8n.name
  password = random_password.n8n_db.result
}
```

## Step 4.3 — Store the password + encryption key in Secret Manager

```hcl
resource "google_secret_manager_secret" "n8n_db_pw" {
  secret_id = "n8n-db-password"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "n8n_db_pw" {
  secret      = google_secret_manager_secret.n8n_db_pw.id
  secret_data = random_password.n8n_db.result
}

# Let the worker SA (which n8n will run as) read the secret
resource "google_secret_manager_secret_iam_member" "n8n_pw_reader" {
  secret_id = google_secret_manager_secret.n8n_db_pw.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

# n8n encryption key — persists so imported credentials survive container
# replacements (without this, a cold start regenerates it and silently
# breaks any credential you saved in the hosted n8n).
resource "random_password" "n8n_enc" {
  length  = 32
  special = false
}
resource "google_secret_manager_secret" "n8n_enc" {
  secret_id = "n8n-encryption-key"
  replication {
    auto {}
  }
}
resource "google_secret_manager_secret_version" "n8n_enc" {
  secret      = google_secret_manager_secret.n8n_enc.id
  secret_data = random_password.n8n_enc.result
}
resource "google_secret_manager_secret_iam_member" "n8n_enc_reader" {
  secret_id = google_secret_manager_secret.n8n_enc.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

# REQUIRED: let that SA actually connect to Cloud SQL.
# Without this, n8n starts but can't reach Postgres — same class of
# permission failure as the Pub/Sub 500 in Phase 3.
resource "google_project_iam_member" "n8n_cloudsql_client" {
  project = "hate-speech-adv"
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.worker.email}"
}
```

`terraform apply` (you'll also need the `random` provider — add `random = { source = "hashicorp/random" }` to your `required_providers`, then `terraform init` again).

## Step 4.4 — Deploy n8n to Cloud Run and import your workflow

Grab the connection name and deploy the official n8n image, mounting the secret as an env var and connecting to Cloud SQL:

This uses `--image` (not `--source`), so there's **no build step** — the `run.builder` grant from Phase 3 isn't involved here, and there's nothing to compile.

```bash
CONN=$(gcloud sql instances describe n8n-db --format="value(connectionName)")

gcloud run deploy n8n \
  --image n8nio/n8n:latest \
  --region europe-west3 \
  --port 5678 \
  --min-instances 1 \
  --memory 1Gi \
  --no-cpu-throttling \
  --service-account classifier-worker@hate-speech-adv.iam.gserviceaccount.com \
  --add-cloudsql-instances "$CONN" \
  --set-env-vars "DB_TYPE=postgresdb,DB_POSTGRESDB_DATABASE=n8n,DB_POSTGRESDB_USER=n8n,DB_POSTGRESDB_HOST=/cloudsql/$CONN,N8N_PORT=5678,N8N_PROTOCOL=https" \
  --set-secrets "DB_POSTGRESDB_PASSWORD=n8n-db-password:latest,N8N_ENCRYPTION_KEY=n8n-encryption-key:latest" \
  --allow-unauthenticated
```

> **Port note:** `--port 5678` tells Cloud Run to route traffic to container port 5678, and n8n listens on `N8N_PORT`, so those two **must match** (both 5678). The earlier draft had `N8N_PORT=443`, which would make n8n listen on the wrong port and the service would fail health checks. `N8N_PROTOCOL=https` is just for how n8n builds external links — Cloud Run terminates TLS for you.

> **Why `--memory 1Gi` and `--no-cpu-throttling` are in this command (required, not optional).** These two flags aren't optional polish — without them the deploy fails in two separate ways, both of which look like the mysterious `{"code":503,"message":"Database is not ready!"}` page:
> - **`--no-cpu-throttling`** — Cloud Run's default only gives a container CPU *while it's serving a request*. n8n runs its ~50-migration schema setup at **startup**, before any request arrives, so on the default setting it's starved of CPU, the migrations crawl, and Cloud SQL drops the idle connection mid-migration (`QueryFailedError: Connection terminated`). That leaves the database **half-migrated**, and every subsequent boot then dies on leftovers like `column "pinData" ... already exists`. Un-throttling CPU lets the whole migration run finish in one clean pass. (`--cpu-boost` / "Startup CPU boost" helps but only for a short burst — it's not enough on its own for the full run.)
> - **`--memory 1Gi`** — Cloud Run defaults to **512 MiB**. n8n is a Node app that uses ~515+ MiB just to idle, so it OOM-crashes seconds after reaching "ready" (`Memory limit of 512 MiB exceeded`, then `FATAL ERROR: ... JavaScript heap out of memory`, then `Uncaught signal: 6`). 1 GiB is the sane minimum and gives real headroom.

> **If your first attempt already got stuck half-migrated** (you'll see `Connection terminated` then `... already exists` in the logs), the database is in an inconsistent state and the fix is to reset it before redeploying with the flags above. The reliable sequence:
> ```bash
> # 1) Remove the service so nothing keeps reconnecting and re-locking the DB.
> #    (min-instances 0 is NOT enough — loading the URL wakes a new instance
> #    that grabs the DB right when you try to drop it.)
> gcloud run services delete n8n --region europe-west3
>
> # 2) Drop and recreate the database (now that nothing holds a connection).
> gcloud sql databases delete n8n --instance=n8n-db
> gcloud sql databases create n8n --instance=n8n-db
> ```
> If the drop still reports *"database is being accessed by other users,"* open **SQL → `n8n-db` → Cloud SQL Studio**, sign in as `postgres`, and run:
> ```sql
> SELECT pg_terminate_backend(pid) FROM pg_stat_activity
> WHERE datname = 'n8n' AND pid <> pg_backend_pid();
> ```
> then retry the drop. After the database is fresh, run the deploy command above (with both flags). A successful boot shows `n8n ready on ::, port 5678` in the logs and *stays up* instead of crashing a few seconds later. Note: once a boot reaches "ready," migrations are done — a later OOM fix does **not** require resetting the DB again, just redeploy with more memory.

> **Harmless noise you can ignore.** `Failed to start Python task runner ... Python 3 is missing` and `[license SDK] Skipping renewal on init` both appear on every boot and are unrelated to your pipeline — the Python runner only matters if you use Python Code nodes, which this workflow doesn't.

**Now tell n8n its own public URL (required — do this before testing webhooks).** By default hosted n8n doesn't know its external address, so the Webhook node prints `https://localhost:5678/webhook/...` — which is unreachable, and any curl against it fails with `Cannot POST /` (wrong path) or `Failed to connect to localhost` (wrong host). Grab the Service URL the deploy printed and set it as `WEBHOOK_URL` (this merges into the existing env vars without touching the secrets or Cloud SQL wiring):

```bash
gcloud run services update n8n --region europe-west3 \
  --update-env-vars WEBHOOK_URL=https://n8n-XXXXXXXXXXXX.europe-west3.run.app
```

After the new revision rolls out (~30s), the Webhook node's **Test URL** and **Production URL** tabs show the real `run.app` address, and you can copy them directly. (If you already know the URL, you can instead append `WEBHOOK_URL=...` to the `--set-env-vars` line in the deploy command above and skip this extra step.)

Open the printed URL and create your n8n owner account (this hosted instance is separate from your local one). Then **import the workflow you built in Step 4.1**: in your local n8n, open the workflow → ⋯ menu → **Download** to get a JSON file; in the hosted n8n, **Import from File**.

**Re-create the Google service-account credential here.** Credentials don't travel inside the workflow JSON, so the imported "Classify (Vertex AI)" and "Insert row" nodes will show a "credential not found" warning until you add the credential and select it in both. This is the same **Google Service Account API** credential you built in Part B — recreate it exactly:

1. **Get the key values.** You created `n8n-key.json` locally in Part B; reuse it if it's still there (`ls ~/dev/hate-speech-adv/n8n-key.json`), or mint a fresh key (a service account can hold several — this doesn't invalidate the local one):
   ```bash
   cd ~/dev/hate-speech-adv
   gcloud iam service-accounts keys create n8n-key.json \
     --iam-account=classifier-worker@hate-speech-adv.iam.gserviceaccount.com
   ```
2. **Add the credential:** in hosted n8n → **Credentials → Add credential → search "Google Service Account API"**.
3. **Service Account Email** ← the `client_email` value (ends in `@hate-speech-adv.iam.gserviceaccount.com`). Print it with:
   ```bash
   python3 -c "import json; print(json.load(open('$HOME/dev/hate-speech-adv/n8n-key.json'))['client_email'])"
   ```
4. **Private Key** ← this is the step that trips people up. **The field is single-line and strips real line breaks on paste**, so pasting a normal multi-line PEM block silently corrupts the key and you get `secretOrPrivateKey must be an asymmetric key when using RS256` when the node runs. Paste the **single-line form with literal `\n` markers instead** — n8n converts them back to real newlines internally. Print that form with:
   ```bash
   python3 -c "import json; print(json.load(open('$HOME/dev/hate-speech-adv/n8n-key.json'))['private_key'].encode('unicode_escape').decode())"
   ```
   Clear the field completely first (Cmd+A, delete), then paste the one long line — everything from `-----BEGIN PRIVATE KEY-----\n` through the trailing `\n` after `-----END PRIVATE KEY-----`. No surrounding quotes.
5. Turn **ON** the toggle **"Set up for use in HTTP Request node."**
6. In the **Scope(s)** field that appears, enter: `https://www.googleapis.com/auth/cloud-platform`
7. Name it `hate-speech-adv SA` and **Save.**
8. **Select it in both nodes:** open **"Classify (Vertex AI)"** → credential dropdown → pick `hate-speech-adv SA`; do the same in **"Insert row"**. One credential drives both.

> **If you still get the RS256 error after a single-line paste,** the key file itself is bad. Verify it: `python3 -c "import json; open('/tmp/pk.pem','w').write(json.load(open('$HOME/dev/hate-speech-adv/n8n-key.json'))['private_key'])" && openssl rsa -in /tmp/pk.pem -check -noout && rm /tmp/pk.pem` — `RSA key ok` means the key is fine and it was a paste issue; an error means mint a fresh key and repeat.

> **Credential persistence — already handled.** The `N8N_ENCRYPTION_KEY` you mounted from Secret Manager in the deploy above is what keeps that re-created credential working across cold starts. Without it, n8n would regenerate the key on each container replacement and silently orphan the credential. Nothing extra to do here — just don't remove that secret.

**Test the imported workflow.** In the Webhook node click **Listen for test event**, copy the **Test URL** (now a real `run.app` address), and — while it's listening — fire a message at it:
```bash
curl -X POST "https://n8n-XXXXXXXXXXXX.europe-west3.run.app/webhook-test/classify" \
  -H "Content-Type: application/json" \
  -d '{"text":"People of that religion are all criminals and should be banned."}'
```
The execution should light up node-by-node and a labeled row should land in `classifications_n8n`. (Long-lived key housekeeping: `n8n-key.json` is already gitignored, and now that it's pasted into n8n and stored encrypted, you can `rm` the local copy if you'd rather not keep it around.)

## Step 4.5 — Wire the second Pub/Sub subscription (run it in parallel)

Now make the hosted n8n a real parallel worker.

**First, activate the workflow (this is a prerequisite, not a formality).** In the hosted n8n, open the workflow and flip the **Active** toggle (top-right). A workflow's **Production URL** (`/webhook/classify`) only responds when it's Active — an inactive workflow rejects the Pub/Sub push, and you'll silently get rows in `classifications_raw` but not `classifications_n8n`.

> **Gotcha — the "Offline" banner blocks activation.** If the editor shows **⚠ Offline** (top-right: "No network connection. Workflow changes will be saved once the connection is restored"), the Activate toggle won't take, because *activating is a save operation* and n8n won't save while it thinks it's offline. This happens when the tab's socket drops — typically right after a container restart. Fix: hard-refresh the tab (Cmd+Shift+R); if it persists, confirm the container is up (**Cloud Run → `n8n` → Logs** shows a recent `n8n ready on ::, port 5678`), then refresh again. Only once the Offline banner is gone will the toggle stick.

With the workflow **Active**, copy its Webhook **Production URL** (Webhook node → **Production URL** tab; it'll be a real `run.app` address because you set `WEBHOOK_URL` in Step 4.4).

Add a second subscription to `infra/main.tf` so every published message also fans out to n8n (the code worker keeps getting its own copy via `worker-sub`). **Add this block exactly once, and paste your real Production URL into `push_endpoint`** — don't paste a second copy or leave the placeholder, or `terraform apply` fails with `Duplicate resource "google_pubsub_subscription" configuration` (resource names must be unique):

```hcl
# Second Pub/Sub subscription — fans every published message out to the
# hosted n8n worker in parallel with worker-sub (the Python worker).
resource "google_pubsub_subscription" "worker_n8n" {
  name                 = "worker-n8n-sub"
  topic                = google_pubsub_topic.messages.name
  ack_deadline_seconds = 60

  push_config {
    push_endpoint = "https://n8n-XXXXXXXXXXXX.europe-west3.run.app/webhook/classify"
    # n8n runs with --allow-unauthenticated, so no oidc_token block is needed.
    # If you later lock it down, add an oidc_token block like worker-sub's.
  }
}
```

`terraform apply` (expect **1 to add, 0 to change, 0 to destroy**). Now `POST /classify` fans out to **both** workers in parallel — Python → `classifications_raw`, n8n → `classifications_n8n` — with zero duplication because they read separate subscriptions and write separate tables. (Full detail in `n8n-twin-setup.md`, Part F.)

## Checkpoint 4

**Test A — fan-out (one POST → both tables).** Send one message, wait ~15s, then query both tables at once:

```bash
INGEST_URL=$(gcloud run services describe ingest --region europe-west3 --format="value(status.url)")
curl -X POST "$INGEST_URL/classify" -H "Content-Type: application/json" \
  -d '{"text":"People of that religion are all criminals and should be banned."}'
```
```sql
SELECT 'raw' AS source, message_id, label, confidence, created_at
FROM `hate-speech-adv.moderation.classifications_raw`
WHERE created_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 MINUTE)
UNION ALL
SELECT 'n8n' AS source, message_id, label, confidence, created_at
FROM `hate-speech-adv.moderation.classifications_n8n`
WHERE created_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 MINUTE)
ORDER BY created_at DESC;
```
**Pass = two rows**, one `source = raw` and one `source = n8n`.

> **If only `raw` comes back,** the n8n path didn't fire. Diagnose from the n8n side: in the workflow, click the **Executions** tab (top-center, next to **Editor** / **Evaluations**) — it lists every run. Empty list → the workflow wasn't **Active** when the push arrived (re-activate, per the gotcha above, and re-test). A red/failed run → open it to see which node errored. Cross-check from GCP: **Pub/Sub → Subscriptions → `worker-n8n-sub` → Metrics** — unacked messages piling up means Pub/Sub is pushing but getting non-200s back (inactive or erroring workflow).

**Test B — persistence (survives a restart).** Force a fresh container and confirm nothing is lost:

```bash
gcloud run services update n8n --region europe-west3 --update-env-vars RESTARTED_AT=$(date +%s)
```

Reload the n8n URL, log back in, and confirm the workflow is still there, still **Active**, and its `hate-speech-adv SA` credential is still attached (open the Classify node — no red "credential not found"). It survives because the workflow lives in Postgres and the encryption key is pinned via Secret Manager.

**✅ Checkpoint 4 passed** when both rows appear *and* the workflow is intact after the restart. You now have two independent workers processing the same stream in parallel — the full Core Path plus the persistent-n8n stretch.

*(That's the Core Path complete. Everything below is stretch value — add phases one at a time by what a target job asks for.)*

---

# STRETCH PHASES

# Phase 5 — Embeddings + near-duplicate detection

**Goal:** Give every message a semantic fingerprint (an embedding vector) so you can flag new messages that are near-identical to recent ones — a simple "brigading / coordinated abuse" signal.

We use **`gemini-embedding-001`** — Google's stable, generally-available text embedding model (the newer `gemini-embedding-2` is multimodal and still in preview; you don't need it here).

## Step 5.1 — Add a proper vector column to BigQuery

Your table has an `embedding STRING` column, but `VECTOR_SEARCH`/`ML.DISTANCE` need a numeric array. Add a new column. In the console → BigQuery → run:

```sql
ALTER TABLE `hate-speech-adv.moderation.classifications_raw`
ADD COLUMN IF NOT EXISTS embedding_vector ARRAY<FLOAT64>;
```

(Keep it in sync with Terraform by adding `{ name = "embedding_vector", type = "FLOAT", mode = "REPEATED" }` to the table's schema list so a future `apply` doesn't try to remove it.)

## Step 5.2 — Add embedding to the worker

In `worker/main.py`, after you compute `text` and before building `row`, add:

```python
    emb = genai_client.models.embed_content(
        model="gemini-embedding-001",
        contents=text,
        config=types.EmbedContentConfig(output_dimensionality=768),  # smaller = cheaper storage
    )
    embedding_vector = emb.embeddings[0].values
```

Then add to the `row` dict, right after the existing `"embedding": None,` line:
```python
        "embedding_vector": embedding_vector,
```

> **Keep the old `"embedding": None` line too.** It fills the original `embedding STRING` column; the new line fills the `embedding_vector ARRAY<FLOAT64>` column from Step 5.1. Both columns exist, so both keys belong in the row.

> **Optional quality tweak:** for slightly better duplicate detection you can add `task_type="SEMANTIC_SIMILARITY"` inside `EmbedContentConfig(...)`. Not required — the default works fine.

Redeploy the worker from the `worker/` folder (same command as Step 3.4):
```bash
cd ~/dev/hate-speech-adv/worker
gcloud run deploy worker --source . --region europe-west3 \
  --set-env-vars PROJECT=hate-speech-adv \
  --service-account classifier-worker@hate-speech-adv.iam.gserviceaccount.com \
  --no-allow-unauthenticated
```

> **Only messages sent *after* this deploy get a vector.** Rows already in the table keep `embedding_vector` empty — that matters for the query in Step 5.3, so send fresh messages now.

Now POST a small batch that includes a **deliberate near-duplicate pair** (two messages that mean nearly the same thing but are worded differently) plus a couple of unrelated ones, so the ranking has something to distinguish. If `$INGEST_URL` isn't set in this shell, grab it first:
```bash
INGEST_URL=$(gcloud run services describe ingest --region europe-west3 --format="value(status.url)")

curl -X POST "$INGEST_URL/classify" -H "Content-Type: application/json" \
  -d '{"text":"I really love this new song, it is amazing."}'
curl -X POST "$INGEST_URL/classify" -H "Content-Type: application/json" \
  -d '{"text":"This new track is fantastic, I adore it."}'
curl -X POST "$INGEST_URL/classify" -H "Content-Type: application/json" \
  -d '{"text":"The weather forecast says rain tomorrow afternoon."}'
curl -X POST "$INGEST_URL/classify" -H "Content-Type: application/json" \
  -d '{"text":"Remember to buy groceries after work today."}'
```
Give the worker ~15 seconds to process them into BigQuery. (The same mechanism works for actual repeated abuse — reword an abusive message twice and they'll cluster identically; the benign pair above just demonstrates it without adding more hateful text to the table.)

## Step 5.3 — Find near-duplicates

For a portfolio-scale table, a brute-force cosine distance is simplest (no index needed). This query is **self-contained** — it picks the target for you (no `message_id` copy-paste), and it only compares rows whose vector is the expected 768 length, which sidesteps the length-mismatch error described in the box below:

```sql
WITH valid AS (
  SELECT message_id, input_text, embedding_vector
  FROM `hate-speech-adv.moderation.classifications_raw`
  WHERE ARRAY_LENGTH(embedding_vector) = 768
),
target AS (
  SELECT embedding_vector
  FROM valid
  WHERE input_text LIKE '%love this new song%'   -- or ORDER BY message_id LIMIT 1 for any row
  LIMIT 1
)
SELECT
  v.message_id,
  v.input_text,
  ROUND(ML.DISTANCE(v.embedding_vector, t.embedding_vector, 'COSINE'), 4) AS distance
FROM valid v, target t
ORDER BY distance ASC
LIMIT 5;
```

Smaller distance = more semantically similar. The **top row is the target compared against itself → distance `0.0`** (your sanity check that the math works). The **next** row should be the paraphrase (`This new track is fantastic...`) with a small distance, clearly below the weather/groceries messages. That gap is the whole point: it matched *meaning*, not words. Reword an abusive message twice and the two versions rank as near-neighbors the same way.

> **Error: `Array inputs are not equal in length; error in ML.DISTANCE expression`.** `ML.DISTANCE` can only compare two vectors of the **same** length, and your table almost always ends up with a mix — some rows carry an *empty* `[]` vector (length 0) from early tests sent before the worker produced real embeddings, alongside your good length-768 rows. The moment the query compares a real vector against an empty one, it errors. Diagnose what lengths you have:
> ```sql
> SELECT ARRAY_LENGTH(embedding_vector) AS vector_length, COUNT(*) AS row_count
> FROM `hate-speech-adv.moderation.classifications_raw`
> WHERE embedding_vector IS NOT NULL
> GROUP BY vector_length
> ORDER BY vector_length;
> ```
> Seeing a `0` bucket confirms it. (Note: alias the count `row_count`, **not** `rows` — `ROWS` is a reserved keyword in BigQuery and `AS rows` throws `Syntax error: Unexpected keyword ROWS`.) The `ARRAY_LENGTH(...) = 768` filter in the query above already excludes the empty rows, so it runs cleanly regardless. Keeping that length guard permanently is good defensive practice and costs nothing.

> **Cleaning up the empty rows (optional).** To tidy the table, delete the length-0 rows once you've confirmed what they are:
> ```sql
> DELETE FROM `hate-speech-adv.moderation.classifications_raw`
> WHERE ARRAY_LENGTH(embedding_vector) = 0;
> ```
> BigQuery reports how many rows it removed — sanity-check the count. **Caveat:** rows written by the worker arrive via streaming inserts and sit in a buffer for up to ~90 minutes, during which `DELETE` is refused with a "streaming buffer" error. If you hit that, just wait and retry. Since the length guard already handles empties in the query, cleanup is cosmetic, not required.

**✅ Checkpoint 5:** the neighbor query returns the target at distance `0.0` and its paraphrase ranked next, above the unrelated messages — semantic near-duplicate detection working end to end.

---

# Phase 6 — dbt transformation layer

**Goal:** Turn raw rows into clean, tested analytics tables — the daily job of an analytics engineer.

## Step 6.1 — Install dbt and initialise the project (in your venv)

First **create** and then **activate** the virtual environment. The `python3 -m venv .venv` line is the one that's easy to skip — and without it, `source .venv/bin/activate` fails with `no such file or directory`. Creating the venv is a one-time thing; activating it is per-terminal-session.

```bash
cd ~/dev/hate-speech-adv
/usr/local/bin/python3 -m venv .venv     # one-time: builds the .venv folder + its bin/activate script
source .venv/bin/activate                # every new terminal: your prompt gains a (.venv) prefix
pip install dbt-bigquery
```

You'll know the venv is active when your prompt starts with `(.venv)`.

Now scaffold the dbt project. Run `dbt init` from the project root — it creates a `hate_speech/` folder right there (→ `~/dev/hate-speech-adv/hate_speech`):

```bash
dbt init hate_speech
```

`dbt init` asks a short series of questions. Answer them like this:

| Prompt | Answer |
|--------|--------|
| Which database would you like to use? | `1` (bigquery) |
| Desired authentication method | `oauth` |
| project (GCP project id) | `hate-speech-adv` |
| dataset | `moderation` |
| threads | `4` |
| job_execution_timeout_seconds | *(press Enter for the default)* |
| Desired location | `EU` (must match your BigQuery dataset's multi-region) |

That writes `~/.dbt/profiles.yml`. It should end up looking like this — edit it by hand if any answer came out wrong:

```yaml
hate_speech:
  target: dev
  outputs:
    dev:
      type: bigquery
      method: oauth
      project: hate-speech-adv
      dataset: moderation          # dbt will build models here
      location: EU                 # must match your dataset's location
      threads: 4
```

dbt uses your local `gcloud auth application-default login` credentials, so no keys.

**Move into the project folder and verify the connection.** Every `dbt` command (`dbt debug`, `dbt build`, …) must be run from *inside* the project folder — the one containing `dbt_project.yml`:

```bash
cd ~/dev/hate-speech-adv/hate_speech
rm -rf models/example    # delete the sample models dbt scaffolds; you don't need them
dbt debug                # should end with "All checks passed!"
```

> **If `dbt debug` says `1 check failed` → `project path <…/dbt_project.yml> not found`:** you're not standing in the project folder. That file lives *inside* the folder `dbt init` created, so `cd ~/dev/hate-speech-adv/hate_speech` and run `dbt debug` again. If you ever lose track of where it is, `find ~/dev/hate-speech-adv -name dbt_project.yml` prints the exact path — the folder it sits in *is* your project folder.

**✅ Mini-checkpoint:** `dbt debug` prints `All checks passed!`.

## Step 6.2 — Declare the source

In `models/`, create **`_sources.yml`**:
```yaml
version: 2
sources:
  - name: moderation
    database: hate-speech-adv
    schema: moderation
    tables:
      - name: classifications_raw
```

## Step 6.3 — Build the models

**`models/stg_classifications.sql`**
```sql
select
    message_id,
    input_text,
    label,
    confidence,
    rationale,
    model_version,
    created_at
from {{ source('moderation', 'classifications_raw') }}
where label is not null
```

> **Note — `embedding_vector` is deliberately left out here.** The 768-number vectors from Phase 5 belong in the raw table for similarity search, not in your analytics/BI layer — they'd bloat the mart and Looker Studio can't do anything useful with them. Keep the near-duplicate query (Step 5.3) as its own thing against `classifications_raw`; the dbt models stay lean and label-focused.

**`models/mart_daily_summary.sql`**
```sql
with base as (
    select date(created_at) as day, label, confidence
    from {{ ref('stg_classifications') }}
)
select
    day,
    label,
    count(*)                                   as message_count,
    round(avg(confidence), 3)                  as avg_confidence,
    countif(label = 'hate_speech')             as hate_count,
    round(safe_divide(countif(label = 'hate_speech'), count(*)), 3) as hate_rate
from base
group by day, label
order by day desc
```

## Step 6.4 — Add tests

**`models/_schema.yml`**
```yaml
version: 2
models:
  - name: stg_classifications
    columns:
      - name: message_id
        tests: [not_null]
      - name: label
        tests:
          - not_null
          - accepted_values:
              values: ['hate_speech', 'offensive', 'neither']
```

## Step 6.5 — Run it

```bash
dbt build
```

`dbt build` runs the models **and** the tests. You'll see the two tables created and the tests pass (or fail loudly if a bad label slipped through — that's the point).

**✅ Checkpoint 6:** `dbt build` succeeds and creates `stg_classifications` + `mart_daily_summary` with passing tests.

> **If the mart comes back empty:** `dbt build` still succeeds, but a mart can only summarise rows that exist — if `classifications_raw` has little or no data, `mart_daily_summary` will be sparse or empty. POST a few varied messages through your Phase 3 ingest API first, then re-run `dbt build`.

---

# Phase 7 — Dashboard + alerting

## Step 7.1 — Looker Studio dashboard

1. Go to **lookerstudio.google.com** → **Create → Report**.
2. **Add data → BigQuery →** project `hate-speech-adv` → dataset `moderation` → table **`mart_daily_summary`** → Add.
3. Build a few charts:
   - **Time series:** dimension `day`, metric `message_count` — volume over time.
   - **Pie/bar:** dimension `label`, metric `message_count` — label breakdown.
   - **Scorecard:** metric `hate_rate` — headline number.
   - **Table:** sorted by `avg_confidence` ascending — a low-confidence review queue.
4. Rename the report and **Share → Anyone with the link → Viewer** so you can drop it in your README.

**✅ Checkpoint 7a:** A live dashboard reading from your dbt mart.

## Step 7.2 — Cloud Monitoring alert (fires on a real test)

Simplest reliable alert: worker error rate.

1. Console → **Monitoring → Alerting → Create policy**.
2. **Select a metric:** resource **Cloud Run Revision**, metric **Request Count**. Add a filter: `service_name = worker` and `response_code_class = 5xx`.
3. **Condition:** threshold, trigger if value **is above 0** over a rolling 1-minute window.
4. **Notifications and name:**
   - Add an email notification channel (yourself). This is the field that shows *"Some form fields are incorrect"* until it's filled.
   - **Policy user labels** (optional, "Recommended" in the console): purely metadata tags for organizing/filtering policies later — they don't affect firing. If you want a clean setup, click **Add Label** and add `component` → `worker` (and optionally `env` → `prod`). Keys must be lowercase letters/numbers/underscores, no spaces. Safe to skip entirely.
   - **Severity:** `Warning` is fine.
   - **Name** it `Worker 5xx errors` and **Create Policy**.

### Test that it actually fires

The obvious test **does not work** — don't be fooled by it:
```bash
curl -X POST "$INGEST_URL/classify" -H "Content-Type: application/json" -d '{"text":""}'
# → {"error":"missing 'text'"}   (HTTP 400 from INGEST, not the worker)
```
An empty string is rejected by the **ingest** service with a 400 *before anything is published*, so the **worker never runs** and produces no 5xx. The alert stays quiet and the metric shows "No data available." Your alert only watches the **worker** service, so you have to make the *worker* itself crash.

**The reliable way** is to publish a message that's missing the `text` key straight to the topic. The worker does `payload["text"]`, throws a `KeyError`, and returns **500**:
```bash
gcloud pubsub topics publish incoming-messages --message='{"foo":"bar"}'
```
The push subscription delivers it to `worker`, the worker 500s, and within ~1–2 minutes you get the alert email. (Confirm in Console → Cloud Run → `worker` → **Logs**: you'll see `POST 500` rows and a Flask traceback.)

### ⚠️ Stop the retry loop afterwards

A 500 tells Pub/Sub the message *wasn't processed*, so Pub/Sub **redelivers the same poison message ~every 0.6 s** — you'll see the worker log fill with hundreds of identical 500s. This is expected (it's why the alert fires so reliably), but you must stop it once the email arrives. Purge the message off the subscription:

- **Console:** Pub/Sub → Subscriptions → `worker-sub` → **Purge messages** → confirm.
- **Or CLI** (discards everything published before "now"):
  ```bash
  gcloud pubsub subscriptions seek worker-sub --time=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  ```
Refresh the worker logs; the 500s stop within a few seconds.

> **Stretch:** For a true "hate-rate spike" alert you'd create a **scheduled BigQuery query** that writes a metric, or a **log-based metric**, then alert on that. The 5xx alert above is enough to demonstrate the skill and say "pipeline monitoring" in interviews.

> **Production hardening (note it for the Phase 10 README, don't build it now):** right now *any* malformed message retries forever and would trigger an alert storm. Two standard fixes: (1) wrap the worker's parse in `try/except` and return a **400 or 204-ack** for un-parseable payloads, so you only emit 500s for genuinely *transient* failures worth retrying (e.g. a temporary BigQuery hiccup); (2) attach a **dead-letter topic** to `worker-sub` (e.g. max 5 delivery attempts) so poison messages get parked instead of looping. Both are real production instincts worth a sentence in interviews.

**✅ Checkpoint 7b:** One alert policy that fires on a test — email received, then subscription purged so the worker logs are quiet again.

---

# Phase 8 — CI/CD with GitHub Actions (keyless / Workload Identity Federation)

> **Does Phase 7 change anything here? Almost nothing — and that's the point.** The `Worker 5xx errors` alert watches the Cloud Run service **by name** (`service_name = worker`), so every CI redeploy in this phase creates a new *revision* under the same service and the alert keeps working untouched. No filter edits, no re-creating the policy. The one thing to be aware of: once CI/CD is live and you're running the pipeline "for real," an un-handled malformed message will both **loop forever** *and* **spam this alert** (see the Phase 7 hardening note). So the `try/except` + dead-letter hardening moves from "nice-to-have" to "do this before you leave it running unattended." If you add it, it ships through the very pipeline you're building in this phase — no special handling. Everything from Phase 9 onwards is likewise unaffected by the Phase 7 alert.

**Goal:** Push to `main` → GitHub Actions deploys your Cloud Run services automatically, authenticating to GCP with **no stored key** (the modern, résumé-worthy way).

> We'll scope CI to **deploying the two Cloud Run services**, not running `terraform apply` (which would need very broad permissions). Keep running Terraform manually.

## Step 8.1 — Enable the APIs WIF needs

```bash
gcloud services enable \
  iam.googleapis.com sts.googleapis.com iamcredentials.googleapis.com \
  --project hate-speech-adv
```

## Step 8.2 — Create the Workload Identity Pool + Provider

```bash
PROJECT_ID=hate-speech-adv
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
REPO="YOUR_GITHUB_USERNAME/hate-speech-adv"   # <-- change to your actual GitHub repo

gcloud iam workload-identity-pools create github \
  --project=$PROJECT_ID --location=global --display-name="GitHub Actions Pool"

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --project=$PROJECT_ID --location=global \
  --workload-identity-pool=github \
  --display-name="GitHub OIDC" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository=='${REPO}'"
```

> The **attribute condition is mandatory for security** — without it, *any* GitHub repo on the planet could authenticate to your project. Restricting to your exact repo is the safe default.

## Step 8.3 — Create the deploy service account and grant it deploy permissions

```bash
gcloud iam service-accounts create github-deployer \
  --project=$PROJECT_ID --display-name="GitHub Actions Deployer"

DEPLOYER="github-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

for ROLE in \
  roles/run.admin \
  roles/iam.serviceAccountUser \
  roles/cloudbuild.builds.editor \
  roles/artifactregistry.writer \
  roles/storage.admin ; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:${DEPLOYER}" --role="$ROLE"
done
```

(`run.admin` + `serviceAccountUser` deploy the service; the other three cover `--source` builds via Cloud Build + Artifact Registry.)

> **Two things carried over from Phase 3 that make this work:** (1) `--source` builds still run as the **Compute Engine default service account**, which you already granted `roles/run.builder` back in Phase 3 — so CI builds won't hit that permission error. (2) The project-level `roles/iam.serviceAccountUser` above lets `github-deployer` *act as* both the build SA and the `classifier-worker` runtime SA it assigns to the worker service. If a CI deploy ever fails with `iam.serviceAccounts.actAs denied`, that's the role to check.

## Step 8.4 — Let your repo impersonate the deployer

```bash
gcloud iam service-accounts add-iam-policy-binding $DEPLOYER \
  --project=$PROJECT_ID \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}"
```

Grab the full provider name for the workflow (note it uses the **project number**, not the ID):
```bash
gcloud iam workload-identity-pools providers describe github-provider \
  --project=$PROJECT_ID --location=global \
  --workload-identity-pool=github --format="value(name)"
# → projects/YOUR_PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/github-provider
```

> **Copy only the command, not the `# →` line.** That last line is just showing you the *expected output* — it's a comment. If you paste it into the terminal, zsh (unlike bash) doesn't treat `#` as a comment at an interactive prompt, so it tries to run `#` as a command and you get `zsh: command not found: #`. Harmless, but confusing — the real output is the `projects/<PROJECT_NUMBER>/...` line the command prints on its own. That printed string is the value you paste into `workload_identity_provider` in Step 8.5. (General rule for this whole guide: when copying a command block, stop before any line starting with `#`.)

## Step 8.5 — The workflow file

> **⚠️ This block is a *file*, not terminal commands.** Everything below is the *contents* of a YAML file that lives in your repo. Do **not** paste it into your terminal — zsh will try to run `id:`, `uses:`, `with:` as commands and spit out a wall of `command not found`. It belongs **inside a file**. (Same idea as the `main.py` blocks back in Phase 3: file contents, not shell commands. When a block is YAML, Python, or HCL, it's a file; when it's `gcloud`/`git`/`curl`, it's a command.)

The reliable way to create it — **swap in your project number on the `workload_identity_provider` line first** (the exact string Step 8.4 printed), then run these in the terminal from your project root and the whole file gets written in one shot:

```bash
cd ~/dev/hate-speech-adv
mkdir -p .github/workflows

cat > .github/workflows/deploy.yml << 'EOF'
name: Deploy to Cloud Run
on:
  push:
    branches: [main]

permissions:
  contents: read
  id-token: write

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - id: auth
        uses: google-github-actions/auth@v3
        with:
          workload_identity_provider: 'projects/YOUR_PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/github-provider'
          service_account: 'github-deployer@hate-speech-adv.iam.gserviceaccount.com'

      - uses: google-github-actions/setup-gcloud@v3

      - name: Deploy ingest
        run: |
          gcloud run deploy ingest --source ./ingest --region europe-west3 \
            --set-env-vars PROJECT=hate-speech-adv --allow-unauthenticated --quiet

      - name: Deploy worker
        run: |
          gcloud run deploy worker --source ./worker --region europe-west3 \
            --set-env-vars PROJECT=hate-speech-adv \
            --service-account classifier-worker@hate-speech-adv.iam.gserviceaccount.com \
            --no-allow-unauthenticated --quiet
EOF
```

Verify it landed with `cat .github/workflows/deploy.yml` — you should see the whole workflow printed back. (Prefer an editor? `code .github/workflows/deploy.yml` opens it in VS Code and you can paste the YAML there instead — same result, clearer for future edits.)

> **Where does the `workload_identity_provider` value come from?** It's the exact string that Step 8.4's `describe` command printed — `projects/YOUR_PROJECT_NUMBER/locations/.../github-provider`, using your **project number**, not the project ID. Substitute your real project number (and GitHub username) into the heredoc before running it. If you ever recreate the pool, re-run the 8.4 `describe` and swap in the new string.

<details>
<summary>Reference: the same file, shown as plain YAML (for reading, not pasting)</summary>

```yaml
name: Deploy to Cloud Run
on:
  push:
    branches: [main]

permissions:
  contents: read
  id-token: write        # required to request the OIDC token

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - id: auth
        uses: google-github-actions/auth@v3
        with:
          workload_identity_provider: 'projects/YOUR_PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/github-provider'
          service_account: 'github-deployer@hate-speech-adv.iam.gserviceaccount.com'

      - uses: google-github-actions/setup-gcloud@v3

      - name: Deploy ingest
        run: |
          gcloud run deploy ingest --source ./ingest --region europe-west3 \
            --set-env-vars PROJECT=hate-speech-adv --allow-unauthenticated --quiet

      - name: Deploy worker
        run: |
          gcloud run deploy worker --source ./worker --region europe-west3 \
            --set-env-vars PROJECT=hate-speech-adv \
            --service-account classifier-worker@hate-speech-adv.iam.gserviceaccount.com \
            --no-allow-unauthenticated --quiet
```

</details>

## Step 8.6 — Put it on GitHub and push (this is what triggers the deploy)

Your code is still only on your Mac. The workflow file just written does nothing until the repo lives on GitHub and you push to `main`. This step has the most moving parts of the whole phase, so go slowly — the troubleshooting box after it covers every error you're likely to hit (each one is normal).

### 1. Add a `.gitignore` first — before you commit anything

This matters: your `infra/` folder holds Terraform **state**, which can contain secrets (like the Cloud SQL password from Phase 4). That must never land on GitHub. From the project root:

```bash
cd ~/dev/hate-speech-adv

cat > .gitignore << 'EOF'
# Python
.venv/
__pycache__/
*.pyc

# Terraform — state can hold secrets, .terraform is huge
infra/.terraform/
*.tfstate
*.tfstate.*
crash.log

# Credentials — never commit these
*-key.json
service-account*.json
.env

# OS junk
.DS_Store
EOF
```

### 2. Initialize git and make the first commit

```bash
git init
git branch -M main
git add .
git status
```

**Read what `git status` lists before committing.** You want to see `infra/main.tf`, `ingest/`, `worker/`, and `.github/workflows/deploy.yml` — but **no** `*.tfstate` files and **no** `*-key.json`. If the junk files aren't listed, `.gitignore` did its job. Then:

```bash
git commit -m "Event-driven moderation pipeline with keyless CI/CD"
```

### 3. Create the GitHub repo — the name must match exactly

Back in Step 8.2 you locked the trust condition to `YOUR_GITHUB_USERNAME/hate-speech-adv`. So the repo **must** be owned by `YOUR_GITHUB_USERNAME` and named **exactly** `hate-speech-adv` — a different name authenticates fine but GCP rejects the deploy with a 403.

Go to **github.com/new** and set:
- **Owner:** `YOUR_GITHUB_USERNAME`
- **Repository name:** `hate-speech-adv` (exactly this)
- **Public** is fine — it's a portfolio piece, and your secrets are gitignored
- **Do NOT** check "Add a README / .gitignore / license" — an empty repo avoids the divergent-history problem in the troubleshooting box below

Click **Create repository**, then connect and push:

```bash
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/hate-speech-adv.git
git push -u origin main
```

> **One-liner alternative:** if you have the GitHub CLI, `gh repo create hate-speech-adv --public --source=. --push` creates the repo and pushes in a single command (and handles auth for you — see the troubleshooting box).

### 4. Watch it deploy

The moment the push lands on `main`, open your repo → **Actions** tab. You'll see a run named after your commit. Click in: the `deploy` job runs checkout → auth → **Deploy ingest** → **Deploy worker**. Green checks on all of them **is the checkpoint**.

---

### 🔧 Troubleshooting the push (every one of these is normal)

The first push is where everyone hits friction. Work down this list in order — these are the exact errors, in the order they tend to appear:

**`error: remote origin already exists`** — Harmless. You already ran `git remote add origin` once. Confirm it points to the right place with `git remote -v` (both lines should read `.../YOUR_GITHUB_USERNAME/hate-speech-adv.git`). If it's wrong, fix it with `git remote set-url origin https://github.com/YOUR_GITHUB_USERNAME/hate-speech-adv.git`.

**`Password authentication is not supported` / `Authentication failed`** — GitHub turned off password login in 2021; typing your account password will always fail. Two fixes:
- **Easiest (recommended) — GitHub CLI:** it does a browser login and wires up git for you, no tokens to manage.
  ```bash
  brew install gh
  gh auth login
  ```
  Answer the prompts: **GitHub.com** → **HTTPS** → **Yes** (authenticate Git) → **Login with a web browser**. It shows a one-time code, opens your browser, you paste + authorize. Then `git push -u origin main` just works.
- **Alternative — Personal Access Token (PAT):** github.com/settings/tokens → **Generate new token (classic)** → check the **`repo`** scope → generate and **copy** it (starts with `ghp_`, shown once). Re-run the push; at **Username** type `YOUR_GITHUB_USERNAME`, and at **Password** paste the **token** (not your account password). macOS saves it to Keychain so you're only asked once.

**`! [rejected] main -> main (fetch first)`** or **`(non-fast-forward)`** — The remote has a commit you don't have locally (usually because a README/license got added when the repo was created). Pull it down and merge the two histories, then push:
```bash
git config pull.rebase false                          # "when histories diverge, merge them"
git pull origin main --allow-unrelated-histories      # stitches the two separate roots together
git push -u origin main
```
The `git config pull.rebase false` line is easy to skip but **required** — without it, git refuses to guess how to reconcile and bails with `Need to specify how to reconcile divergent branches`. The `--allow-unrelated-histories` flag is what lets your local repo and the GitHub one (which started as separate roots) join.

**The `git pull` opens a text editor** (a merge-commit message like `Merge branch 'main'`) — that's expected, the default message is fine. Save and close:
- **nano** (shows `^O ^X` at the bottom): `Ctrl+O`, `Enter`, then `Ctrl+X`
- **vim** (a column of `~` down the left): type `:wq` then `Enter`

**`error: You have not concluded your merge (MERGE_HEAD exists)`** — A previous pull started a merge but never finished it. Just complete it, then push:
```bash
git commit --no-edit
git push -u origin main
```

**Nothing lines up no matter what you try — check you're in the right folder.** If your terminal prompt shows a *different* folder name (e.g. `hate_speech` — the dbt subfolder — instead of `hate-speech-adv`), you're running git from the wrong directory. This silently breaks everything. Verify and fix:
```bash
pwd                       # should end in /hate-speech-adv
ls                        # should show ingest/ worker/ infra/ .github/
cd ~/dev/hate-speech-adv  # if you were somewhere else
```
Then retry the push from the correct repo.

---

> **Two gotchas from the docs:** (1) IAM/pool changes can take ~5 minutes to propagate — if the first Actions run **fails at the `auth` step with a 403**, that's the binding from 8.4 still propagating. Wait a few minutes and **re-run the job** from the Actions tab — no code change needed. It's the single most common first-run stumble and it fixes itself. (2) GitHub is rolling out immutable subject claims (default for new repos from June 18, 2026); the `attribute.repository` condition we used is unaffected, so you're fine.

**✅ Checkpoint 8:** Repo is on GitHub, and a push to `main` → GitHub Actions deploys both services with zero stored credentials (green checks on checkout, auth, Deploy ingest, Deploy worker). From here on, every change ships by pushing to `main`.

---

## Where you are now

After Phase 8 you can honestly say the interview sentence — event-driven ingestion, Vertex AI classification with embeddings-based duplicate detection, BigQuery + dbt, Looker Studio + Cloud Monitoring, all Terraform-provisioned and deployed via keyless GitHub Actions. Phase 9 (the eval harness) is the last build piece and the one that most proves you're an engineer, not just a wirer-together of tools. Phase 10 turns the whole thing into a portfolio artifact.

**Cost hygiene:** the only things that cost money while idle are Cloud SQL (Phase 4) and any `--min-instances=1` service. `terraform destroy` or scale those to zero between demos; the code workers and Pub/Sub scale to zero on their own.

---

# Phase 9 — Evaluation harness (~2 hrs)

**Goal:** Prove — with numbers — how good your classifier actually is, and optionally compare it against other models (a bigger Gemini tier, or a local Ollama model if you have one). This is the part that separates "I wired some services together" from "I measured my system and can defend it." An accuracy figure and a confusion matrix are exactly the kind of concrete evidence interviewers latch onto.

> **Term — confusion matrix:** a small grid that shows, for each true label, what the model *predicted*. The diagonal is correct predictions; everything off the diagonal is a specific mistake (e.g. "offensive" texts wrongly called "hate_speech"). It tells you not just *how often* the model is wrong but *how* it's wrong.

Work in the virtual environment from the top of this guide so the interpreter and packages are clean:

```bash
cd ~/dev/hate-speech-adv
source .venv/bin/activate        # create it first if you haven't: /usr/local/bin/python3 -m venv .venv
pip install google-genai scikit-learn pandas tabulate
```

## Step 9.1 — Build a labeled eval set (~150+ messages)

Create `eval/labeled.csv` with two columns: `text` and `label`, where `label` is exactly one of `hate_speech`, `offensive`, `neither`. Aim for **180 rows, evenly split 60/60/60** across the three classes so accuracy isn't dominated by one class and every row of the confusion matrix is directly comparable.

```bash
mkdir -p ~/dev/hate-speech-adv/eval
```

**`eval/labeled.csv`** (format — the header + quoted-text style; the full 180-row file is generated, not hand-typed):
```csv
text,label
"I love this new song!",neither
"Have a great weekend everyone",neither
"This film is garbage and its fans are idiots.",offensive
"You're an absolute moron and everyone knows it.",offensive
"People of that religion are all criminals and should be banned.",hate_speech
"Members of that ethnic group are subhuman.",hate_speech
```

Python's `csv` writer only quotes cells that contain a comma, which is correct CSV — don't worry that some rows are quoted and others aren't.

**Design principles that make this a *meaningful* eval set** (not just a random dump):

- **Balanced classes (60/60/60)** so a lazy model can't score well by always guessing the majority label, and per-class metrics are comparable.
- **False-positive traps in `neither`:** neutral mentions of protected groups ("Members of the local church organized a food drive") and criticism of *ideas/policies* rather than *people* ("I strongly disagree with the teachings of that religion"). A weak classifier over-flags these as `hate_speech` — this is exactly the boundary you want to measure.
- **The `offensive` / `hate_speech` boundary is the hard part:** `offensive` rows are rude/profane but never target a protected attribute; `hate_speech` rows all attack based on race, ethnicity, religion, national origin, gender, sexual orientation, or disability. This is where most misclassifications land, so load up on cases right on that line.
- **Templated, non-slur hate examples:** abstract group references ("that religion", "that ethnic group") rather than real slurs or named groups — enough to test the *pattern* your rubric defines (dehumanization, calls for exclusion/violence, claims of inferiority) without the file being a pile of slurs. This is safer to keep in a public portfolio repo, too.

> **Trust your labels.** Because the rows are generated to follow the rubric above, **hand-check 15–20 of them yourself** — especially the boundary cases — before treating the file as ground truth. A benchmark is only as trustworthy as its labels, and spot-verifying a sample is cheap insurance. This CSV is the "ground truth" everything else is measured against.

> **Sourcing tips:** you can also pull a few real (mild) examples from your n8n test history and write some yourself. A clean, correctly-labeled 180 beats a noisy 500. Keep anything genuinely extreme out — you don't need it to demonstrate the skill.

## Step 9.2 — The eval script

This runs every row through one or more models, compares predictions to your labels, and prints accuracy + a confusion matrix per model. It reuses the **exact same prompt and `location="global"` client** as your deployed worker, so you're measuring the real thing.

**`eval/run_eval.py`**
```python
import csv, json, sys
from google import genai
from google.genai import types
from sklearn.metrics import accuracy_score, confusion_matrix, classification_report
from tabulate import tabulate

PROJECT = "hate-speech-adv"
LABELS  = ["hate_speech", "offensive", "neither"]

# Same client + prompt as the deployed worker
client = genai.Client(vertexai=True, project=PROJECT, location="global")

SYSTEM_PROMPT = """You are a content-moderation classifier. Classify the USER TEXT into exactly one label.

LABELS:
- "hate_speech": attacks or dehumanizes people based on a protected attribute
  (race, ethnicity, religion, national origin, gender, sexual orientation, disability).
  Includes slurs, calls for exclusion/violence, or claims a group is inferior.
- "offensive": rude, insulting, or profane, but NOT targeting a protected group.
- "neither": neutral, positive, or not offensive.

RULES:
- Judge the text as written; don't invent context.
- If it targets a protected group, prefer "hate_speech".
- "confidence" is 0.0-1.0. "target_groups" is an array (empty [] if none).
- "rationale" is ONE short sentence.
OUTPUT: return ONLY valid JSON matching:
{"label": string, "confidence": number, "target_groups": string[], "rationale": string}"""

def classify(model, text):
    resp = client.models.generate_content(
        model=model,
        contents=text,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0,
            response_mime_type="application/json",
        ),
    )
    r = json.loads(resp.text)
    # same field-name guard as the worker
    return r.get("label") or r.get("classification")

def load(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r["text"] for r in rows], [r["label"] for r in rows]

def evaluate(model, texts, gold):
    preds = []
    for i, t in enumerate(texts, 1):
        try:
            preds.append(classify(model, t))
        except Exception as e:
            print(f"  row {i} failed: {e}")
            preds.append("neither")   # safe fallback so lengths line up
        print(f"\r  {i}/{len(texts)}", end="", flush=True)
    print()
    acc = accuracy_score(gold, preds)
    cm  = confusion_matrix(gold, preds, labels=LABELS)
    print(f"\n=== {model} ===")
    print(f"accuracy: {acc:.3f}")
    print(tabulate(
        [[LABELS[i]] + list(cm[i]) for i in range(len(LABELS))],
        headers=["true\\pred"] + LABELS, tablefmt="github"))
    print("\n" + classification_report(gold, preds, labels=LABELS, zero_division=0))
    return acc

if __name__ == "__main__":
    texts, gold = load("eval/labeled.csv")
    models = sys.argv[1:] or ["gemini-3.1-flash-lite"]
    results = {m: evaluate(m, texts, gold) for m in models}
    print("\n=== SUMMARY ===")
    print(tabulate([[m, f"{a:.3f}"] for m, a in results.items()],
                   headers=["model", "accuracy"], tablefmt="github"))
```

Run it against one or several models (each extra model is just another argument):

```bash
python eval/run_eval.py gemini-3.1-flash-lite
# compare tiers:
python eval/run_eval.py gemini-3.1-flash-lite gemini-2.5-flash
```

> **Including a local Ollama model (optional, great story).** If you have Ollama installed, add a second `classify_ollama(text)` function that POSTs to `http://localhost:11434/api/generate` with the same prompt, and pass a pseudo-name like `ollama:llama3` as an argument. If you migrated to this pipeline from a local setup, being able to say *"the managed Flash-Lite model beat my old local model by X points on my own eval set"* is a concrete, honest before/after — exactly the kind of migration story that lands in interviews.

## Step 9.3 — Log eval runs to BigQuery for drift tracking

Re-running the eval every so often and storing the accuracy lets you *watch for drift* — if a model update quietly drops your accuracy, you'll see it. You'll add a tiny `eval_runs` table that gets one new row per model every time the eval runs. "I log eval metrics over time to detect model drift" is a genuine MLOps sentence, and even this minimal version demonstrates the instinct — which is what matters for a portfolio.

### Step 1 — Add the `eval_runs` table to Terraform

Staying consistent with Phase 1, add the table as code rather than clicking it into existence. Open `~/dev/hate-speech-adv/infra/main.tf` and paste this near your existing `classifications_raw` table resource:

```hcl
resource "google_bigquery_table" "eval_runs" {
  dataset_id          = google_bigquery_dataset.moderation.dataset_id
  table_id            = "eval_runs"
  deletion_protection = false
  schema = jsonencode([
    { name = "run_at",   type = "TIMESTAMP" },
    { name = "model",    type = "STRING" },
    { name = "accuracy", type = "FLOAT" },
    { name = "n",        type = "INTEGER" },
  ])
}
```

Apply it:

```bash
cd ~/dev/hate-speech-adv/infra
terraform plan    # should show "1 to add" — the eval_runs table
terraform apply   # type 'yes'
```

`terraform plan` should read **1 to add, 0 to change, 0 to destroy**. If it wants to change or destroy anything else, stop and check before typing yes.

### Step 2 — Install the BigQuery client library

The eval script runs locally, so it needs the Python BigQuery library — **installed into the same venv you run the script from** (Phase 9's setup activated it; if you're in a fresh terminal, activate it again first):

```bash
cd ~/dev/hate-speech-adv
source .venv/bin/activate
pip install google-cloud-bigquery
```

> **Why the venv matters here:** installing with the bare system interpreter (`/usr/local/bin/python3 -m pip install ...`) while the venv is active puts the package in a *different* Python than the one `python eval/run_eval.py` runs — and Step 4 then fails with `ModuleNotFoundError: No module named 'google.cloud'`. Same class of interpreter mismatch as the Phase 2 gotcha; the venv sidesteps it entirely.

No keys needed — the script authenticates with the `gcloud auth application-default login` you ran in Phase 0.

### Step 3 — Add the logging code to `run_eval.py`

Three small edits to `eval/run_eval.py`:

**3a.** Next to the other imports at the top, add:
```python
from datetime import datetime, timezone
from google.cloud import bigquery
```

**3b.** Just below the existing `LABELS = [...]` line, add:
```python
BQ_DATASET = "moderation"
BQ_TABLE   = "eval_runs"
```

**3c.** Add this function anywhere above the `if __name__ == "__main__":` block:
```python
def log_to_bigquery(results, n):
    bq = bigquery.Client(project=PROJECT)
    table_id = f"{PROJECT}.{BQ_DATASET}.{BQ_TABLE}"
    run_at = datetime.now(timezone.utc).isoformat()
    rows = [
        {"run_at": run_at, "model": m, "accuracy": float(acc), "n": int(n)}
        for m, acc in results.items()
    ]
    errors = bq.insert_rows_json(table_id, rows)
    if errors:
        print(f"BigQuery insert errors: {errors}")
    else:
        print(f"Logged {len(rows)} row(s) to {table_id}")
```

**3d.** At the very end of the `if __name__ == "__main__":` block (after the `=== SUMMARY ===` print), add one line — **indented to line up with the lines above it**:
```python
    log_to_bigquery(results, len(texts))
```

### Step 4 — Run it

```bash
cd ~/dev/hate-speech-adv
python eval/run_eval.py gemini-3.1-flash-lite
```

After the confusion matrix and summary, you should see:
```
Logged 1 row(s) to hate-speech-adv.moderation.eval_runs
```
Run it with two models and it logs two rows — one per model.

### Step 5 — Verify in the console

Open BigQuery with the project pinned in the URL (the reliable workaround when the picker defaults elsewhere):

`https://console.cloud.google.com/bigquery?project=hate-speech-adv`

Left panel: **moderation → eval_runs**. Click the table → **Preview** tab, or run:
```sql
SELECT * FROM `hate-speech-adv.moderation.eval_runs`
ORDER BY run_at DESC
```

> **Heads-up:** `insert_rows_json` uses BigQuery's *streaming buffer*, so rows usually appear within seconds but occasionally take up to a minute. If Preview looks empty right after running, wait a moment and re-query.

### The drift query (the actual payoff)

Accuracy per model over time — this is what makes the table worth keeping:
```sql
SELECT
  model,
  DATE(run_at) AS day,
  MAX(accuracy) AS accuracy
FROM `hate-speech-adv.moderation.eval_runs`
GROUP BY model, day
ORDER BY day DESC, model
```
Run it after any model-version bump. A dropping accuracy for the same model = drift you caught before it hit production.

> **Why this reads well:** even the simple table version demonstrates the MLOps instinct. Charting this drift query in Looker Studio (next to your Phase 7 dashboard) turns it into a visual for the portfolio.

**✅ Checkpoint 9:** `run_eval.py` prints an accuracy figure and a confusion matrix for at least one model — ideally a small comparison table showing which model wins and by how much. Screenshot that table; it goes straight into the Phase 10 README.

---

# Phase 10 — Package it as a portfolio piece (~2 hrs)

**Goal:** Everything you built is only valuable in a job hunt if someone can *see* it and understand it in two minutes. This phase is writing, not building — but it's what converts the project into interview currency.

## Step 10.1 — Write the README

Your repo is already on GitHub (Phase 8). Now give it a README that a recruiter or engineer can skim. Create **`README.md`** at the repo root covering, in this order:

1. **One-line description** + the architecture diagram (reuse the ASCII diagram from the top of this guide, or redraw it).
2. **Why each service** — one sentence each: Terraform (reproducible infra), Pub/Sub (decoupled, spike-tolerant ingestion), Cloud Run (scale-to-zero serverless), Vertex AI (managed Gemini with proper IAM), BigQuery + dbt (analytics layer), Looker Studio + Cloud Monitoring (observability), GitHub Actions + WIF (keyless CI/CD).
3. **The eval results table** from Phase 9 — this is your proof the thing works.
4. **Monthly cost** — honestly ~€0 idle on the free tier, single-digit € only if Cloud SQL / a min-instance service is left running.
5. **What I'd do next** — a short, honest list (the two Phase 7 hardening items: `try/except` + dead-letter topic; a human-review UI; auto-retraining on reviewed data; the dedicated `ingest` service account from the Phase 3 note). Naming your own project's rough edges signals maturity more than pretending it's perfect.

> **Include the manual, out-of-Terraform bits** you noted along the way — the two Compute Engine default SA grants from Phase 3, and the WIF/deployer setup from Phase 8. A "known manual steps" subsection is honest and shows you know exactly what isn't yet codified.

## Step 10.2 — Make sure the repo is legible

- **Folder layout** is self-explanatory: `infra/` (Terraform), `ingest/`, `worker/`, `eval/`, `.github/workflows/`, and the dbt project if you did Phase 6.
- **`.gitignore` is doing its job** — double-check no `*.tfstate` or `*-key.json` ever got committed (`git log --all --full-history -- '*.tfstate'` should return nothing). If one slipped in, it's in history and you should rotate that secret.
- A short **architecture diagram image** (even a screenshot of the console topology, or the ASCII one) at the top of the README earns disproportionate attention.

## Step 10.3 — The interview sentence

Rehearse the one-liner until it's natural — it hits IaC, event-driven design, GenAI, data engineering, MLOps, and CI/CD in a single breath:

> "I built an event-driven moderation pipeline on GCP — Pub/Sub ingestion, Vertex AI classification with embeddings-based duplicate detection, BigQuery + dbt for the data layer, Looker Studio and Cloud Monitoring for observability — all provisioned with Terraform and deployed via GitHub Actions with keyless auth, and I benchmarked multiple models against a labeled eval set."

**✅ Checkpoint 10:** A stranger can open your GitHub repo and, from the README alone, understand what you built, why, how it's deployed, and how well it performs.

---

## Final cost hygiene

When you're not actively demoing, tear down the two things that cost money while idle:
- **Cloud SQL** (Phase 4) and any `--min-instances=1` service (n8n) — the only idle costs.
- `terraform destroy` removes everything and rebuilds in minutes; or scale just the n8n service to zero: `gcloud run services update n8n --region europe-west3 --min-instances 0`.

The code workers, ingestion API, and Pub/Sub all scale to zero on their own — they cost nothing sitting idle.
