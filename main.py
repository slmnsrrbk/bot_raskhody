import re
import json
import os
import datetime
import logging
import gspread
import pytz
import requests
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import CommandHandler, MessageHandler, Application, filters, CallbackContext
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Токены и ключи
TELEGRAM_TOKEN = "8116816516:AAEmHIdS9tNQGhNVO83HF3fm_yVnY8RszZk"
CHAD_API_KEY = "chad-9814409421bc4afda8cb736d7d3403f4de4qu6jf"

# Google Таблица
gc = gspread.service_account(filename="credentials.json")
sheet = gc.open("Мои расходы").sheet1

# Было:
# limits = {
#     "daily": None,
#     "weekly": None,
#     "monthly": None
# }

LIMITS_FILE = "limits.json"

def load_limits():
    if os.path.exists(LIMITS_FILE):
        with open(LIMITS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"daily": None, "weekly": None, "monthly": None}

def save_limits():
    with open(LIMITS_FILE, "w", encoding="utf-8") as f:
        json.dump(limits, f, ensure_ascii=False, indent=2)
        
# Стало:
limits = load_limits()

# Планировщик
scheduler = AsyncIOScheduler()

def detect_category(name: str) -> str:
    data = {
        "message": f"Определи категорию для траты '{name}' одним словом. Только: Еда, Транспорт, Одежда, Развлечения, Другое.",
        "api_key": CHAD_API_KEY
    }
    try:
        r = requests.post("https://ask.chadgpt.ru/api/public/gpt-4o-mini", json=data)
        resp = r.json()
        if resp.get("is_success"):
            return resp["response"].strip().split()[0].capitalize()
    except:
        pass
    return "Другое"

def generate_advice(text: str) -> str:
    prompt = f"{text}\nДай один короткий и практичный совет по оптимизации расходов."
    data = {"message": prompt, "api_key": CHAD_API_KEY}
    try:
        r = requests.post("https://ask.chadgpt.ru/api/public/gpt-4o-mini", json=data)
        if r.ok and r.json().get("is_success"):
            return r.json()["response"]
    except:
        pass
    return ""

def parse_expense(text: str):
    text = text.lower().replace("₽", "").replace("руб", "").strip()
    date = datetime.datetime.now(pytz.timezone("Asia/Krasnoyarsk")).date()

    if text.startswith("вчера"):
        date -= datetime.timedelta(days=1)
        text = text.replace("вчера", "", 1).strip()
    elif text.startswith("сегодня"):
        text = text.replace("сегодня", "", 1).strip()

    match = re.match(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", text)
    if match:
        day, month, year = map(int, match.groups(default=str(date.year)))
        date = datetime.date(year, month, day)
        text = text[match.end():].strip()

    match = re.match(r"(.+?)\s+(\d+)", text)
    if not match:
        return None

    name = match.group(1).capitalize()
    amount = int(match.group(2))
    category = detect_category(name)
    return name, amount, category, date.strftime("%d.%m.%Y")

def add_expense(row):
    sheet.append_row(row)

def build_report(days=1):
    today = datetime.datetime.now(pytz.timezone("Asia/Krasnoyarsk")).date()
    rows = sheet.get_all_values()[1:]
    filtered = []

    for row in rows:
        try:
            r_date = datetime.datetime.strptime(row[3], "%d.%m.%Y").date()
            if (today - r_date).days < days:
                filtered.append((row[0], int(row[1]), row[2]))
        except:
            continue

    total = sum(r[1] for r in filtered)
    categories = {}
    for _, amt, cat in filtered:
        categories[cat] = categories.get(cat, 0) + amt

    if not total:
        return "📊 Нет расходов за выбранный период.", 0, {}

    lines = [f"📊 Расходы за {days} день" if days == 1 else f"📊 За {days} дней", f"Общая сумма: {total} ₽"]
    for cat, amt in categories.items():
        pct = amt / total * 100
        lines.append(f"• {cat}: {amt} ₽ ({pct:.1f}%)")
    return "\n".join(lines), total, categories

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    parsed = parse_expense(msg)
    if not parsed:
        await update.message.reply_text("⚠️ Не понял. Пример: `вчера хлеб 200`", parse_mode="Markdown")
        return

    name, amount, category, date = parsed
    add_expense([name, amount, category, date])

    await update.message.reply_text(f"✅ {name} — {amount} ₽ ({category}) — {date} добавлено.")

    # Показываем расходы за сегодня и лимит
    today_total = build_report(1)[1]
    if limits['daily']:
        remaining = limits['daily'] - today_total
        await update.message.reply_text(
            f"📊 Сегодня потрачено: {today_total} ₽\n"
            f"🔔 Лимит: {limits['daily']} ₽\n"
            f"💸 Осталось: {remaining} ₽"
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["Расходы за сегодня", "За 7 дней"], ["За 30 дней", "Установить лимит"]]

    # Получаем текст с лимитами
    limit_text = (
        f"🔔 Текущие лимиты:\n"
        f"• Ежедневный: {limits['daily'] or 'не установлен'} ₽\n"
        f"• Еженедельный: {limits['weekly'] or 'не установлен'} ₽\n"
        f"• Ежемесячный: {limits['monthly'] or 'не установлен'} ₽"
    )

    await update.message.reply_text(
        "Дорово! Я помогу вам вести учёт расходов.\n\n"
        "Просто напишите, например: `вчера хлеб 200` или `такси 350`\n\n"
        f"{limit_text}\n\n",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="Markdown"
    )

def get_limit_text():
    return (
        f"🔔 Текущие лимиты:\n"
        f"• Ежедневный: {limits['daily'] or 'не установлен'} ₽\n"
        f"• Еженедельный: {limits['weekly'] or 'не установлен'} ₽\n"
        f"• Ежемесячный: {limits['monthly'] or 'не установлен'} ₽"
    )

async def ask_limit_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Ежедневный лимит"], ["Еженедельный лимит"], ["Ежемесячный лимит"], ["🔙 Назад"]]
    await update.message.reply_text(
        get_limit_text() + "\n\nВыберите лимит, который хотите изменить:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
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
        limits[context.user_data["limit_type"]] = amount
        save_limits()
        await update.message.reply_text(f"✅ Лимит обновлён.\n{get_limit_text()}")
        context.user_data.pop("limit_type")
    except:
        await update.message.reply_text("⚠️ Введите сумму числом, например: 3000")

async def report_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, _, _ = build_report(1)
    await update.message.reply_text(text)

async def report_7(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, _, _ = build_report(7)
    await update.message.reply_text(text)

async def report_30(update: Update, context: ContextTypes.DEFAULT_TYPE):
    report, _, _ = build_report(30)
    advice = generate_advice(report)
    full = f"{report}\n\n💡 Совет: {advice}"
    await update.message.reply_text(full)

async def post_init(app: Application):
    scheduler.add_job(
        lambda: app.create_task(send_daily_report(app)),
        "cron",
        hour=17,
        minute=54,
        timezone="Asia/Krasnoyarsk"
    )

    scheduler.add_job(
        lambda: app.create_task(send_weekly_report(app)),
        "cron",
        day_of_week='fri',
        hour=23,
        minute=59,
        timezone="Asia/Krasnoyarsk"
    )

    scheduler.add_job(
        lambda: app.create_task(send_monthly_report(app)),
        "cron",
        day='last',
        hour=23,
        minute=59,
        timezone="Asia/Krasnoyarsk"
    )

    scheduler.start()
    
async def send_daily_report(app: Application):
    chat_id = YOUR_CHAT_ID  # ← Подставьте ваш chat_id
    text, _, _ = build_report(1)
    await app.bot.send_message(chat_id=chat_id, text=f"🕒 Автоотчёт за сегодня:\n\n{text}")

async def send_weekly_report(app: Application):
    chat_id = YOUR_CHAT_ID
    text, _, _ = build_report(7)
    await app.bot.send_message(chat_id=chat_id, text=f"📅 Автоотчёт за неделю:\n\n{text}")

async def send_monthly_report(app: Application):
    chat_id = YOUR_CHAT_ID
    report, _, _ = build_report(30)
    advice = generate_advice(report)
    await app.bot.send_message(chat_id=chat_id, text=f"📆 Автоотчёт за месяц:\n\n{report}\n\n💡 Совет: {advice}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^(Расходы за сегодня)$"), report_today))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^(За 7 дней)$"), report_7))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^(За 30 дней)$"), report_30))
    app.add_handler(MessageHandler(filters.Regex("Установить лимит"), ask_limit_type))
    app.add_handler(MessageHandler(filters.Regex("лимит"), ask_limit_value))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^\d+$"), set_limit_value))
    app.add_handler(MessageHandler(filters.Regex("🔙 Назад"), start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Бот запущен.")
    app.run_polling()

