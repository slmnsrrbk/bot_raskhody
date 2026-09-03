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
    "Продукты": ["продукт", "хлеб", "молок", "мяс", "рыб", "фрукт", "овощ", "яйц", "сыр", "крупа", "макарон", "сахар",
                 "пятёроч", "пятероч", "магнит", "перекрёст", "перекрест", "вкусвилл", "ашан", "лента", "дикси", "окей",
                 "метро кэш", "самокат", "лавка", "сладк", "конфет", "шоколад", "мороженое", "вода", "сок", "пиво", "вино",
                 "супермаркет", "бакале", "гастроном"],
    "Еда": ["кофе", "чай", "обед", "ужин", "завтрак", "ланч", "еда", "пицц", "суши", "бургер", "шаурм", "ресторан", "кафе",
            "столов", "доставк", "яндекс еда", "деливери", "фастфуд", "макдон", "вкусно и точка", "kfc", "перекус", "бизнес-ланч"],
    "Транспорт": ["такси", "метро", "автобус", "трамвай", "троллейбус", "маршрутк", "самокат аренда", "поезд", "электричк",
                  "проезд", "яндекс го", "uber", "убер", "ржд", "авиабилет", "самолёт", "самолет", "каршеринг", "драйв", "делимобиль"],
    "Машина": ["бензин", "заправк", "топлив", "азс", "парковк", "мойк", "шиномонтаж", "шины", "резин", "осаго", "каско",
               "страховк авто", "то авто", "масло", "автосервис", "ремонт авто", "штраф гибдд", "антифриз", "омывайк"],
    "Жильё": ["аренда", "квартплат", "коммунал", "жкх", "ипотек", "свет", "электричеств", "газ", "отоплен", "водоснабж",
              "интернет", "домофон", "консьерж", "капремонт", "мебель", "ремонт квартир", "хозтовар", "уборк", "быт хим"],
    "Телефон": ["телефон", "связь", "мтс", "билайн", "мегафон", "теле2", "йота", "yota", "тариф", "сотов", "сим", "sim", "роуминг"],
    "Здоровье": ["аптек", "лекарств", "таблетк", "врач", "стоматолог", "зубн", "анализ", "клиник", "больниц", "поликлиник",
                 "медицин", "витамин", "окулист", "линз", "очки", "массаж", "психолог", "терапевт", "медосмотр"],
    "Одежда": ["футболк", "джинс", "кроссовк", "обув", "ботинк", "куртк", "пальто", "рубашк", "платье", "носк", "шапк",
               "кепк", "одежд", "свитер", "худи", "толстовк", "юбк", "брюк", "штан", "белье", "бельё", "костюм", "zara",
               "h&m", "uniqlo", "wildberries", "вайлдберриз", "ламода", "lamoda", "ozon одежда"],
    "Развлечения": ["кино", "театр", "концерт", "клуб", "бар", "игр", "playstation", "steam", "подписк", "netflix",
                    "кинопоиск", "иви", "ivi", "музык", "spotify", "яндекс музык", "боулинг", "бильярд", "квест",
                    "парк аттракц", "музей", "выставк", "книг", "билет в кино", "развлеч", "отдых", "караоке", "кальян"],
    "Работа": ["работ", "канцеляр", "командировк", "офис", "коворкинг", "курс", "обучен", "хостинг", "домен", "сервер", "vps",
               "лицензи", "софт", "программ", "ноутбук", "монитор"],
    "Благотворительность": ["благотвор", "пожертвован", "донат", "милостын", "храм", "церков", "мечет", "фонд", "помощь"],
    "Непредвиденные": ["непредвид", "внезапн", "штраф", "потерял", "сломал", "починить", "починк", "ремонт котл", "котел", "котёл",
                       "сантехник", "электрик", "эвакуатор", "залил", "ключ потер", "замок"],
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
    + ", ".join(CATEGORIES) + ". Продукты — покупки в магазинах для дома; Еда — кафе, доставка, перекусы; "
    "Транспорт — такси и общественный транспорт; Машина — расходы на свой автомобиль. Без пояснений и знаков препинания."
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


def classify_many(names):
    """Категории для списка названий одним запросом (словарь и кэш — без запроса)."""
    result = [None] * len(names)
    todo = []
    for i, n in enumerate(names):
        key = normalize(n)
        result[i] = by_keywords(key) or storage.cache_get(key)
        if not result[i]:
            todo.append(i)
    if todo and POLZA_API_KEY:
        prompt = ("Для каждой строки укажи категорию из списка: " + ", ".join(CATEGORIES) +
                  ". Ответь строго построчно в формате «номер. категория», без пояснений.\n" +
                  "\n".join(f"{k + 1}. {names[i]}" for k, i in enumerate(todo)))
        answer = _polza([{"role": "user", "content": prompt}], max_tokens=20 * len(todo) + 20)
        if answer:
            for line in answer.splitlines():
                m = re.match(r"\s*(\d+)[.):\s-]+\s*(.+)", line)
                if not m:
                    continue
                k = int(m.group(1)) - 1
                cat = _pick_category(m.group(2))
                if 0 <= k < len(todo) and cat:
                    result[todo[k]] = cat
                    storage.cache_set(normalize(names[todo[k]]), cat)
    return [c or DEFAULT for c in result]


EXPENSES_PROMPT = (
    "Извлеки из сообщения пользователя список трат. Верни ТОЛЬКО JSON-массив вида "
    '[{"name": "короткое название", "amount": число в рублях, "date": "ГГГГ-ММ-ДД"}]. '
    "Сегодня {today}. «Вчера» — это {yesterday}. Строка с датой относится ко всем тратам ниже неё, пока не встретится другая дата. "
    "Если дата не указана — {today}. Если трат нет, верни []."
)


def parse_expenses_text(text: str, today):
    """Запасной разбор свободного текста моделью. -> список {name, amount, date(ISO)} или None."""
    if not POLZA_API_KEY or not text.strip():
        return None
    yesterday = (today - __import__("datetime").timedelta(days=1)).isoformat()
    answer = _polza([{"role": "system", "content": EXPENSES_PROMPT.format(today=today.isoformat(), yesterday=yesterday)},
                     {"role": "user", "content": text[:4000]}], max_tokens=1200)
    data = _extract_json_any(answer or "")
    if not isinstance(data, list):
        return None
    out = []
    for it in data[:60]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        try:
            amount = int(round(float(str(it.get("amount", "")).replace(",", ".").replace(" ", ""))))
        except ValueError:
            continue
        if not name or amount <= 0:
            continue
        try:
            date = storage.to_iso(it.get("date") or today)
        except ValueError:
            date = today.isoformat()
        out.append({"name": name[:100], "amount": amount, "date": date})
    return out


def _extract_json_any(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\[.*\]", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    return None


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
