"""Определение категории траты и советы: словарь → кэш в базе → Polza AI (OpenAI-совместимый API).

Polza AI: https://polza.ai/docs — base URL https://api.polza.ai/api/v1, Bearer-ключ, /chat/completions.
"""
import base64
import json
import logging
import os
import re

import requests

import storage

logger = logging.getLogger("ai")

POLZA_API_KEY = os.getenv("POLZA_API_KEY", "")
POLZA_BASE_URL = os.getenv("POLZA_BASE_URL", "https://api.polza.ai/api/v1")
POLZA_MODEL = os.getenv("POLZA_MODEL", "google/gemini-2.5-flash-lite")
POLZA_FALLBACK_MODEL = os.getenv("POLZA_FALLBACK_MODEL", "openai/gpt-4.1-nano")
POLZA_VISION_MODEL = os.getenv("POLZA_VISION_MODEL", "google/gemini-2.5-flash-lite")
POLZA_VISION_FALLBACK_MODEL = os.getenv("POLZA_VISION_FALLBACK_MODEL", "openai/gpt-4.1-mini")
CHAD_API_KEY = os.getenv("CHAD_API_KEY", "")
CHAD_API_URL = "https://ask.chadgpt.ru/api/public/gpt-4o-mini"
TIMEOUT = 20

CATEGORIES = storage.CATEGORIES
DEFAULT = "Другое"

# Быстрый словарь: без сети и без затрат. Ключ — начало слова в названии.
KEYWORDS = {
    "Еда": ["кофе", "чай", "обед", "ужин", "завтрак", "ланч", "продукт", "еда", "пицц", "суши", "бургер", "шаурм",
            "хлеб", "молок", "мяс", "рыб", "фрукт", "овощ", "пятёроч", "пятероч", "магнит", "перекрёст", "перекрест",
            "вкусвилл", "ашан", "лента", "ресторан", "кафе", "столов", "доставк", "яндекс еда", "самокат", "сладк",
            "конфет", "шоколад", "мороженое", "вода", "сок", "пиво", "вино"],
    "Транспорт": ["такси", "метро", "автобус", "трамвай", "троллейбус", "маршрутк", "бензин", "заправк", "топлив",
                  "парковк", "каршеринг", "самокат аренда", "поезд", "электричк", "билет на поезд", "проезд",
                  "яндекс го", "uber", "убер", "ржд", "авиабилет", "самолёт", "самолет"],
    "Одежда": ["футболк", "джинс", "кроссовк", "обув", "ботинк", "куртк", "пальто", "рубашк", "платье", "носк",
               "шапк", "кепк", "одежд", "свитер", "худи", "толстовк", "юбк", "брюк", "штан", "белье", "бельё",
               "костюм", "zara", "h&m", "uniqlo", "wildberries", "вайлдберриз", "ламода", "lamoda", "ozon одежда"],
    "Развлечения": ["кино", "театр", "концерт", "клуб", "бар", "игр", "playstation", "steam", "подписк", "netflix",
                    "кинопоиск", "иви", "ivi", "музык", "spotify", "яндекс музык", "боулинг", "бильярд", "квест",
                    "парк аттракц", "музей", "выставк", "книг", "билет в кино", "развлеч", "отдых", "караоке"],
}

_session = requests.Session()


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def by_keywords(name: str):
    n = normalize(name)
    for cat, words in KEYWORDS.items():
        for w in words:
            if re.search(r"(^|[^а-яa-z])" + re.escape(w), n):
                return cat
    return None


def _polza_chat(messages, model, max_tokens=8, temperature=0, timeout=TIMEOUT, **extra):
    r = _session.post(
        f"{POLZA_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {POLZA_API_KEY}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, **extra},
        timeout=timeout,
    )
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


def _polza(messages, max_tokens=8, temperature=0):
    if not POLZA_API_KEY:
        return None
    for model in (POLZA_MODEL, POLZA_FALLBACK_MODEL):
        try:
            answer = _polza_chat(messages, model, max_tokens, temperature)
            if answer:
                return answer
        except Exception as e:  # noqa: BLE001
            logger.warning("Polza (%s): %s", model, e)
    return None


def _chad(message: str):
    if not CHAD_API_KEY:
        return None
    try:
        r = _session.post(CHAD_API_URL, json={"message": message, "api_key": CHAD_API_KEY}, timeout=TIMEOUT)
        resp = r.json()
        if r.ok and resp.get("is_success"):
            return (resp.get("response") or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("ChadGPT: %s", e)
    return None


def _pick_category(answer: str):
    if not answer:
        return None
    word = answer.strip().split()[0].strip(".,!:\"'«»").capitalize()
    return word if word in CATEGORIES else None


SYSTEM_PROMPT = (
    "Ты классификатор личных трат. Ответь одним словом — категорией из списка: "
    + ", ".join(CATEGORIES) + ". Без пояснений и знаков препинания."
)


def detect_category(name: str) -> str:
    """Категория для названия траты. Порядок: словарь → кэш → Polza AI → ChadGPT → «Другое»."""
    key = normalize(name)
    if not key:
        return DEFAULT
    cat = by_keywords(key)
    if cat:
        return cat
    cached = storage.cache_get(key)
    if cached:
        return cached
    cat = _pick_category(_polza([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": name}]))
    if not cat:
        cat = _pick_category(_chad(f"Определи категорию для траты '{name}' одним словом. Только: {', '.join(CATEGORIES)}."))
    if cat:
        storage.cache_set(key, cat)
        return cat
    return DEFAULT


def generate_advice(report: str) -> str:
    prompt = f"{report}\n\nДай один короткий и практичный совет по оптимизации расходов (1–2 предложения, по-русски)."
    answer = _polza([{"role": "user", "content": prompt}], max_tokens=120, temperature=0.7)
    if answer is None:
        answer = _chad(prompt)
    return answer or ""


# ---------------------------------------------------------------------------
# Чек по фотографии
# ---------------------------------------------------------------------------
RECEIPT_PROMPT = (
    "На фото кассовый чек. Извлеки покупки и верни ТОЛЬКО JSON без пояснений вида:\n"
    '{"store": "название магазина или null", "date": "ГГГГ-ММ-ДД или null", "total": число или null, '
    '"items": [{"name": "короткое понятное название товара по-русски", "amount": сумма за позицию в рублях с учётом количества, '
    '"category": одна из: ' + ", ".join(CATEGORIES) + "}]}\n"
    "Названия сокращай до понятных (например «Молоко 2,5% 1л», а не код товара). Скидки учитывай в сумме позиции. "
    "Если это не чек — верни {\"items\": []}."
)


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    return None


def parse_receipt(image_bytes: bytes, mime: str = "image/jpeg"):
    """-> {"store", "date", "total", "items": [{"name", "amount", "category"}]} или None, если не распознано."""
    if not POLZA_API_KEY:
        return None
    data_url = f"data:{mime};base64," + base64.b64encode(image_bytes).decode()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": RECEIPT_PROMPT},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}]
    for model in (POLZA_VISION_MODEL, POLZA_VISION_FALLBACK_MODEL):
        try:
            answer = _polza_chat(messages, model, max_tokens=1500, temperature=0, timeout=60)
        except Exception as e:  # noqa: BLE001
            logger.warning("Polza vision (%s): %s", model, e)
            continue
        parsed = _extract_json(answer or "")
        if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            return _clean_receipt(parsed)
        logger.warning("Polza vision (%s): не удалось разобрать ответ: %.200s", model, answer)
    return None


def _clean_receipt(parsed: dict) -> dict:
    items = []
    for it in parsed.get("items", [])[:60]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        try:
            amount = int(round(float(str(it.get("amount", "")).replace(",", ".").replace(" ", ""))))
        except ValueError:
            continue
        if not name or amount <= 0:
            continue
        cat = str(it.get("category") or "").strip().capitalize()
        if cat not in CATEGORIES:
            cat = by_keywords(name) or DEFAULT
        items.append({"name": name[:100], "amount": amount, "category": cat})
    date = parsed.get("date")
    try:
        date = storage.to_iso(date) if date else None
    except ValueError:
        date = None
    try:
        total = int(round(float(parsed.get("total")))) if parsed.get("total") is not None else None
    except (TypeError, ValueError):
        total = None
    store = str(parsed.get("store") or "").strip()[:60] or None
    return {"store": store, "date": date, "total": total, "items": items}
