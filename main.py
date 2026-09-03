import datetime
import json
import logging
import os
import re
import sys
from pathlib import Path

import gspread
import pytz
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger("bot_raskhody")

# ---------------------------------------------------------------------------
# Конфигурация: все секреты берутся из переменных окружения (см. .env.example)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAD_API_KEY = os.getenv("CHAD_API_KEY", "")
REPORT_CHAT_ID = os.getenv("REPORT_CHAT_ID", "")  # chat_id для автоотчётов
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", str(BASE_DIR / "credentials.json"))
SHEET_NAME = os.getenv("SHEET_NAME", "Мои расходы")
LIMITS_FILE = Path(os.getenv("LIMITS_FILE", str(BASE_DIR / "limits.json")))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Krasnoyarsk")
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "21:00")  # ЧЧ:ММ по TIMEZONE

CHAD_API_URL = "https://ask.chadgpt.ru/api/public/gpt-4o-mini"
REQUEST_TIMEOUT = 30

TZ = pytz.timezone(TIMEZONE)

# ---------------------------------------------------------------------------
# Google Таблица (подключение создаётся при первом обращении)
# ---------------------------------------------------------------------------
_sheet = None


def get_sheet():
    global _sheet
    if _sheet is None:
        gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)
        _sheet = gc.open(SHEET_NAME).sheet1
    return _sheet


# ---------------------------------------------------------------------------
# Лимиты
# ---------------------------------------------------------------------------
def load_limits():
    if LIMITS_FILE.exists():
        with open(LIMITS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"daily": None, "weekly": None, "monthly": None}


def save_limits():
    LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LIMITS_FILE, "w", encoding="utf-8") as f:
        json.dump(limits, f, ensure_ascii=False, indent=2)


limits = load_limits()

scheduler = AsyncIOScheduler(timezone=TZ)


# ---------------------------------------------------------------------------
# ChadGPT
# ---------------------------------------------------------------------------
def _ask_chad(message: str):
    if not CHAD_API_KEY:
        return None
    try:
        r = requests.post(
            CHAD_API_URL,
            json={"message": message, "api_key": CHAD_API_KEY},
            timeout=REQUEST_TIMEOUT,
        )
        resp = r.json()
        if r.ok and resp.get("is_success"):
            return resp.get("response", "")
        logger.warning("ChadGPT вернул ошибку: %s", resp)
    except Exception as e:  # noqa: BLE001
        logger.warning("Ошибка запроса к ChadGPT: %s", e)
    return None


def detect_category(name: str) -> str:
    answer = _ask_chad(
        f"Определи категорию для траты '{name}' одним словом. "
        "Только: Еда, Транспорт, Одежда, Развлечения, Другое."
    )
    if answer and answer.strip():
        return answer.strip().split()[0].strip(".,!").capitalize()
    return "Другое"


def generate_advice(text: str) -> str:
    answer = _ask_chad(f"{text}\nДай один короткий и практичный совет по оптимизации расходов.")
    return answer or ""


# ---------------------------------------------------------------------------
# Разбор сообщений и отчёты
# ---------------------------------------------------------------------------
def now_local():
    return datetime.datetime.now(TZ)


def parse_expense(text: str):
    text = text.lower().replace("₽", "").replace("руб", "").strip()
    date = now_local().date()

    if text.startswith("вчера"):
        date -= datetime.timedelta(days=1)
        text = text.replace("вчера", "", 1).strip()
    elif text.startswith("сегодня"):
        text = text.replace("сегодня", "", 1).strip()

    match = re.match(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", text)
    if match:
        day, month, year = map(int, match.groups(default=str(date.year)))
        try:
            date = datetime.date(year, month, day)
        except ValueError:
            return None
        text = text[match.end():].strip()

    match = re.match(r"(.+?)\s+(\d+)", text)
    if not match:
        return None

    name = match.group(1).capitalize()
    amount = int(match.group(2))
    category = detect_category(name)
    return name, amount, category, date.strftime("%d.%m.%Y")


def add_expense(row):
    get_sheet().append_row(row)


def build_report(days=1):
    today = now_local().date()
    rows = get_sheet().get_all_values()[1:]
    filtered = []

    for row in rows:
        try:
            r_date = datetime.datetime.strptime(row[3], "%d.%m.%Y").date()
            if (today - r_date).days < days:
                filtered.append((row[0], int(row[1]), row[2]))
        except (ValueError, IndexError):
            continue

    total = sum(r[1] for r in filtered)
    categories = {}
    for _, amt, cat in filtered:
        categories[cat] = categories.get(cat, 0) + amt

    if not total:
        return "📊 Нет расходов за выбранный период.", 0, {}

    lines = [
        "📊 Расходы за сегодня" if days == 1 else f"📊 За {days} дней",
        f"Общая сумма: {total} ₽",
    ]
    for cat, amt in categories.items():
        pct = amt / total * 100
        lines.append(f"• {cat}: {amt} ₽ ({pct:.1f}%)")
    return "\n".join(lines), total, categories


def get_limit_text():
    return (
        "🔔 Текущие лимиты:\n"
        f"• Ежедневный: {limits['daily'] or 'не установлен'} ₽\n"
        f"• Еженедельный: {limits['weekly'] or 'не установлен'} ₽\n"
        f"• Ежемесячный: {limits['monthly'] or 'не установлен'} ₽"
    )


# ---------------------------------------------------------------------------
# Обработчики Telegram
# ---------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    parsed = parse_expense(msg)
    if not parsed:
        await update.message.reply_text("⚠️ Не понял. Пример: `вчера хлеб 200`", parse_mode="Markdown")
        return

    name, amount, category, date = parsed
    add_expense([name, amount, category, date])

    await update.message.reply_text(f"✅ {name} — {amount} ₽ ({category}) — {date} добавлено.")

    today_total = build_report(1)[1]
    if limits["daily"]:
        remaining = limits["daily"] - today_total
        await update.message.reply_text(
            f"📊 Сегодня потрачено: {today_total} ₽\n"
            f"🔔 Лимит: {limits['daily']} ₽\n"
            f"💸 Осталось: {remaining} ₽"
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["Расходы за сегодня", "За 7 дней"], ["За 30 дней", "Установить лимит"]]
    await update.message.reply_text(
        "👋 Привет! Я помогу вам вести учёт расходов.\n\n"
        "Просто напишите, например: `вчера хлеб 200` или `такси 350`\n\n"
        f"{get_limit_text()}\n\n"
        f"Ваш chat_id: `{update.effective_chat.id}`",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="Markdown",
    )


async def ask_limit_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Ежедневный лимит"], ["Еженедельный лимит"], ["Ежемесячный лимит"], ["🔙 Назад"]]
    await update.message.reply_text(
        get_limit_text() + "\n\nВыберите лимит, который хотите изменить:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )


async def ask_limit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if "дневн" in text:
        context.user_data["limit_type"] = "daily"
    elif "недель" in text:
        context.user_data["limit_type"] = "weekly"
    elif "месяч" in text:
        context.user_data["limit_type"] = "monthly"
    else:
        await update.message.reply_text("❌ Не удалось определить тип лимита.")
        return
    await update.message.reply_text("Введите сумму в рублях:")


async def set_limit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "limit_type" not in context.user_data:
        await update.message.reply_text("❌ Сначала выберите тип лимита.")
        return
    try:
        amount = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ Введите сумму числом, например: 3000")
        return
    limits[context.user_data["limit_type"]] = amount
    save_limits()
    await update.message.reply_text(f"✅ Лимит обновлён.\n{get_limit_text()}")
    context.user_data.pop("limit_type")


async def report_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, _, _ = build_report(1)
    await update.message.reply_text(text)


async def report_7(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, _, _ = build_report(7)
    await update.message.reply_text(text)


async def report_30(update: Update, context: ContextTypes.DEFAULT_TYPE):
    report, _, _ = build_report(30)
    advice = generate_advice(report)
    full = f"{report}\n\n💡 Совет: {advice}" if advice else report
    await update.message.reply_text(full)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Ошибка при обработке обновления: %s", context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Произошла ошибка. Попробуйте ещё раз позже.")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Автоотчёты
# ---------------------------------------------------------------------------
async def send_daily_report(app: Application):
    text, _, _ = build_report(1)
    await app.bot.send_message(chat_id=REPORT_CHAT_ID, text=f"🕒 Автоотчёт за сегодня:\n\n{text}")


async def send_weekly_report(app: Application):
    text, _, _ = build_report(7)
    await app.bot.send_message(chat_id=REPORT_CHAT_ID, text=f"📅 Автоотчёт за неделю:\n\n{text}")


async def send_monthly_report(app: Application):
    report, _, _ = build_report(30)
    advice = generate_advice(report)
    text = f"📆 Автоотчёт за месяц:\n\n{report}"
    if advice:
        text += f"\n\n💡 Совет: {advice}"
    await app.bot.send_message(chat_id=REPORT_CHAT_ID, text=text)


async def post_init(app: Application):
    if not REPORT_CHAT_ID:
        logger.warning("REPORT_CHAT_ID не задан — автоотчёты отключены. Узнать chat_id можно командой /start.")
        return

    hour, minute = (int(x) for x in DAILY_REPORT_TIME.split(":"))
    scheduler.add_job(send_daily_report, "cron", args=[app], hour=hour, minute=minute)
    scheduler.add_job(send_weekly_report, "cron", args=[app], day_of_week="fri", hour=23, minute=59)
    scheduler.add_job(send_monthly_report, "cron", args=[app], day="last", hour=23, minute=59)
    scheduler.start()
    logger.info("Автоотчёты включены: ежедневно в %s (%s), по пятницам и в последний день месяца.",
                DAILY_REPORT_TIME, TIMEZONE)


async def post_shutdown(app: Application):
    if scheduler.running:
        scheduler.shutdown(wait=False)


def check_config():
    problems = []
    if not TELEGRAM_TOKEN:
        problems.append("TELEGRAM_TOKEN не задан")
    if not CHAD_API_KEY:
        logger.warning("CHAD_API_KEY не задан — категория всегда будет «Другое», советы отключены.")
    if not Path(GOOGLE_CREDENTIALS_FILE).exists():
        problems.append(f"Файл сервисного аккаунта Google не найден: {GOOGLE_CREDENTIALS_FILE}")
    if problems:
        for p in problems:
            logger.error(p)
        logger.error("Заполните .env (см. .env.example) и положите credentials.json рядом с main.py.")
        sys.exit(1)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    check_config()

    # Проверяем доступ к таблице сразу, чтобы упасть с понятной ошибкой, а не в первом сообщении
    get_sheet()
    logger.info("Google Таблица «%s» доступна.", SHEET_NAME)

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^(Расходы за сегодня)$"), report_today))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^(За 7 дней)$"), report_7))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^(За 30 дней)$"), report_30))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("Установить лимит"), ask_limit_type))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("лимит"), ask_limit_value))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^\d+$"), set_limit_value))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("🔙 Назад"), start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("🤖 Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
