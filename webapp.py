"""HTTP API и раздача мини-приложения Telegram для бота учёта расходов.

Отдельный процесс рядом с main.py, общая база data.db (storage.py). Каждый запрос
подписан Telegram (initData), данные отдаются только их владельцу. Наружу — через nginx/HTTPS.
"""
import asyncio
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
from aiohttp import web

import aiohttp

import ai
import export
import receipt
import storage
import currencies
import rates

BASE_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

logger = logging.getLogger("webapp")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_USER_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USER_IDS", ""))}
OWNER_ID = storage.OWNER_ID                          # владелец: видит админку и управляет доступом


def is_admin(uid: int, username: str = None) -> bool:
    return storage.is_owner(uid, username)
DEV_MODE = os.getenv("WEBAPP_DEV", "") == "1"          # без проверки подписи Telegram (только локально)
HOST = os.getenv("WEBAPP_HOST", "127.0.0.1")
PORT = int(os.getenv("WEBAPP_PORT", "8080"))
TZ = pytz.timezone(os.getenv("TIMEZONE", "Asia/Krasnoyarsk"))

STATIC_DIR = BASE_DIR / "webapp"
CATEGORIES = storage.CATEGORIES
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "120"))   # запросов в минуту на пользователя
MAX_IMAGE = 8 * 1024 * 1024


detect_category = ai.detect_category


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
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    resp.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
    return resp


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    if DEV_MODE:
        user = {"id": 0, "first_name": "Dev", "username": storage.OWNER_USERNAME}   # локально dev-пользователь = владелец
    else:
        user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
        if user is None or not isinstance(user.get("id"), int):
            raise _error(web.HTTPUnauthorized, "Откройте приложение через Telegram")
        if ALLOWED_USER_IDS and user["id"] not in ALLOWED_USER_IDS:
            raise _error(web.HTTPForbidden, "Доступ закрыт")
    if rate_limited(user["id"]):
        raise _error(web.HTTPTooManyRequests, "Слишком много запросов, подождите минуту")
    if not is_admin(user["id"], user.get("username")) and storage.is_blocked(user["id"]):
        raise _error(web.HTTPForbidden, "Доступ закрыт администратором")
    storage.upsert_user(user["id"], user.get("first_name", ""), user.get("username", ""), today())
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
    if rates.is_stale():
        asyncio.get_running_loop().run_in_executor(None, rates.refresh)   # в фоне, ответ не ждёт
    return web.json_response({
        "today": today().isoformat(),
        "categories": storage.all_categories(uid),
        "limits": storage.get_limits(uid),
        "expenses": storage.list_expenses(uid),
        "user": {"id": uid, "first_name": request["user"].get("first_name", ""), "is_admin": is_admin(uid, request["user"].get("username"))},
        "settings": storage.get_settings(uid),
        "currencies": currencies.as_list(),
        "rates": rates.info(),
    })


async def api_settings(request: web.Request):
    uid = request["user_id"]
    body = await request.json()
    try:
        out = storage.set_settings(uid, currency=body.get("currency"), favorites=body.get("favorites"))
    except ValueError as e:
        raise _error(web.HTTPBadRequest, str(e))
    return web.json_response(out)


# --- админка владельца: только метаданные, содержимое трат остаётся зашифрованным ---
def _require_admin(request: web.Request):
    if not is_admin(request["user_id"], request["user"].get("username")):
        raise _error(web.HTTPForbidden, "Только для владельца")


async def api_admin_overview(request: web.Request):
    _require_admin(request)
    data = await asyncio.get_running_loop().run_in_executor(None, storage.admin_overview, today())
    data["owner_id"] = request["user_id"]
    return web.json_response(data)


async def api_admin_block(request: web.Request):
    _require_admin(request)
    target = int(request.match_info["id"])
    if target == request["user_id"]:
        raise _error(web.HTTPBadRequest, "Нельзя закрыть доступ самому себе")
    body = await request.json()
    if not storage.set_blocked(target, bool(body.get("blocked"))):
        raise _error(web.HTTPNotFound, "Пользователь не найден")
    return web.json_response({"id": target, "blocked": bool(body.get("blocked"))})


async def api_add(request: web.Request):
    uid = request["user_id"]
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise _error(web.HTTPBadRequest, "Укажите название")
    amount = parse_amount(body.get("amount"))
    date = parse_date(body.get("date"))
    category = storage.normalize_category(body.get("category") or "")
    if category and category.lower() not in {c.lower() for c in storage.all_categories(uid)}:
        category = storage.add_user_category(uid, category)
    if not category:
        category = await asyncio.get_running_loop().run_in_executor(None, detect_category, name, uid)
    note = str(body.get("note") or "")
    currency = str(body.get("currency") or "").upper()
    if currency and not currencies.is_valid(currency):
        raise _error(web.HTTPBadRequest, "Неизвестная валюта")
    return web.json_response(storage.add_expense(uid, name, amount, category, date, note, currency or None), status=201)


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
    if body.get("category"):
        cat = storage.normalize_category(body["category"])
        if cat and cat.lower() not in {c.lower() for c in storage.all_categories(uid)}:
            cat = storage.add_user_category(uid, cat)      # своя категория пользователя
        fields["category"] = cat
    if "date" in body:
        fields["date"] = parse_date(body["date"])
    if "note" in body:
        fields["note"] = str(body["note"] or "")
    if body.get("currency"):
        if not currencies.is_valid(str(body["currency"]).upper()):
            raise _error(web.HTTPBadRequest, "Неизвестная валюта")
        fields["currency"] = str(body["currency"]).upper()
    item = storage.update_expense(uid, expense_id, **fields)
    if item and fields.get("category"):
        storage.cache_set(ai.normalize(item["name"]), item["category"], uid)   # запоминаем выбор
    return web.json_response(item)


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


async def api_categories_add(request: web.Request):
    uid = request["user_id"]
    body = await request.json()
    try:
        name = storage.add_user_category(uid, body.get("name", ""))
    except ValueError as e:
        raise _error(web.HTTPBadRequest, str(e))
    return web.json_response({"categories": storage.all_categories(uid), "added": name}, status=201)


async def api_categories_delete(request: web.Request):
    uid = request["user_id"]
    name = request.match_info["name"]
    if name in CATEGORIES:
        raise _error(web.HTTPBadRequest, "Базовую категорию удалить нельзя")
    storage.remove_user_category(uid, name)
    return web.json_response({"categories": storage.all_categories(uid)})


async def api_bulk(request: web.Request):
    uid = request["user_id"]
    body = await request.json()
    items = body.get("items")
    if not isinstance(items, list) or not items or len(items) > 60:
        raise _error(web.HTTPBadRequest, "Нет позиций для добавления")
    clean = []
    for it in items:
        name = str(it.get("name", "")).strip()
        if not name:
            continue
        cat = it.get("category") if it.get("category") in storage.all_categories(uid) else (ai.by_keywords(name) or "Другое")
        cur = str(it.get("currency") or "").upper()
        clean.append({"name": name, "amount": parse_amount(it.get("amount")), "category": cat, "date": parse_date(it.get("date")),
                      "note": str(it.get("note") or ""), "currency": cur if currencies.is_valid(cur) else None})
    if not clean:
        raise _error(web.HTTPBadRequest, "Нет позиций для добавления")
    added = storage.add_expenses_bulk(uid, clean)
    return web.json_response({"added": added, "total": sum(a["amount"] for a in added)}, status=201)


async def api_receipt(request: web.Request):
    """Фото чека (multipart, поле image) -> распознанные позиции без сохранения."""
    reader = await request.multipart()
    data, mime = b"", "image/jpeg"
    async for part in reader:
        if part.name == "image":
            mime = part.headers.get("Content-Type", mime) or mime
            while True:
                chunk = await part.read_chunk(256 * 1024)
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_IMAGE:
                    raise _error(web.HTTPRequestEntityTooLarge, "Фото больше 8 МБ")
    if not data:
        raise _error(web.HTTPBadRequest, "Нет фото")
    parsed = await asyncio.get_running_loop().run_in_executor(None, receipt.resolve, data, None, mime if mime.startswith("image/") else "image/jpeg")
    if not parsed or not parsed["items"]:
        raise _error(web.HTTPUnprocessableEntity, "Не удалось разобрать чек. Сфотографируйте ровнее при хорошем свете, чтобы был виден QR-код")
    parsed["date"] = parsed["date"] or today().isoformat()
    return web.json_response(parsed)


async def api_receipt_qr(request: web.Request):
    """Строка QR-кода (например, из сканера Telegram) -> позиции чека."""
    body = await request.json()
    qr = receipt.find_qr_text(str(body.get("qr", "")))
    if not qr:
        raise _error(web.HTTPBadRequest, "Это не QR-код кассового чека")
    parsed = await asyncio.get_running_loop().run_in_executor(None, receipt.resolve, None, qr)
    if not parsed:
        raise _error(web.HTTPUnprocessableEntity, "Не удалось получить чек по этому QR")
    parsed["date"] = parsed["date"] or today().isoformat()
    return web.json_response(parsed)


async def api_export(request: web.Request):
    """Excel за период: в Telegram отправляем файл в чат с ботом, в dev-режиме отдаём файл напрямую."""
    uid = request["user_id"]
    body = await request.json()
    if body.get("from") and body.get("to"):
        d_from, d_to = parse_date(body["from"]), parse_date(body["to"])
        if d_from > d_to:
            d_from, d_to = d_to, d_from
        items = storage.list_expenses_between(uid, d_from, d_to)
        label = f"{storage.to_display(d_from)} – {storage.to_display(d_to)}"
        name = f"расходы_{d_from}_{d_to}.xlsx"
    else:
        key = str(body.get("period", "30"))
        label, days = export.PERIODS.get(key, export.PERIODS["30"])
        items = storage.list_expenses(uid, days, today())
        name = export.filename(key, today())
    data = await asyncio.get_running_loop().run_in_executor(None, export.build_xlsx, items, label, today(), storage.currency_symbol(uid))
    if DEV_MODE or body.get("download"):
        return web.Response(body=data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})
    form = aiohttp.FormData()
    form.add_field("chat_id", str(uid))
    form.add_field("caption", f"📥 Расходы: {label.lower()} · {len(items)} зап. · {sum(i['amount'] for i in items):,} ₽".replace(",", " "))
    form.add_field("document", data, filename=name, content_type="application/octet-stream")
    async with aiohttp.ClientSession() as session:
        async with session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument", data=form, timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status != 200:
                logger.warning("sendDocument: %s %s", r.status, await r.text())
                raise _error(web.HTTPBadGateway, "Не удалось отправить файл в Telegram")
    return web.json_response({"ok": True, "count": len(items)})


async def index(request: web.Request):
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request: web.Request):
    return web.json_response({"ok": True})


def make_app() -> web.Application:
    app = web.Application(middlewares=[security_headers, auth_middleware], client_max_size=MAX_IMAGE + 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/expenses", api_add)
    app.router.add_put(r"/api/expenses/{id:\d+}", api_update)
    app.router.add_delete(r"/api/expenses/{id:\d+}", api_delete)
    app.router.add_put("/api/limits", api_limits)
    app.router.add_put("/api/settings", api_settings)
    app.router.add_post("/api/expenses/bulk", api_bulk)
    app.router.add_post("/api/categories", api_categories_add)
    app.router.add_delete("/api/categories/{name}", api_categories_delete)
    app.router.add_post("/api/receipt", api_receipt)
    app.router.add_post("/api/receipt/qr", api_receipt_qr)
    app.router.add_post("/api/export", api_export)
    app.router.add_get("/api/admin/overview", api_admin_overview)
    app.router.add_put(r"/api/admin/users/{id:\d+}", api_admin_block)
    app.router.add_static("/static", STATIC_DIR, show_index=False)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TELEGRAM_TOKEN and not DEV_MODE:
        raise SystemExit("TELEGRAM_TOKEN не задан: он нужен для проверки подписи Telegram Mini App")
    storage.init_db()
    logger.info("Mini App API на http://%s:%s (dev=%s)", HOST, PORT, DEV_MODE)
    web.run_app(make_app(), host=HOST, port=PORT, print=None)
