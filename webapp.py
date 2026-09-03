"""HTTP API и раздача мини-приложения Telegram для бота учёта расходов.

Запускается отдельным процессом рядом с main.py, использует те же файлы
expenses.json и limits.json. Наружу выходит через HTTPS-прокси (Caddy).
"""
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl

import pytz
import requests
from aiohttp import web

BASE_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

logger = logging.getLogger("webapp")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAD_API_KEY = os.getenv("CHAD_API_KEY") or "chad-9814409421bc4afda8cb736d7d3403f4de4qu6jf"
ALLOWED_USER_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USER_IDS", ""))}
DEV_MODE = os.getenv("WEBAPP_DEV", "") == "1"          # без проверки подписи Telegram (только локально)
HOST = os.getenv("WEBAPP_HOST", "127.0.0.1")
PORT = int(os.getenv("WEBAPP_PORT", "8080"))
TZ = pytz.timezone(os.getenv("TIMEZONE", "Asia/Krasnoyarsk"))

EXPENSES_FILE = BASE_DIR / "expenses.json"
LIMITS_FILE = BASE_DIR / "limits.json"
STATIC_DIR = BASE_DIR / "webapp"

CATEGORIES = ["Еда", "Транспорт", "Одежда", "Развлечения", "Другое"]
DATE_FMT = "%d.%m.%Y"
CHAD_API_URL = "https://ask.chadgpt.ru/api/public/gpt-4o-mini"


# ---------------------------------------------------------------------------
# Файлы (формат совместим с main.py: список [название, сумма, категория, "ДД.ММ.ГГГГ"])
# ---------------------------------------------------------------------------
def _read_json(path: Path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def _write_json_atomic(path: Path, data):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_expenses():
    return _read_json(EXPENSES_FILE, [])


def save_expenses(rows):
    _write_json_atomic(EXPENSES_FILE, rows)


def load_limits():
    return _read_json(LIMITS_FILE, {"daily": None, "weekly": None, "monthly": None})


def save_limits(limits):
    _write_json_atomic(LIMITS_FILE, limits)


def detect_category(name: str) -> str:
    if not CHAD_API_KEY:
        return "Другое"
    try:
        r = requests.post(
            CHAD_API_URL,
            json={
                "message": f"Определи категорию для траты '{name}' одним словом. Только: {', '.join(CATEGORIES)}.",
                "api_key": CHAD_API_KEY,
            },
            timeout=20,
        )
        resp = r.json()
        if r.ok and resp.get("is_success"):
            word = resp["response"].strip().split()[0].strip(".,!").capitalize()
            return word if word in CATEGORIES else "Другое"
    except Exception as e:  # noqa: BLE001
        logger.warning("ChadGPT: %s", e)
    return "Другое"


# ---------------------------------------------------------------------------
# Проверка подписи Telegram Mini App
# https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
# ---------------------------------------------------------------------------
def validate_init_data(init_data: str):
    """Возвращает dict user или None, если подпись неверна."""
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if datetime.datetime.now().timestamp() - auth_date > 60 * 60 * 24:  # initData старше суток
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return {}


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    if DEV_MODE:
        request["user"] = {"id": 0, "first_name": "Dev"}
        return await handler(request)
    user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if user is None:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Откройте приложение через Telegram"}),
                                   content_type="application/json")
    if ALLOWED_USER_IDS and user.get("id") not in ALLOWED_USER_IDS:
        raise web.HTTPForbidden(text=json.dumps({"error": "Доступ только для владельца бота"}),
                                content_type="application/json")
    request["user"] = user
    return await handler(request)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def today() -> datetime.date:
    return datetime.datetime.now(TZ).date()


def row_to_item(idx: int, row) -> dict:
    name, amount, category, date = row[0], row[1], row[2], row[3]
    try:
        iso = datetime.datetime.strptime(date, DATE_FMT).date().isoformat()
    except ValueError:
        iso = None
    return {"id": idx, "name": name, "amount": int(amount), "category": category or "Другое",
            "date": date, "iso": iso}


def parse_date(value) -> str:
    """Принимает 'ГГГГ-ММ-ДД' или 'ДД.ММ.ГГГГ', возвращает 'ДД.ММ.ГГГГ'."""
    if not value:
        return today().strftime(DATE_FMT)
    for fmt in ("%Y-%m-%d", DATE_FMT):
        try:
            return datetime.datetime.strptime(str(value), fmt).strftime(DATE_FMT)
        except ValueError:
            continue
    raise web.HTTPBadRequest(text=json.dumps({"error": "Неверная дата"}), content_type="application/json")


async def api_state(request: web.Request):
    rows = load_expenses()
    items = [row_to_item(i, r) for i, r in enumerate(rows) if isinstance(r, list) and len(r) >= 4]
    items.sort(key=lambda x: (x["iso"] or "", x["id"]), reverse=True)
    return web.json_response({
        "today": today().isoformat(),
        "categories": CATEGORIES,
        "limits": load_limits(),
        "expenses": items,
        "user": request["user"],
    })


async def api_add(request: web.Request):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise web.HTTPBadRequest(text=json.dumps({"error": "Укажите название"}), content_type="application/json")
    try:
        amount = int(round(float(str(body.get("amount", "")).replace(",", ".").replace(" ", ""))))
        if amount <= 0:
            raise ValueError
    except ValueError:
        raise web.HTTPBadRequest(text=json.dumps({"error": "Сумма должна быть числом больше нуля"}),
                                 content_type="application/json")
    category = body.get("category") or ""
    if category not in CATEGORIES:
        category = await request.loop.run_in_executor(None, detect_category, name)
    date = parse_date(body.get("date"))
    rows = load_expenses()
    row = [name[:1].upper() + name[1:], amount, category, date]
    rows.append(row)
    save_expenses(rows)
    return web.json_response(row_to_item(len(rows) - 1, row), status=201)


def _find_row(rows, idx: int):
    if idx < 0 or idx >= len(rows):
        raise web.HTTPNotFound(text=json.dumps({"error": "Запись не найдена"}), content_type="application/json")
    return rows[idx]


async def api_update(request: web.Request):
    idx = int(request.match_info["id"])
    body = await request.json()
    rows = load_expenses()
    row = _find_row(rows, idx)
    if "name" in body and str(body["name"]).strip():
        row[0] = str(body["name"]).strip()
    if "amount" in body:
        try:
            row[1] = int(round(float(str(body["amount"]).replace(",", "."))))
        except ValueError:
            raise web.HTTPBadRequest(text=json.dumps({"error": "Неверная сумма"}), content_type="application/json")
    if body.get("category") in CATEGORIES:
        row[2] = body["category"]
    if "date" in body:
        row[3] = parse_date(body["date"])
    save_expenses(rows)
    return web.json_response(row_to_item(idx, row))


async def api_delete(request: web.Request):
    idx = int(request.match_info["id"])
    rows = load_expenses()
    _find_row(rows, idx)
    rows.pop(idx)
    save_expenses(rows)
    return web.json_response({"ok": True})


async def api_limits(request: web.Request):
    body = await request.json()
    limits = load_limits()
    for key in ("daily", "weekly", "monthly"):
        if key in body:
            val = body[key]
            if val in (None, "", 0, "0"):
                limits[key] = None
            else:
                try:
                    limits[key] = int(float(str(val).replace(" ", "")))
                except ValueError:
                    raise web.HTTPBadRequest(text=json.dumps({"error": f"Неверный лимит {key}"}),
                                             content_type="application/json")
    save_limits(limits)
    return web.json_response(limits)


async def index(request: web.Request):
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request: web.Request):
    return web.json_response({"ok": True})


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/expenses", api_add)
    app.router.add_put(r"/api/expenses/{id:\d+}", api_update)
    app.router.add_delete(r"/api/expenses/{id:\d+}", api_delete)
    app.router.add_put("/api/limits", api_limits)
    app.router.add_static("/static", STATIC_DIR, show_index=False)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TELEGRAM_TOKEN and not DEV_MODE:
        raise SystemExit("TELEGRAM_TOKEN не задан: он нужен для проверки подписи Telegram Mini App")
    logger.info("Mini App API на http://%s:%s (dev=%s)", HOST, PORT, DEV_MODE)
    web.run_app(make_app(), host=HOST, port=PORT, print=None)
