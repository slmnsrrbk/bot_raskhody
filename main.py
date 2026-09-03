import asyncio
import datetime
import functools
import io
import logging
import secrets
import os
import re
import sys
from pathlib import Path

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import ai
import export
import parsing
import receipt
import storage

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
logger = logging.getLogger("bot_raskhody")

# ---------------------------------------------------------------------------
# Конфигурация (секреты только в .env)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)          # кому отдать старые записи из expenses.json
ALLOWED_USER_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USER_IDS", ""))}  # пусто = бот открыт всем
TIMEZONE = os.getenv("TIMEZONE", "Asia/Krasnoyarsk")
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "21:00")
TZ = pytz.timezone(TIMEZONE)

CATEGORIES = storage.CATEGORIES
BTN_TODAY, BTN_WEEK, BTN_MONTH = "Расходы за сегодня", "За 7 дней", "За 30 дней"
BTN_LIMIT, BTN_DELETE, BTN_APP = "Установить лимит", "🗑 Удалить трату", "📱 Открыть приложение"
BTN_EXPORT = "📥 Выгрузка"
BUTTON_TEXTS = {BTN_TODAY, BTN_WEEK, BTN_MONTH, BTN_LIMIT, BTN_DELETE, BTN_EXPORT, BTN_APP, "🔙 Назад",
                "Ежедневный лимит", "Еженедельный лимит", "Ежемесячный лимит"}

scheduler = AsyncIOScheduler(timezone=TZ)


def today() -> datetime.date:
    return datetime.datetime.now(TZ).date()


def fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ") + " ₽"


detect_category = ai.detect_category
generate_advice = ai.generate_advice


async def in_thread(func, *args):
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)


# ---------------------------------------------------------------------------
# Разбор сообщения «вчера хлеб 200»
# ---------------------------------------------------------------------------
def parse_expense(text: str):
    """Одна трата из строки -> (name, amount, date) или None (совместимость)."""
    items, unparsed = parsing.parse_free_text(text, today())
    if len(items) == 1 and not unparsed:
        it = items[0]
        return it["name"], it["amount"], it["date"]
    return None


def parse_expenses(text: str, spoken: bool = False):
    """Все траты из свободного текста: строгий разбор, при неудаче — модель.
    Для голосовых (spoken=True) сначала модель: речь разговорная, суммы часто словами."""
    items, unparsed = parsing.parse_free_text(text, today())
    if items and not unparsed and not spoken:
        return items
    ai_items = ai.parse_expenses_text(text, today(), spoken=spoken)
    if ai_items:
        return [{"name": i["name"], "amount": i["amount"], "date": datetime.date.fromisoformat(i["date"])} for i in ai_items]
    return items


# ---------------------------------------------------------------------------
# Отчёты
# ---------------------------------------------------------------------------
def build_report(user_id: int, days: int):
    total, cats, count = storage.totals(user_id, days, today())
    if not total:
        return "📊 Нет расходов за выбранный период.", 0, {}
    lines = ["📊 Расходы за сегодня" if days == 1 else f"📊 Расходы за {days} дней", f"Общая сумма: {fmt(total)}"]
    for cat, amt in cats.items():
        lines.append(f"• {cat}: {fmt(amt)} ({amt / total * 100:.1f}%)")
    return "\n".join(lines), total, cats


def limit_text(user_id: int) -> str:
    lim = storage.get_limits(user_id)
    show = lambda v: fmt(v) if v else "не установлен"  # noqa: E731
    return (
        "🔔 Текущие лимиты:\n"
        f"• Ежедневный: {show(lim['daily'])}\n"
        f"• Еженедельный: {show(lim['weekly'])}\n"
        f"• Ежемесячный: {show(lim['monthly'])}"
    )


def limit_status(user_id: int) -> str:
    """Строки вида «Сегодня: 1 200 ₽ из 2 500 ₽, осталось 1 300 ₽» по заданным лимитам."""
    lim = storage.get_limits(user_id)
    out = []
    for key, days, label in (("daily", 1, "Сегодня"), ("weekly", 7, "За неделю"), ("monthly", 30, "За месяц")):
        if lim[key]:
            spent = storage.totals(user_id, days, today())[0]
            left = lim[key] - spent
            tail = f"осталось {fmt(left)}" if left >= 0 else f"⚠️ превышен на {fmt(-left)}"
            out.append(f"{label}: {fmt(spent)} из {fmt(lim[key])}, {tail}")
    return "\n".join(out)


def main_keyboard():
    rows = [[BTN_TODAY, BTN_WEEK], [BTN_MONTH, BTN_LIMIT], [BTN_DELETE, BTN_EXPORT]]
    if WEBAPP_URL:
        rows.append([KeyboardButton(BTN_APP, web_app=WebAppInfo(url=WEBAPP_URL))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ---------------------------------------------------------------------------
# Доступ и регистрация пользователя
# ---------------------------------------------------------------------------
def register(update: Update) -> int:
    """Возвращает id пользователя; ValueError — если доступ закрыт."""
    u = update.effective_user
    if ALLOWED_USER_IDS and u.id not in ALLOWED_USER_IDS:
        raise PermissionError
    storage.upsert_user(u.id, u.first_name or "", u.username or "")
    if storage.legacy_pending():
        owner = OWNER_ID or u.id
        n = storage.migrate_legacy(owner)
        logger.info("Старые записи (%s шт.) перенесены пользователю %s", n, owner)
    return u.id


def guarded(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user_id = register(update)
        except PermissionError:
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Этот бот приватный, доступ закрыт.")
            return
        return await handler(update, context, user_id)
    return wrapper


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------
@guarded
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    context.user_data.pop("limit_type", None)
    await update.message.reply_text(
        f"👋 Привет, {update.effective_user.first_name}! Я помогу вести учёт расходов.\n\n"
        "Просто напишите, например: `вчера хлеб 200` или `такси 350`.\n"
        "📷 Пришлите фото чека — я распознаю покупки и добавлю их сами.\n"
        "🎙 Или запишите голосовое: «такси триста пятьдесят и кофе двести» — добавлю всё, что услышу.\n\n"
        f"{limit_text(user_id)}",
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )


async def category_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перехватывает название новой категории, когда бот его ждёт (группа -1)."""
    pending = context.user_data.get("await_category")
    msg = update.message
    if not pending or not msg or not msg.text or msg.text.startswith("/") or msg.text in BUTTON_TEXTS:
        return
    try:
        user_id = register(update)
        cat = storage.add_user_category(user_id, msg.text)
    except PermissionError:
        raise ApplicationHandlerStop
    except ValueError as e:
        await msg.reply_text(f"⚠️ {e}")
        raise ApplicationHandlerStop
    context.user_data.pop("await_category", None)
    item = storage.update_expense(user_id, pending["item_id"], category=cat)
    if item:
        storage.cache_set(ai.normalize(item["name"]), cat, user_id)
    text = _single_text(item) if pending["token"] == "-" and item else None
    if text is None:
        items = _batch_items(context, user_id, pending["token"])
        text = _batch_text(items) if items else "Записи уже удалены."
        kb = _batch_kb(pending["token"]) if items else None
    else:
        kb = _single_kb(item["id"])
    try:
        await context.bot.edit_message_text(text, chat_id=pending["chat_id"], message_id=pending["message_id"], reply_markup=kb)
    except Exception:  # noqa: BLE001
        pass
    await msg.reply_text(f"✅ Категория «{cat}» добавлена и назначена.")
    raise ApplicationHandlerStop


@guarded
async def handle_qr_text(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    wait = await update.message.reply_text("🔎 Проверяю чек по QR…")
    parsed = await in_thread(receipt.resolve, None, receipt.find_qr_text(update.message.text))
    if not parsed:
        await wait.edit_text("Не удалось получить данные чека по этому QR.")
        return
    await _finish_receipt(update, context, user_id, parsed, wait)


def _items_text(items, header: str) -> str:
    lines, last_date = [], None
    for a in items:
        if a["date"] != last_date:
            lines.append(f"\n📅 {a['date']}")
            last_date = a["date"]
        lines.append(f"• {a['name']} — {fmt(a['amount'])} ({a['category']})")
    return header + "\n".join(lines)


def _remember_batch(context, ids) -> str:
    token = secrets.token_hex(4)
    if context is not None:
        context.user_data.setdefault("receipts", {})[token] = list(ids)
    return token


def _batch_items(context, user_id: int, token: str):
    ids = (context.user_data.get("receipts", {}) if context is not None else {}).get(token, [])
    return [it for it in (storage.get_expense(user_id, i) for i in ids) if it]


def _single_kb(item_id: int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отменить", callback_data=f"del:{item_id}"),
                                  InlineKeyboardButton("✏️ Категория", callback_data=f"cat:{item_id}:-")]])


def _batch_kb(token: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отменить всё", callback_data=f"rc:{token}"),
                                  InlineKeyboardButton("✏️ Категории", callback_data=f"cats:{token}")]])


def _single_text(item) -> str:
    return f"✅ {item['name']} — {fmt(item['amount'])} ({item['category']}) — {item['date']} добавлено."


def _batch_text(items) -> str:
    total = sum(a["amount"] for a in items)
    return _items_text(items, f"✅ Добавлено {len(items)} {plural(len(items), 'трата', 'траты', 'трат')} на {fmt(total)}:")


async def process_expense(bot, user_id: int, text: str, context=None, spoken: bool = False):
    items = await in_thread(parse_expenses, text, spoken)
    if not items:
        if spoken:
            await bot.send_message(user_id, "🤷 Не нашёл трат в голосовом. Скажите, например: «такси триста пятьдесят и кофе двести».")
        else:
            await bot.send_message(user_id, "⚠️ Не понял. Примеры: `такси 350`, `вчера хлеб 200`, или список строк с датами.",
                                   parse_mode="Markdown")
        return
    # Быстрая категория (словарь/кэш) — сразу; остальное уточнит модель в фоне
    quick = [storage.cache_get(ai.normalize(it["name"]), user_id) or ai.by_keywords(it["name"]) or storage.cache_get(ai.normalize(it["name"])) for it in items]
    added = storage.add_expenses_bulk(user_id, [dict(it, category=c or "Другое") for it, c in zip(items, quick)])
    if len(added) == 1:
        msg = await bot.send_message(user_id, _single_text(added[0]), reply_markup=_single_kb(added[0]["id"]))
        token = None
    else:
        token = _remember_batch(context, [a["id"] for a in added])
        msg = await bot.send_message(user_id, _batch_text(added), reply_markup=_batch_kb(token))
    status = limit_status(user_id)
    if status:
        await bot.send_message(user_id, "📊 " + status)
    pending = [a for a, c in zip(added, quick) if c is None]
    if pending and (ai.POLZA_API_KEY or ai.CHAD_API_KEY):
        asyncio.create_task(_refine_categories(user_id, msg, pending, token, context))


async def _refine_categories(user_id: int, msg, pending, token, context):
    """Фоновое уточнение категорий моделью и обновление сообщения бота."""
    try:
        cats = await in_thread(ai.classify_many, [a["name"] for a in pending], user_id)
        changed = False
        for a, c in zip(pending, cats):
            if c and c != a["category"]:
                storage.update_expense(user_id, a["id"], category=c)
                changed = True
        if not changed:
            return
        if token is None:
            item = storage.get_expense(user_id, pending[0]["id"])
            if item:
                await msg.edit_text(_single_text(item), reply_markup=_single_kb(item["id"]))
        else:
            items = _batch_items(context, user_id, token)
            if items:
                await msg.edit_text(_batch_text(items), reply_markup=_batch_kb(token))
    except Exception as e:  # noqa: BLE001
        logger.warning("Уточнение категорий: %s", e)


def _category_kb(user_id: int, item_id: int, token: str):
    rows, row = [], []
    for i, c in enumerate(storage.all_categories(user_id)):
        row.append(InlineKeyboardButton(c, callback_data=f"setcat:{item_id}:{i}:{token}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("➕ Своя категория", callback_data=f"newcat:{item_id}:{token}")])
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data=f"back:{item_id}:{token}")])
    return InlineKeyboardMarkup(rows)


def _items_kb(context, user_id: int, token: str):
    items = _batch_items(context, user_id, token)
    rows = [[InlineKeyboardButton(f"{it['name']} · {it['category']}", callback_data=f"cat:{it['id']}:{token}")] for it in items[:30]]
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data=f"back:0:{token}")])
    return InlineKeyboardMarkup(rows)


async def _show_after_edit(q, context, user_id: int, item_id: int, token: str):
    """Вернуть сообщение к обычному виду после смены категории."""
    if token == "-":
        item = storage.get_expense(user_id, item_id)
        if item:
            await q.edit_message_text(_single_text(item), reply_markup=_single_kb(item_id))
        else:
            await q.edit_message_text("Эта запись уже удалена.")
    else:
        items = _batch_items(context, user_id, token)
        if items:
            await q.edit_message_text(_batch_text(items), reply_markup=_batch_kb(token))
        else:
            await q.edit_message_text("Записи уже удалены.")


@guarded
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    await process_expense(context.bot, user_id, update.message.text, context)


MAX_VOICE_SECONDS = 180


@guarded
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Голосовое/аудио: расшифровка речи -> траты в свободной форме."""
    msg = update.message
    media = msg.voice or msg.audio
    if not media:
        return
    if (media.duration or 0) > MAX_VOICE_SECONDS:
        await msg.reply_text(f"Голосовое длиннее {MAX_VOICE_SECONDS // 60} минут — запишите покороче.")
        return
    if not ai.POLZA_API_KEY:
        await msg.reply_text("Распознавание речи не настроено. Напишите траты текстом.")
        return
    wait = await msg.reply_text("🎙 Слушаю…")
    try:
        f = await media.get_file(read_timeout=30)
        data = bytes(await f.download_as_bytearray(read_timeout=90, connect_timeout=20))
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось скачать голосовое: %s", e)
        await wait.edit_text("Не удалось получить голосовое, попробуйте ещё раз.")
        return
    mime = getattr(media, "mime_type", None) or "audio/ogg"
    ext = {"audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/wav": "wav", "audio/x-wav": "wav"}.get(mime, "ogg")
    text = await in_thread(ai.transcribe, data, f"voice.{ext}", mime)
    if not text:
        await wait.edit_text("🤷 Не разобрал речь. Попробуйте ещё раз или напишите текстом.")
        return
    await wait.edit_text(f"🎙 Услышал: «{text[:500]}»")
    await process_expense(context.bot, user_id, text, context, spoken=True)


@guarded
async def report_today(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    await update.message.reply_text(build_report(user_id, 1)[0])


@guarded
async def report_7(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    await update.message.reply_text(build_report(user_id, 7)[0])


@guarded
async def report_30(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    report, total, _ = build_report(user_id, 30)
    advice = await in_thread(generate_advice, report) if total else ""
    await update.message.reply_text(f"{report}\n\n💡 Совет: {advice}" if advice else report)


# --- лимиты ---
@guarded
async def ask_limit_type(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    kb = [["Ежедневный лимит"], ["Еженедельный лимит"], ["Ежемесячный лимит"], ["🔙 Назад"]]
    await update.message.reply_text(
        limit_text(user_id) + "\n\nВыберите лимит, который хотите изменить:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )


@guarded
async def ask_limit_value(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    text = update.message.text.lower()
    kind = "daily" if "дневн" in text else "weekly" if "недель" in text else "monthly" if "месяч" in text else None
    if not kind:
        await update.message.reply_text("❌ Не удалось определить тип лимита.")
        return
    context.user_data["limit_type"] = kind
    await update.message.reply_text("Введите сумму в рублях (0 — убрать лимит):")


@guarded
async def set_limit_value(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    kind = context.user_data.get("limit_type")
    if not kind:
        await update.message.reply_text("❌ Сначала выберите тип лимита.", reply_markup=main_keyboard())
        return
    amount = int(update.message.text.strip())
    storage.set_limits(user_id, **{kind: amount})
    context.user_data.pop("limit_type", None)
    await update.message.reply_text(f"✅ Лимит обновлён.\n{limit_text(user_id)}", reply_markup=main_keyboard())


# --- удаление ---
def _item_label(it: dict) -> str:
    return f"{it['name']} · {fmt(it['amount'])} · {it['date'][:5]}"


def delete_keyboard(user_id: int):
    items = storage.list_expenses(user_id, limit=10)
    if not items:
        return None
    rows = [[InlineKeyboardButton("❌ " + _item_label(it), callback_data=f"del:{it['id']}")] for it in items]
    rows.append([InlineKeyboardButton("Закрыть", callback_data="close")])
    return InlineKeyboardMarkup(rows)


@guarded
async def ask_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    kb = delete_keyboard(user_id)
    if not kb:
        await update.message.reply_text("Удалять пока нечего — трат нет.")
        return
    await update.message.reply_text("Выберите трату, которую нужно удалить (показаны последние 10):", reply_markup=kb)


@guarded
async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    item = storage.last_expense(user_id)
    if not item:
        await update.message.reply_text("Удалять нечего — трат нет.")
        return
    await _do_delete(update.message.reply_text, context, user_id, item["id"])


async def _do_delete(send, context, user_id: int, expense_id: int):
    item = storage.delete_expense(user_id, expense_id)
    if not item:
        await send("Эта запись уже удалена.")
        return
    context.user_data.setdefault("deleted", {})[expense_id] = item
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Вернуть", callback_data=f"restore:{expense_id}")]])
    await send(f"🗑 Удалено: {_item_label(item)}", reply_markup=kb)


# --- чек по фото ---
@guarded
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    wait = await update.message.reply_text("🔎 Читаю чек…")
    photo = update.message.photo[-1] if update.message.photo else None
    doc = update.message.document
    try:
        if photo:
            f = await photo.get_file()
            mime = "image/jpeg"
        elif doc and (doc.mime_type or "").startswith("image/"):
            f = await doc.get_file()
            mime = doc.mime_type
        else:
            await wait.edit_text("Пришлите чек как фото.")
            return
        data = bytes(await f.download_as_bytearray(read_timeout=90, connect_timeout=20))
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось скачать фото: %s", e)
        await wait.edit_text("Не удалось получить фото, попробуйте ещё раз.")
        return
    parsed = await in_thread(receipt.resolve, data, None, mime)
    if not parsed or not parsed["items"]:
        await wait.edit_text("🤷 Не смог разобрать чек. Попробуйте сфотографировать ровнее и при хорошем свете, чтобы был виден QR-код.")
        return
    await _finish_receipt(update, context, user_id, parsed, wait)


async def _finish_receipt(update, context, user_id: int, parsed: dict, wait):
    date = parsed["date"] or today().isoformat()
    for it in parsed["items"]:
        it["date"] = date
    added = storage.add_expenses_bulk(user_id, parsed["items"])
    token = secrets.token_hex(4)
    context.user_data.setdefault("receipts", {})[token] = [a["id"] for a in added]
    total = sum(a["amount"] for a in added)
    src = {"qr": " · по QR", "ai": "", "qr-sum": " · только сумма из QR"}.get(parsed.get("source"), "")
    head = f"🧾 {parsed['store'] or 'Чек'} · {storage.to_display(date)}{src}"
    lines = [f"• {a['name']} — {fmt(a['amount'])} ({a['category']})" for a in added[:20]]
    if len(added) > 20:
        lines.append(f"… и ещё {len(added) - 20}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отменить весь чек", callback_data=f"rc:{token}")]])
    await wait.edit_text(f"{head}\nДобавлено {len(added)} {plural(len(added), 'позиция', 'позиции', 'позиций')} на {fmt(total)}:\n\n" + "\n".join(lines), reply_markup=kb)
    status = limit_status(user_id)
    if status:
        await update.message.reply_text("📊 " + status)


def plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


# --- выгрузка в Excel ---
def export_keyboard():
    rows = [[InlineKeyboardButton(label, callback_data=f"xls:{key}")] for key, (label, _) in export.PERIODS.items()]
    return InlineKeyboardMarkup(rows)


@guarded
async def ask_export(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    await update.message.reply_text("За какой период выгрузить таблицу?", reply_markup=export_keyboard())


async def send_export(bot, user_id: int, period_key: str):
    label, days = export.PERIODS.get(period_key, ("Всё время", None))
    items = storage.list_expenses(user_id, days, today())
    data = await in_thread(export.build_xlsx, items, label, today())
    await bot.send_document(
        chat_id=user_id,
        document=io.BytesIO(data),
        filename=export.filename(period_key, today()),
        caption=f"📥 Расходы: {label.lower()} · {len(items)} {plural(len(items), 'запись', 'записи', 'записей')} · {fmt(sum(i['amount'] for i in items))}",
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        user_id = register(update)
    except PermissionError:
        await q.answer("Доступ закрыт", show_alert=True)
        return
    data = q.data or ""
    if data == "close":
        await q.answer()
        await q.edit_message_text("Закрыто.")
    elif data.startswith("del:"):
        await q.answer()
        expense_id = int(data[4:])
        item = storage.delete_expense(user_id, expense_id)
        if not item:
            await q.edit_message_text("Эта запись уже удалена.")
            return
        context.user_data.setdefault("deleted", {})[expense_id] = item
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Вернуть", callback_data=f"restore:{expense_id}")]])
        await q.edit_message_text(f"🗑 Удалено: {_item_label(item)}", reply_markup=kb)
    elif data.startswith("restore:"):
        item = context.user_data.get("deleted", {}).pop(int(data[8:]), None)
        if not item:
            await q.answer("Не удалось вернуть: запись не найдена", show_alert=True)
            return
        new = storage.add_expense(user_id, item["name"], item["amount"], item["category"], item["iso"])
        await q.answer("Возвращено")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отменить", callback_data=f"del:{new['id']}")]])
        await q.edit_message_text(f"✅ Возвращено: {_item_label(new)}", reply_markup=kb)
    elif data.startswith("cats:"):
        await q.answer()
        await q.edit_message_reply_markup(_items_kb(context, user_id, data[5:]))
    elif data.startswith("cat:"):
        await q.answer()
        _, item_id, token = data.split(":")
        item = storage.get_expense(user_id, int(item_id))
        if not item:
            await q.edit_message_text("Эта запись уже удалена.")
            return
        await q.edit_message_text(f"Категория для «{item['name']}» — {fmt(item['amount'])}. Сейчас: {item['category']}. Выберите:",
                                  reply_markup=_category_kb(user_id, int(item_id), token))
    elif data.startswith("setcat:"):
        _, item_id, idx, token = data.split(":")
        cats = storage.all_categories(user_id)
        cat = cats[int(idx)] if int(idx) < len(cats) else "Другое"
        item = storage.update_expense(user_id, int(item_id), category=cat)
        if item:
            storage.cache_set(ai.normalize(item["name"]), cat, user_id)   # запоминаем выбор для будущих трат
        await q.answer(f"Категория: {cat}")
        await _show_after_edit(q, context, user_id, int(item_id), token)
    elif data.startswith("newcat:"):
        _, item_id, token = data.split(":")
        context.user_data["await_category"] = {"item_id": int(item_id), "token": token,
                                               "chat_id": q.message.chat_id, "message_id": q.message.message_id}
        await q.answer()
        await q.edit_message_text("Напишите название новой категории (например, «Дети» или «Спорт»):",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"back:{item_id}:{token}")]]))
    elif data.startswith("back:"):
        _, item_id, token = data.split(":")
        await q.answer()
        await _show_after_edit(q, context, user_id, int(item_id), token)
    elif data.startswith("rc:"):
        ids = context.user_data.get("receipts", {}).pop(data[3:], None)
        if not ids:
            await q.answer("Чек уже отменён", show_alert=True)
            return
        n = storage.delete_many(user_id, ids)
        await q.answer("Чек отменён")
        await q.edit_message_text(f"🗑 Чек отменён, удалено {n} {plural(n, 'позиция', 'позиции', 'позиций')}.")
    elif data.startswith("xls:"):
        await q.answer("Готовлю файл…")
        await send_export(context.bot, user_id, data[4:])
        try:
            await q.edit_message_reply_markup(None)
        except Exception:  # noqa: BLE001
            pass
    else:
        await q.answer()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Ошибка при обработке обновления: %s", context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Произошла ошибка. Попробуйте ещё раз.")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Автоотчёты — каждому пользователю по его тратам
# ---------------------------------------------------------------------------
async def _broadcast(app: Application, days: int, title: str, with_advice=False):
    for user_id in storage.users_with_expenses(days, today()):
        try:
            report, total, _ = build_report(user_id, days)
            if with_advice and total:
                advice = await in_thread(generate_advice, report)
                if advice:
                    report += f"\n\n💡 Совет: {advice}"
            await app.bot.send_message(chat_id=user_id, text=f"{title}\n\n{report}")
        except Exception as e:  # noqa: BLE001
            logger.warning("Не удалось отправить отчёт %s: %s", user_id, e)


async def send_daily_report(app):
    await _broadcast(app, 1, "🕒 Автоотчёт за сегодня:")


async def send_weekly_report(app):
    await _broadcast(app, 7, "📅 Автоотчёт за неделю:")


async def send_monthly_report(app):
    await _broadcast(app, 30, "📆 Автоотчёт за месяц:", with_advice=True)


async def post_init(app: Application):
    if WEBAPP_URL:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Приложение", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    hour, minute = (int(x) for x in DAILY_REPORT_TIME.split(":"))
    scheduler.add_job(send_daily_report, "cron", args=[app], hour=hour, minute=minute)
    scheduler.add_job(send_weekly_report, "cron", args=[app], day_of_week="fri", hour=23, minute=59)
    scheduler.add_job(send_monthly_report, "cron", args=[app], day="last", hour=23, minute=59)
    scheduler.start()


async def post_shutdown(app: Application):
    if scheduler.running:
        scheduler.shutdown(wait=False)


def build_app(token: str) -> Application:
    app = (
        Application.builder().token(token)
        # скачивание голосовых/фото и отправка файлов идут дольше 5 с по умолчанию
        .connect_timeout(20).read_timeout(60).write_timeout(60).pool_timeout(20)
        .post_init(post_init).post_shutdown(post_shutdown).build()
    )
    T = filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, category_gate), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("delete", ask_delete))
    app.add_handler(CommandHandler("undo", delete_last))
    app.add_handler(CommandHandler("export", ask_export))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_TODAY}$"), report_today))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_WEEK}$"), report_7))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_MONTH}$"), report_30))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_LIMIT}$"), ask_limit_type))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_DELETE}$"), ask_delete))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_EXPORT}$"), ask_export))
    app.add_handler(MessageHandler(T & filters.Regex(r"(?i)^удалить\s+последн"), delete_last))
    app.add_handler(MessageHandler(T & filters.Regex(receipt.QR_RE.pattern), handle_qr_text))
    app.add_handler(MessageHandler(T & filters.Regex(r"(?i)лимит$"), ask_limit_value))
    app.add_handler(MessageHandler(T & filters.Regex(r"^\d+$"), set_limit_value))
    app.add_handler(MessageHandler(T & filters.Regex("^🔙 Назад$"), start))
    app.add_handler(MessageHandler(T, handle_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(error_handler)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не задан: добавьте строку TELEGRAM_TOKEN=... в файл .env рядом с main.py")
        sys.exit(1)
    if not (ai.POLZA_API_KEY or ai.CHAD_API_KEY):
        logger.warning("POLZA_API_KEY не задан — категории определяются только по словарю, советы отключены.")
    storage.init_db()
    app = build_app(TELEGRAM_TOKEN)
    logger.info("🤖 Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
