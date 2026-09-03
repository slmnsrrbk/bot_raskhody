import asyncio
import datetime
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
import crypto
import export
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
    """-> (name, amount, date) или None."""
    text = text.lower().replace("₽", "").replace("руб.", "").replace("руб", "").strip()
    date = today()
    if text.startswith("вчера"):
        date -= datetime.timedelta(days=1)
        text = text[len("вчера"):].strip()
    elif text.startswith("сегодня"):
        text = text[len("сегодня"):].strip()

    m = re.match(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", text)
    if m:
        day, month, year = map(int, m.groups(default=str(date.year)))
        try:
            date = datetime.date(year, month, day)
        except ValueError:
            return None
        text = text[m.end():].strip()

    m = re.match(r"(.+?)\s+(\d+)\s*$", text) or re.match(r"(\d+)\s+(.+?)\s*$", text)
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    name, amount = (a, b) if b.isdigit() else (b, a)
    amount = int(amount)
    if amount <= 0 or amount > 100_000_000:
        return None
    return name.strip().capitalize(), amount, date


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


PIN_SETUP_TEXT = (
    "🔐 Ваши данные шифруются паролем, который знаете только вы. Сервер его не хранит, "
    "поэтому восстановить забытый пароль невозможно.\n\n"
    "Придумайте пароль (от 6 символов, лучше буквы и цифры) и отправьте его сообщением — я сразу удалю его из чата."
)
PIN_UNLOCK_TEXT = "🔒 Данные закрыты. Введите пароль сообщением — я удалю его из чата."


async def ensure_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    st = crypto.status(user_id)
    if st == "unlocked":
        return True
    context.user_data["await_pin"] = "setup" if st == "nopin" else "unlock"
    context.user_data.pop("pin_first", None)
    msg = update.effective_message
    if msg and msg.text and msg.text not in BUTTON_TEXTS and not msg.text.startswith("/"):
        context.user_data["pending"] = msg.text
    if msg:
        await msg.reply_text(PIN_SETUP_TEXT if st == "nopin" else PIN_UNLOCK_TEXT)
    return False


def guarded(handler, needs_key=True):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user_id = register(update)
        except PermissionError:
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Этот бот приватный, доступ закрыт.")
            return
        if needs_key and not await ensure_unlocked(update, context, user_id):
            return
        try:
            return await handler(update, context, user_id)
        except (crypto.Locked, crypto.NoPin):
            await ensure_unlocked(update, context, user_id)
    return wrapper


def open_handler(handler):
    return guarded(handler, needs_key=False)


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------
@open_handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    context.user_data.pop("limit_type", None)
    st = crypto.status(user_id)
    tail = limit_text(user_id) if st == "unlocked" else (
        "🔐 Данные шифруются вашим паролем. Задайте его при первой трате." if st == "nopin"
        else "🔒 Данные закрыты — введите пароль при следующем действии.")
    await update.message.reply_text(
        f"👋 Привет, {update.effective_user.first_name}! Я помогу вести учёт расходов.\n\n"
        "Просто напишите, например: `вчера хлеб 200` или `такси 350`.\n"
        "📷 Пришлите фото чека — я распознаю покупки и добавлю их сами.\n"
        "Команды: /pin — сменить пароль, /lock — закрыть данные, /export — таблица Excel.\n\n"
        f"{tail}",
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )
    if st == "nopin":
        context.user_data["await_pin"] = "setup"
        context.user_data.pop("pin_first", None)
        await update.message.reply_text(PIN_SETUP_TEXT)


# --- пароль шифрования ---
async def pin_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перехватывает сообщение с паролем, когда бот его ждёт (группа -1)."""
    mode = context.user_data.get("await_pin")
    msg = update.message
    if not mode or not msg or not msg.text:
        return
    text = msg.text.strip()
    if text.startswith("/") or text in BUTTON_TEXTS:
        return
    try:
        await msg.delete()
    except Exception:  # noqa: BLE001
        pass
    try:
        user_id = register(update)
    except PermissionError:
        raise ApplicationHandlerStop
    reply = context.bot.send_message
    try:
        if mode == "setup":
            first = context.user_data.get("pin_first")
            if not first:
                crypto.validate_pin(text)
                context.user_data["pin_first"] = text
                await reply(user_id, "Повторите пароль ещё раз:")
                raise ApplicationHandlerStop
            if text != first:
                context.user_data.pop("pin_first", None)
                await reply(user_id, "Пароли не совпали. Придумайте пароль заново и отправьте его:")
                raise ApplicationHandlerStop
            crypto.setup_pin(user_id, text)
            await reply(user_id, "✅ Пароль установлен, данные зашифрованы. Запомните его: восстановить пароль нельзя.",
                        reply_markup=main_keyboard())
        elif mode == "change":
            old = context.user_data.get("pin_old")
            if not old:
                context.user_data["pin_old"] = text
                await reply(user_id, "Теперь отправьте новый пароль:")
                raise ApplicationHandlerStop
            crypto.change_pin(user_id, old, text)
            await reply(user_id, "✅ Пароль изменён.")
        else:
            crypto.unlock(user_id, text)
            await reply(user_id, "🔓 Данные открыты.", reply_markup=main_keyboard())
    except ValueError as e:
        await reply(user_id, f"⚠️ {e}")
        raise ApplicationHandlerStop
    except crypto.WrongPin:
        context.user_data.pop("pin_old", None)
        await reply(user_id, "❌ Неверный пароль, попробуйте ещё раз.")
        raise ApplicationHandlerStop
    except crypto.TooManyAttempts:
        await reply(user_id, "⛔ Слишком много неверных попыток. Подождите 15 минут.")
        raise ApplicationHandlerStop
    context.user_data.pop("await_pin", None)
    context.user_data.pop("pin_first", None)
    context.user_data.pop("pin_old", None)
    pending = context.user_data.pop("pending", None)
    if pending:
        await process_expense(context.bot, user_id, pending)
    raise ApplicationHandlerStop


@open_handler
async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if crypto.status(user_id) == "nopin":
        context.user_data["await_pin"] = "setup"
        context.user_data.pop("pin_first", None)
        await update.message.reply_text(PIN_SETUP_TEXT)
    else:
        context.user_data["await_pin"] = "change"
        context.user_data.pop("pin_old", None)
        await update.message.reply_text("Отправьте текущий пароль (сообщение будет удалено):")


@open_handler
async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    crypto.lock(user_id)
    context.user_data.pop("await_pin", None)
    await update.message.reply_text("🔒 Данные закрыты. Для работы снова понадобится пароль.")


@open_handler
async def cmd_forgot(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Да, удалить всё и начать заново", callback_data="wipe:yes")],
                               [InlineKeyboardButton("Отмена", callback_data="close")]])
    await update.message.reply_text(
        "Без пароля данные расшифровать невозможно. Единственный выход — удалить все ваши записи и задать новый пароль. Удалить?",
        reply_markup=kb)


async def process_expense(bot, user_id: int, text: str):
    parsed = parse_expense(text)
    if not parsed:
        await bot.send_message(user_id, "⚠️ Не понял. Пример: `вчера хлеб 200`", parse_mode="Markdown")
        return
    name, amount, date = parsed
    category = await in_thread(detect_category, name)
    item = storage.add_expense(user_id, name, amount, category, date)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отменить", callback_data=f"del:{item['id']}")]])
    await bot.send_message(user_id, f"✅ {item['name']} — {fmt(item['amount'])} ({item['category']}) — {item['date']} добавлено.",
                           reply_markup=kb)
    status = limit_status(user_id)
    if status:
        await bot.send_message(user_id, "📊 " + status)


@guarded
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    await process_expense(context.bot, user_id, update.message.text)


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
    if not ai.POLZA_API_KEY:
        await update.message.reply_text("Распознавание чеков не настроено (нет ключа Polza AI).")
        return
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
        data = bytes(await f.download_as_bytearray())
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось скачать фото: %s", e)
        await wait.edit_text("Не удалось получить фото, попробуйте ещё раз.")
        return
    parsed = await in_thread(ai.parse_receipt, data, mime)
    if not parsed or not parsed["items"]:
        await wait.edit_text("🤷 Не смог разобрать чек. Попробуйте сфотографировать ровнее и при хорошем свете.")
        return
    date = parsed["date"] or today().isoformat()
    for it in parsed["items"]:
        it["date"] = date
    added = storage.add_expenses_bulk(user_id, parsed["items"])
    token = secrets.token_hex(4)
    context.user_data.setdefault("receipts", {})[token] = [a["id"] for a in added]
    total = sum(a["amount"] for a in added)
    head = f"🧾 {parsed['store'] or 'Чек'} · {storage.to_display(date)}"
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
    if data == "wipe:yes":
        storage.wipe_user(user_id)
        context.user_data.clear()
        await q.answer("Удалено")
        await q.edit_message_text("🗑 Все данные удалены. Отправьте /start, чтобы задать новый пароль.")
        return
    if data != "close" and crypto.status(user_id) != "unlocked":
        await q.answer("Сначала введите пароль", show_alert=True)
        await ensure_unlocked(update, context, user_id)
        return
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
        if not crypto.is_unlocked(user_id):
            continue
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


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не задан: добавьте строку TELEGRAM_TOKEN=... в файл .env рядом с main.py")
        sys.exit(1)
    if not (ai.POLZA_API_KEY or ai.CHAD_API_KEY):
        logger.warning("POLZA_API_KEY не задан — категории определяются только по словарю, советы отключены.")
    storage.init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    T = filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, pin_gate), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pin", cmd_pin))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("forgot", cmd_forgot))
    app.add_handler(CommandHandler("delete", ask_delete))
    app.add_handler(CommandHandler("undo", delete_last))
    app.add_handler(CommandHandler("export", ask_export))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_TODAY}$"), report_today))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_WEEK}$"), report_7))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_MONTH}$"), report_30))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_LIMIT}$"), ask_limit_type))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_DELETE}$"), ask_delete))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_EXPORT}$"), ask_export))
    app.add_handler(MessageHandler(T & filters.Regex(r"(?i)^удалить\s+последн"), delete_last))
    app.add_handler(MessageHandler(T & filters.Regex(r"(?i)лимит$"), ask_limit_value))
    app.add_handler(MessageHandler(T & filters.Regex(r"^\d+$"), set_limit_value))
    app.add_handler(MessageHandler(T & filters.Regex("^🔙 Назад$"), start))
    app.add_handler(MessageHandler(T, handle_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(error_handler)

    logger.info("🤖 Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
