"""Определение категории траты и советы: словарь → кэш в базе → Polza AI (OpenAI-совместимый API).

Polza AI: https://polza.ai/docs — base URL https://api.polza.ai/api/v1, Bearer-ключ, /chat/completions.
"""
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


def _polza_chat(messages, model, max_tokens=8, temperature=0):
    r = _session.post(
        f"{POLZA_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {POLZA_API_KEY}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
        timeout=TIMEOUT,
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
