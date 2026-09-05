from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())
import os
from groq import Groq

client = Groq(api_key=os.getenv("GROQ_API_KEY"))
resp = client.chat.completions.create(
    model="openai/gpt-oss-120b",
    messages=[
        {"role": "system", "content": "You are a financial AI. Reply ONLY with valid JSON."},
        {"role": "user", "content": 'Return this JSON exactly: {"status": "ok", "model": "working"}'}
    ],
    temperature=0.0,
    response_format={"type": "json_object"},
)
print("SUCCESS:", resp.choices[0].message.content)
