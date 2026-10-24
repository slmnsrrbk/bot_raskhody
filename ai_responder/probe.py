"""Проверяет, какие модели сейчас отвечают через подключённый API (печатает только коды ответов)."""
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv(os.getenv("ENV_FILE", ".env"))
base = os.getenv("AI_BASE_URL", "").rstrip("/")
key = os.environ["AI_API_KEY"]
for model in sys.argv[1:]:
    try:
        r = httpx.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, timeout=60,
                       json={"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "привет"}]})
        detail = "" if r.status_code == 200 else " " + r.text[:150].replace("\n", " ")
        print(f"{model}: {r.status_code}{detail}")
    except Exception as e:
        print(f"{model}: {e!r}")
