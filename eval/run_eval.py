import csv, json, sys
from google import genai
from google.genai import types
from sklearn.metrics import accuracy_score, confusion_matrix, classification_report
from tabulate import tabulate
from datetime import datetime, timezone
from google.cloud import bigquery

PROJECT = "hate-speech-adv"
LABELS  = ["hate_speech", "offensive", "neither"]
BQ_DATASET = "moderation"
BQ_TABLE   = "eval_runs"

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

if __name__ == "__main__":
    texts, gold = load("eval/labeled.csv")
    models = sys.argv[1:] or ["gemini-3.1-flash-lite"]
    results = {m: evaluate(m, texts, gold) for m in models}
    print("\n=== SUMMARY ===")
    log_to_bigquery(results, len(texts))
    print(tabulate([[m, f"{a:.3f}"] for m, a in results.items()],
                   headers=["model", "accuracy"], tablefmt="github"))