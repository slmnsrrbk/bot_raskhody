"""Самопроверка на сервере: доходят ли ключи и работает ли AI. Запуск: venv/bin/python deploy/selftest.py"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import ai  # noqa: E402
import receipt  # noqa: E402

def show(name, ok, extra=""):
    print(f"{'OK ' if ok else 'ERR'} {name} {extra}")

show("TELEGRAM_TOKEN задан", bool(os.getenv("TELEGRAM_TOKEN")))
show("POLZA_API_KEY задан", bool(ai.POLZA_API_KEY), f"(модель {ai.POLZA_MODEL}, vision {ai.POLZA_VISION_MODEL})")
show("PROVERKACHEKA_TOKEN задан", bool(receipt.PROVERKACHEKA_TOKEN), "" if receipt.PROVERKACHEKA_TOKEN else "(без него по QR берётся только сумма)")
show("WEBAPP_URL", bool(os.getenv("WEBAPP_URL")), os.getenv("WEBAPP_URL", ""))
try:
    import zxingcpp  # noqa: F401
    from PIL import Image  # noqa: F401
    show("QR-декодер (zxing-cpp, pillow)", True)
except Exception as e:  # noqa: BLE001
    show("QR-декодер", False, str(e))
t = time.time()
try:
    answer = ai._polza_chat([{"role": "user", "content": "Ответь одним словом: столица Франции?"}], ai.POLZA_MODEL, max_tokens=5)
    show("Polza chat", bool(answer), f"-> {answer!r} за {time.time() - t:.1f} с")
except Exception as e:  # noqa: BLE001
    show("Polza chat", False, f"{type(e).__name__}: {e}")
t = time.time()
try:
    cat = ai._pick_category(ai._polza([{"role": "system", "content": ai.SYSTEM_PROMPT}, {"role": "user", "content": "Яндекс Драйв"}]) or "")
    show("Категория через AI («Яндекс Драйв»)", cat == "Транспорт", f"-> {cat} за {time.time() - t:.1f} с")
except Exception as e:  # noqa: BLE001
    show("Категория через AI", False, f"{type(e).__name__}: {e}")
