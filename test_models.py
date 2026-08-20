from google import genai
import os
import sys
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise SystemExit("GEMINI_API_KEY is missing. Add it to .env before running this script.")
client = genai.Client(api_key=api_key)

try:
    for model in client.models.list():
        print(model.name)
except Exception as e:
    print(f"Error listing models: {e}")
