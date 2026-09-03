"""HTTP API и раздача мини-приложения Telegram для бота учёта расходов.

Отдельный процесс рядом с main.py, общая база data.db (storage.py). Каждый запрос
подписан Telegram (initData), данные отдаются только их владельцу. Наружу — через nginx/HTTPS.
"""
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import parse_qsl

import pytz
import requests
from aiohttp import web

import storage

BASE_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

logger = logging.getLogger("webapp")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAD_API_KEY = os.getenv("CHAD_API_KEY", "")
ALLOWED_USER_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USER_IDS", ""))}
DEV_MODE = os.getenv("WEBAPP_DEV", "") == "1"          # без проверки подписи Telegram (только локально)
HOST = os.getenv("WEBAPP_HOST", "127.0.0.1")
PORT = int(os.getenv("WEBAPP_PORT", "8080"))
TZ = pytz.timezone(os.getenv("TIMEZONE", "Asia/Krasnoyarsk"))

STATIC_DIR = BASE_DIR / "webapp"
CATEGORIES = storage.CATEGORIES
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "120"))   # запросов в минуту на пользователя
CHAD_API_URL = "https://ask.chadgpt.ru/api/public/gpt-4o-mini"


# ---------------------------------------------------------------------------
# Категория через ChadGPT
# ---------------------------------------------------------------------------
def detect_category(name: str) -> str:
    if not CHAD_API_KEY:
        return "Другое"
    try:
        r = requests.post(
            CHAD_API_URL,
            json={"message": f"Определи категорию для траты '{name}' одним словом. Только: {', '.join(CATEGORIES)}.",
                  "api_key": CHAD_API_KEY},
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


def _error(status_cls, message):
    return status_cls(text=json.dumps({"error": message}, ensure_ascii=False), content_type="application/json")


_buckets: dict = {}   # user_id -> [timestamps] за последнюю минуту


def rate_limited(user_id: int) -> bool:
    import time
    now = time.monotonic()
    hits = [t for t in _buckets.get(user_id, []) if now - t < 60]
    hits.append(now)
    _buckets[user_id] = hits
    if len(_buckets) > 10000:  # не даём словарю расти бесконечно
        _buckets.clear()
    return len(hits) > RATE_LIMIT


@web.middleware
async def security_headers(request: web.Request, handler):
    resp = await handler(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "ALLOW-FROM https://web.telegram.org"
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    resp.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
    return resp


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    if DEV_MODE:
        user = {"id": 0, "first_name": "Dev"}
    else:
        user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
        if user is None or not isinstance(user.get("id"), int):
            raise _error(web.HTTPUnauthorized, "Откройте приложение через Telegram")
        if ALLOWED_USER_IDS and user["id"] not in ALLOWED_USER_IDS:
            raise _error(web.HTTPForbidden, "Доступ закрыт")
    if rate_limited(user["id"]):
        raise _error(web.HTTPTooManyRequests, "Слишком много запросов, подождите минуту")
    storage.upsert_user(user["id"], user.get("first_name", ""), user.get("username", ""))
    request["user"] = user
    request["user_id"] = user["id"]
    return await handler(request)


# ---------------------------------------------------------------------------
# API — все данные только текущего пользователя
# ---------------------------------------------------------------------------
def today() -> datetime.date:
    return datetime.datetime.now(TZ).date()


def parse_amount(value) -> int:
    try:
        amount = int(round(float(str(value).replace(",", ".").replace(" ", "").replace("\u00a0", ""))))
    except ValueError:
        raise _error(web.HTTPBadRequest, "Сумма должна быть числом")
    if amount <= 0 or amount > 100_000_000:
        raise _error(web.HTTPBadRequest, "Сумма должна быть больше нуля")
    return amount


def parse_date(value) -> str:
    if not value:
        return today().isoformat()
    try:
        return storage.to_iso(value)
    except ValueError:
        raise _error(web.HTTPBadRequest, "Неверная дата")


async def api_state(request: web.Request):
    uid = request["user_id"]
    return web.json_response({
        "today": today().isoformat(),
        "categories": CATEGORIES,
        "limits": storage.get_limits(uid),
        "expenses": storage.list_expenses(uid),
        "user": {"id": uid, "first_name": request["user"].get("first_name", "")},
    })


async def api_add(request: web.Request):
    uid = request["user_id"]
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise _error(web.HTTPBadRequest, "Укажите название")
    amount = parse_amount(body.get("amount"))
    date = parse_date(body.get("date"))
    category = body.get("category") or ""
    if category not in CATEGORIES:
        category = await request.loop.run_in_executor(None, detect_category, name)
    return web.json_response(storage.add_expense(uid, name, amount, category, date), status=201)


async def api_update(request: web.Request):
    uid = request["user_id"]
    expense_id = int(request.match_info["id"])
    if not storage.get_expense(uid, expense_id):
        raise _error(web.HTTPNotFound, "Запись не найдена")
    body = await request.json()
    fields = {}
    if "name" in body:
        fields["name"] = str(body["name"])
    if "amount" in body:
        fields["amount"] = parse_amount(body["amount"])
    if "category" in body:
        fields["category"] = body["category"]
    if "date" in body:
        fields["date"] = parse_date(body["date"])
    return web.json_response(storage.update_expense(uid, expense_id, **fields))


async def api_delete(request: web.Request):
    uid = request["user_id"]
    item = storage.delete_expense(uid, int(request.match_info["id"]))
    if not item:
        raise _error(web.HTTPNotFound, "Запись не найдена")
    return web.json_response({"ok": True, "deleted": item})


async def api_limits(request: web.Request):
    uid = request["user_id"]
    body = await request.json()
    values = {}
    for key in ("daily", "weekly", "monthly"):
        if key in body:
            val = body[key]
            if val in (None, "", 0, "0"):
                values[key] = None
            else:
                try:
                    values[key] = int(float(str(val).replace(" ", "").replace("\u00a0", "")))
                except ValueError:
                    raise _error(web.HTTPBadRequest, "Лимит должен быть числом")
    return web.json_response(storage.set_limits(uid, **values))


async def index(request: web.Request):
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request: web.Request):
    return web.json_response({"ok": True})


def make_app() -> web.Application:
    app = web.Application(middlewares=[security_headers, auth_middleware])
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
    storage.init_db()
    logger.info("Mini App API на http://%s:%s (dev=%s)", HOST, PORT, DEV_MODE)
    web.run_app(make_app(), host=HOST, port=PORT, print=None)
