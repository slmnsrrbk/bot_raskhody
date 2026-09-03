import asyncio
import datetime
import logging
import os
import re
import sys
from pathlib import Path

import pytz
import requests
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
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import storage

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
logger = logging.getLogger("bot_raskhody")

# ---------------------------------------------------------------------------
# Конфигурация (секреты только в .env)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAD_API_KEY = os.getenv("CHAD_API_KEY", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)          # кому отдать старые записи из expenses.json
ALLOWED_USER_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USER_IDS", ""))}  # пусто = бот открыт всем
TIMEZONE = os.getenv("TIMEZONE", "Asia/Krasnoyarsk")
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "21:00")
TZ = pytz.timezone(TIMEZONE)
CHAD_API_URL = "https://ask.chadgpt.ru/api/public/gpt-4o-mini"

CATEGORIES = storage.CATEGORIES
BTN_TODAY, BTN_WEEK, BTN_MONTH = "Расходы за сегодня", "За 7 дней", "За 30 дней"
BTN_LIMIT, BTN_DELETE, BTN_APP = "Установить лимит", "🗑 Удалить трату", "📱 Открыть приложение"

scheduler = AsyncIOScheduler(timezone=TZ)


def today() -> datetime.date:
    return datetime.datetime.now(TZ).date()


def fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ") + " ₽"


# ---------------------------------------------------------------------------
# ChadGPT
# ---------------------------------------------------------------------------
def _ask_chad(message: str):
    if not CHAD_API_KEY:
        return None
    try:
        r = requests.post(CHAD_API_URL, json={"message": message, "api_key": CHAD_API_KEY}, timeout=20)
        resp = r.json()
        if r.ok and resp.get("is_success"):
            return resp.get("response", "")
    except Exception as e:  # noqa: BLE001
        logger.warning("ChadGPT: %s", e)
    return None


def detect_category(name: str) -> str:
    answer = _ask_chad(f"Определи категорию для траты '{name}' одним словом. Только: {', '.join(CATEGORIES)}.")
    if answer and answer.strip():
        word = answer.strip().split()[0].strip(".,!").capitalize()
        if word in CATEGORIES:
            return word
    return "Другое"


def generate_advice(text: str) -> str:
    return _ask_chad(f"{text}\nДай один короткий и практичный совет по оптимизации расходов.") or ""


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
    rows = [[BTN_TODAY, BTN_WEEK], [BTN_MONTH, BTN_LIMIT], [BTN_DELETE]]
    if WEBAPP_URL:
        rows[2].append(KeyboardButton(BTN_APP, web_app=WebAppInfo(url=WEBAPP_URL)))
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
        "Просто напишите, например: `вчера хлеб 200` или `такси 350`\n\n"
        f"{limit_text(user_id)}",
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )


@guarded
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    parsed = parse_expense(update.message.text)
    if not parsed:
        await update.message.reply_text("⚠️ Не понял. Пример: `вчера хлеб 200`", parse_mode="Markdown")
        return
    name, amount, date = parsed
    category = await in_thread(detect_category, name)
    item = storage.add_expense(user_id, name, amount, category, date)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отменить", callback_data=f"del:{item['id']}")]])
    await update.message.reply_text(
        f"✅ {item['name']} — {fmt(item['amount'])} ({item['category']}) — {item['date']} добавлено.",
        reply_markup=kb,
    )
    status = limit_status(user_id)
    if status:
        await update.message.reply_text("📊 " + status)


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


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не задан: добавьте строку TELEGRAM_TOKEN=... в файл .env рядом с main.py")
        sys.exit(1)
    if not CHAD_API_KEY:
        logger.warning("CHAD_API_KEY не задан — категории будут «Другое», советы отключены.")
    storage.init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    T = filters.TEXT & ~filters.COMMAND
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("delete", ask_delete))
    app.add_handler(CommandHandler("undo", delete_last))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_TODAY}$"), report_today))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_WEEK}$"), report_7))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_MONTH}$"), report_30))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_LIMIT}$"), ask_limit_type))
    app.add_handler(MessageHandler(T & filters.Regex(f"^{BTN_DELETE}$"), ask_delete))
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
