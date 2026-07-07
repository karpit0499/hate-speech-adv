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

    # --- embedding for near-duplicate detection (Phase 5) ---
    # 768 dims is a recommended smaller size: cheaper to store, minimal quality loss.
    emb = genai_client.models.embed_content(
        model="gemini-embedding-001",
        contents=text,
        config=types.EmbedContentConfig(output_dimensionality=768),
    )
    embedding_vector = emb.embeddings[0].values

    row = {
        "message_id":    msg.get("messageId", str(uuid.uuid4())),
        "input_text":    text,
        "label":         label,
        "confidence":    confidence,
        "target_groups": json.dumps(target_groups),   # column is STRING
        "rationale":     rationale,
        "embedding":     None,
        "embedding_vector": embedding_vector,
        "model_version": MODEL,
        "created_at":    datetime.now(timezone.utc).isoformat(),
    }

    errors = bq.insert_rows_json(TABLE, [row])
    if errors:
        print("BQ insert errors:", errors)
        return ("insert failed", 500)   # non-2xx → Pub/Sub retries

    return ("", 204)   # 2xx acks the message
