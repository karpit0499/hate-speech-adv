from google import genai
from google.genai import types

client = genai.Client(vertexai=True, project="hate-speech-adv", location="global")

resp = client.models.generate_content(
    model="gemini-3.1-flash-lite",
    contents="People of that religion are all criminals and should be banned.",
    config=types.GenerateContentConfig(
        system_instruction="<paste your classification prompt here, minus the USER TEXT line>",
        temperature=0,
        response_mime_type="application/json",
    ),
)
print(resp.text)