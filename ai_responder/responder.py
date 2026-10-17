"""
ИИ-автоответчик для личного Telegram-аккаунта (юзербот на Telethon).

Отвечает только тем, кого ты добавил в список, учитывая цель для каждого человека.
Управление — командами в «Избранном» (Saved Messages), см. /help.
"""
import asyncio
import json
import logging
import os
import random
import re
import time

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import User

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
AI_PROVIDER = os.getenv("AI_PROVIDER", "chadgpt")
AI_API_KEY = os.environ["AI_API_KEY"]
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
DEBOUNCE_SECONDS = float(os.getenv("DEBOUNCE_SECONDS", "8"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "30"))
TAKEOVER_MINUTES = float(os.getenv("TAKEOVER_MINUTES", "60"))

DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
HANDOFF = "[[HANDOFF]]"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("responder")

DEFAULT_PERSONA = (
    "Ты отвечаешь в Telegram от имени владельца аккаунта. Пиши по-русски, "
    "коротко и по-человечески, как в мессенджере: без канцелярита, без списков "
    "и markdown, 1–3 предложения."
)

HELP = """Команды (пиши их здесь, в Избранном):
/add @username цель — добавить человека и его цель
/goal @username цель — поменять цель
/del @username — убрать из списка
/pause @username [минуты] — заглушить для одного человека
/resume @username — снова отвечать ему
/list — кому бот отвечает
/persona текст — общая инструкция: кто ты, чем занимаешься, тон
/on, /off — включить или выключить бота целиком
Вместо @username можно указать числовой ID."""


# ---------- состояние ----------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"enabled": True, "persona": DEFAULT_PERSONA, "contacts": {}}


def save_state():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


state = load_state()


SHORT_WORDS = re.compile(r"(?<![\w@/])([а-яёА-ЯЁ]{1,2}|[—–-])[ ](?=\S)")


def nbsp(text: str) -> str:
    """Неразрывный пробел после коротких слов и перед тире — без висячих предлогов."""
    text = SHORT_WORDS.sub(lambda m: m.group(1) + "\u00a0", text)
    return text.replace(" —", "\u00a0—")


def norm_key(raw: str) -> str:
    return raw.strip().lstrip("@").lower()


def contact_for(user: User):
    """Возвращает (ключ, настройки) если человек в списке."""
    keys = [str(user.id)]
    if user.username:
        keys.append(user.username.lower())
    for k in keys:
        if k in state["contacts"]:
            return k, state["contacts"][k]
    return None, None


# ---------- нейросеть ----------

def build_system_prompt(goal: str, name: str) -> str:
    return (
        f"{state['persona']}\n\n"
        f"Собеседник: {name}.\n"
        f"Твоя цель в этом диалоге: {goal}\n\n"
        "Правила:\n"
        "- Веди разговор к цели мягко, не дави и не повторяйся.\n"
        "- Не выдумывай факты, цены, сроки и обещания, которых нет в инструкции.\n"
        "- Если прямо спросят, бот ли ты, честно скажи, что это ИИ-ассистент владельца.\n"
        f"- Если вопрос требует решения владельца или ты не знаешь ответа, "
        f"ответь ровно {HANDOFF} и ничего больше.\n"
        "- Отвечай только текстом следующего сообщения, без пометок вроде «Я:»."
    )


async def ask_ai(system: str, history: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=60) as http:
        if AI_PROVIDER == "openai":
            r = await http.post(
                f"{AI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {AI_API_KEY}"},
                json={
                    "model": AI_MODEL,
                    "messages": [{"role": "system", "content": system}, *history],
                    "temperature": 0.7,
                },
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()

        # ChadGPT принимает одно поле message, поэтому склеиваем всё в один текст
        transcript = "\n".join(
            f"{'Я' if m['role'] == 'assistant' else 'Собеседник'}: {m['content']}"
            for m in history
        )
        prompt = (
            f"{system}\n\nПереписка (последнее сообщение внизу):\n{transcript}\n\n"
            "Напиши мой следующий ответ."
        )
        r = await http.post(
            f"https://ask.chadgpt.ru/api/public/{AI_MODEL}",
            json={"message": prompt, "api_key": AI_API_KEY},
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("is_success"):
            raise RuntimeError(f"ChadGPT error: {data}")
        return data["response"].strip()


# ---------- телеграм ----------

os.makedirs(DATA_DIR, exist_ok=True)
client = TelegramClient(os.path.join(DATA_DIR, "account"), API_ID, API_HASH)
me: User | None = None
pending: dict[int, asyncio.Task] = {}   # chat_id -> отложенный ответ
bot_sent_ids: set[int] = set()          # id сообщений, отправленных ботом
bot_sending: set[int] = set()           # чаты, куда бот прямо сейчас отправляет
takeover_until: dict[int, float] = {}   # chat_id -> до какого времени молчать


def describe(msg) -> str:
    if msg.text:
        return msg.text
    if msg.photo:
        return "[фото]"
    if msg.voice:
        return "[голосовое сообщение]"
    if msg.video or msg.video_note:
        return "[видео]"
    if msg.sticker:
        return "[стикер]"
    if msg.document:
        return "[файл]"
    return "[сообщение]"


async def load_history(chat_id: int) -> list[dict]:
    msgs = await client.get_messages(chat_id, limit=HISTORY_LIMIT)
    history = []
    for m in reversed(msgs):
        role = "assistant" if m.out else "user"
        content = describe(m)
        if history and history[-1]["role"] == role:
            history[-1]["content"] += "\n" + content
        else:
            history.append({"role": role, "content": content})
    return history


async def reply_later(chat_id: int, user: User, key: str):
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        contact = state["contacts"].get(key)
        if not contact or not state["enabled"]:
            return

        history = await load_history(chat_id)
        if not history or history[-1]["role"] != "user":
            return  # последнее слово уже за нами

        name = " ".join(filter(None, [user.first_name, user.last_name])) or key
        answer = await ask_ai(build_system_prompt(contact["goal"], name), history)
        await client.send_read_acknowledge(chat_id)

        if HANDOFF in answer:
            await client.send_message(
                "me",
                nbsp(f"⚠️ Нужен ты: @{user.username or user.id} ждёт ответа. "
                "Последнее сообщение:\n") + history[-1]["content"],
            )
            return

        # имитация набора текста
        async with client.action(chat_id, "typing"):
            await asyncio.sleep(min(2 + len(answer) / 25, 12) + random.uniform(0, 2))
        bot_sending.add(chat_id)
        try:
            sent = await client.send_message(chat_id, answer)
            bot_sent_ids.add(sent.id)
        finally:
            bot_sending.discard(chat_id)
        log.info("Ответил %s: %s", key, answer[:80])
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("Не удалось ответить %s", key)
    finally:
        if pending.get(chat_id) is asyncio.current_task():
            pending.pop(chat_id, None)


@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def on_incoming(event):
    if not state["enabled"]:
        return
    user = await event.get_sender()
    if not isinstance(user, User) or user.bot:
        return
    key, contact = contact_for(user)
    if not contact:
        return
    if contact.get("paused_until", 0) > time.time():
        return
    if takeover_until.get(event.chat_id, 0) > time.time():
        return

    # человек может написать несколько сообщений подряд — ждём, пока допишет
    if task := pending.get(event.chat_id):
        task.cancel()
    pending[event.chat_id] = asyncio.create_task(reply_later(event.chat_id, user, key))


@client.on(events.NewMessage(outgoing=True, func=lambda e: e.is_private))
async def on_outgoing(event):
    if event.chat_id == me.id:
        await handle_command(event)
        return
    if event.id in bot_sent_ids or event.chat_id in bot_sending:
        return
    # ты написал сам — бот не мешает
    takeover_until[event.chat_id] = time.time() + TAKEOVER_MINUTES * 60
    if task := pending.pop(event.chat_id, None):
        task.cancel()


async def handle_command(event):
    text = (event.raw_text or "").strip()
    if not text.startswith("/"):
        return
    cmd, _, rest = text.partition(" ")
    cmd = cmd.lower()
    arg, _, tail = rest.strip().partition(" ")
    key = norm_key(arg) if arg else ""
    contacts = state["contacts"]

    if cmd == "/help":
        out = HELP
    elif cmd in ("/add", "/goal"):
        if not key or not tail.strip():
            out = f"Формат: {cmd} @username цель"
        else:
            contacts.setdefault(key, {})["goal"] = tail.strip()
            contacts[key].pop("paused_until", None)
            out = f"✅ @{key}: {tail.strip()}"
    elif cmd == "/del":
        out = f"🗑 @{key} удалён" if contacts.pop(key, None) else "Такого нет в списке"
    elif cmd == "/pause":
        if key not in contacts:
            out = "Такого нет в списке"
        else:
            minutes = float(tail) if tail.strip() else 60 * 24 * 365
            contacts[key]["paused_until"] = time.time() + minutes * 60
            out = f"⏸ @{key} на паузе"
    elif cmd == "/resume":
        if key not in contacts:
            out = "Такого нет в списке"
        else:
            contacts[key].pop("paused_until", None)
            out = f"▶️ @{key} снова на автоответе"
    elif cmd == "/list":
        if not contacts:
            out = "Список пуст. Добавь: /add @username цель"
        else:
            now = time.time()
            lines = [
                f"{'⏸' if c.get('paused_until', 0) > now else '▶️'} @{k}: {c['goal']}"
                for k, c in contacts.items()
            ]
            out = f"Бот {'включён' if state['enabled'] else 'выключен'}\n\n" + "\n".join(lines)
    elif cmd == "/persona":
        if rest.strip():
            state["persona"] = rest.strip()
            out = "✅ Инструкция обновлена"
        else:
            out = f"Текущая инструкция:\n{state['persona']}"
    elif cmd in ("/on", "/off"):
        state["enabled"] = cmd == "/on"
        out = "Бот включён" if state["enabled"] else "Бот выключен"
    else:
        return

    save_state()
    await event.reply(nbsp(out))


async def main():
    global me
    await client.start()  # при первом запуске спросит телефон и код
    me = await client.get_me()
    log.info("Вошёл как @%s. Команды — в Избранном, начни с /help", me.username)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
