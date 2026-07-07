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