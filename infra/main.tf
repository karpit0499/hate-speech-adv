terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.0" }
    random = { source = "hashicorp/random" }
  }
}

provider "google" {
  project = "hate-speech-adv"
  region  = "europe-west3"   # Frankfurt
}

# Look up project number + the deployed worker service automatically
# (so we don't have to hardcode the URL or project number)
data "google_project" "current" {}

data "google_cloud_run_v2_service" "worker" {
  name     = "worker"
  location = "europe-west3"
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

# Pub/Sub topic
resource "google_pubsub_topic" "messages" { name = "incoming-messages" }

# Pub/Sub subscription — now PUSH to the Cloud Run worker
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
    { name = "embedding_vector", type = "FLOAT", mode = "REPEATED" },
    { name = "model_version", type = "STRING" },
    { name = "created_at",    type = "TIMESTAMP" },
  ])
}

# A service account for the worker to use (least-privilege identity)
resource "google_service_account" "worker" {
  account_id   = "classifier-worker"
  display_name = "Classifier Worker"
}

resource "google_project_iam_member" "worker_roles" {
  for_each = toset([
    "roles/aiplatform.user",      # use Vertex AI (call Gemini)
    "roles/bigquery.dataEditor",  # write result rows
    "roles/pubsub.subscriber",    # read from the queue
  ])
  project = "hate-speech-adv"
  role    = each.value
  member  = "serviceAccount:${google_service_account.worker.email}"
}

# ---- Push-subscription auth (Step 3.5) ----

# 1) Let the worker SA invoke the private "worker" Cloud Run service.
#    Pub/Sub will present an OIDC token as this SA, so this is what
#    authorizes the incoming push request.
resource "google_cloud_run_v2_service_iam_member" "worker_invoker" {
  name     = "worker"
  location = "europe-west3"
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.worker.email}"
}

# 2) Let the Pub/Sub service agent mint OIDC tokens as the worker SA.
#    Scoped to just this one SA (not project-wide) for least privilege.
resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

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

# REQUIRED: let that SA actually connect to Cloud SQL.
# Without this, n8n starts but can't reach Postgres — same class of
# permission failure as the Pub/Sub 500 in Phase 3.
resource "google_project_iam_member" "n8n_cloudsql_client" {
  project = "hate-speech-adv"
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

# Second Pub/Sub subscription — fans every published message out to the
# hosted n8n worker in parallel with worker-sub (the Python worker).
# n8n runs with --allow-unauthenticated, so no oidc_token block is needed.
resource "google_pubsub_subscription" "worker_n8n" {
  name                 = "worker-n8n-sub"
  topic                = google_pubsub_topic.messages.name
  ack_deadline_seconds = 60

  push_config {
    push_endpoint = "https://n8n-1008979816387.europe-west3.run.app/webhook/classify"
  }
}