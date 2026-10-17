"""
ИИ-автоответчик для личного Telegram-аккаунта (юзербот на Telethon).

Отвечает только тем, кого ты добавил в список, учитывая цель для каждого человека.
Управление — командами в «Избранном» (Saved Messages), см. /help.
"""
from __future__ import annotations

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

load_dotenv(os.getenv("ENV_FILE", ".env"))

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
AI_PROVIDER = os.getenv("AI_PROVIDER", "chadgpt")
AI_API_KEY = os.environ["AI_API_KEY"]
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
DEBOUNCE_SECONDS = float(os.getenv("DEBOUNCE_SECONDS", "8"))
# пауза перед ответом, секунды: обычно от MIN до MAX, иногда «отвлёкся» до LONG_PAUSE_MAX
REPLY_DELAY_MIN = float(os.getenv("REPLY_DELAY_MIN", "20"))
REPLY_DELAY_MAX = float(os.getenv("REPLY_DELAY_MAX", "180"))
LONG_PAUSE_CHANCE = float(os.getenv("LONG_PAUSE_CHANCE", "0.15"))
LONG_PAUSE_MAX = float(os.getenv("LONG_PAUSE_MAX", "600"))
# если переписка живая (отвечали недавно), отвечаем быстрее
ACTIVE_CHAT_SECONDS = 300
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "30"))
TAKEOVER_MINUTES = float(os.getenv("TAKEOVER_MINUTES", "60"))

DATA_DIR = os.getenv("DATA_DIR", "data")
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
/memory @username — что бот помнит о человеке
/forget @username — стереть эту память
/persona текст — общая инструкция: кто ты, чем занимаешься, тон
/delay 20 180 — пауза перед ответом, от и до, в секундах
/style текст — как ты пишешь (коротко, с маленькой буквы, без точек…)
/example текст — добавить пример твоего сообщения, можно несколько строк сразу
/examples — показать примеры; /examples clear — удалить их
/model — какая нейросеть отвечает; /model название — сменить её
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

EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]+")


def strip_smileys(text: str) -> str:
    """Убирает скобки-смайлики «)))» и эмодзи, но не настоящие скобки."""
    text = EMOJI.sub("", text)
    if text.count(")") > text.count("("):
        text = re.sub(r"\s*(?:[:;]-?)?[)(]+(?=\s|$|[.,!?])", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def style_block() -> str:
    out = ""
    if state.get("style"):
        out += f"Как пишет владелец (пиши именно так, это главное):\n{state['style']}\n\n"
    if state.get("style_examples"):
        out += "Примеры его настоящих сообщений:\n" + "\n".join(
            f"— {e}" for e in state["style_examples"][-40:]) + "\n\n"
    return out


def build_system_prompt(goal: str, name: str, memory: str = "") -> str:
    notes = f"Что ты уже знаешь об этом собеседнике из прошлых разговоров:\n{memory}\n\n" if memory else ""
    return (
        f"{state['persona']}\n\n"
        f"{style_block()}"
        f"Собеседник: {name}.\n"
        f"Твоя цель в этом диалоге: {goal}\n\n"
        f"{notes}"
        "Правила:\n"
        "- Веди разговор к цели мягко, не дави и не повторяйся.\n"
        "- Не выдумывай факты, цены, сроки и обещания, которых нет в инструкции.\n"
        "- Если прямо спросят, бот ли ты, честно скажи, что это ИИ-ассистент владельца.\n"
        f"- Если вопрос требует решения владельца или ты не знаешь ответа, "
        f"ответь ровно {HANDOFF} и ничего больше.\n"
        "- Отвечай только текстом следующего сообщения, без пометок вроде «Я:».\n"
        "- Сообщения владельца в этой переписке — образец его манеры: длина, "
        "заглавные буквы, знаки препинания, словечки. Пиши так же.\n"
        "- Никаких скобок-смайликов «)» «))» и эмодзи.\n"
        "- Не звучи как ассистент: без «Конечно!», «Отличный вопрос», «Буду рад помочь».\n"
        "- Как живой человек в мессенджере, можешь разбить ответ на 2–3 коротких "
        "сообщения, разделив их пустой строкой. Не делай так каждый раз."
    )


MEMORY_PROMPT = (
    "Ты ведёшь заметки о собеседнике владельца Telegram-аккаунта, чтобы помнить "
    "контекст между разговорами. Обнови заметки по свежей переписке: кто этот человек, "
    "что ему нужно, что уже обсудили, о чём договорились (даты, суммы, обещания), "
    "на каком этапе цель и что важно не забыть. Пиши кратко, пунктами, не больше 15 пунктов. "
    "Старые факты сохраняй, если переписка их не опровергает. Выведи только заметки."
)


async def update_memory(key: str, goal: str, history: list[dict]):
    contact = state["contacts"].get(key)
    if not contact:
        return
    transcript = "\n".join(
        f"{'Владелец' if m['role'] == 'assistant' else 'Собеседник'}: {m['content']}" for m in history
    )
    request = (
        f"Цель владельца в диалоге: {goal}\n\n"
        f"Прежние заметки:\n{contact.get('memory') or '(пока пусто)'}\n\n"
        f"Свежая переписка:\n{transcript}"
    )
    try:
        notes = await ask_ai(MEMORY_PROMPT, [{"role": "user", "content": request}])
    except Exception:
        log.exception("Не удалось обновить память %s", key)
        return
    if key in state["contacts"]:
        state["contacts"][key]["memory"] = notes.strip()
        save_state()


async def ask_ai(system: str, history: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=60) as http:
        if AI_PROVIDER == "openai":
            r = await http.post(
                f"{AI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {AI_API_KEY}"},
                json={
                    "model": state.get("model") or AI_MODEL,
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
last_incoming: dict[int, float] = {}    # chat_id -> когда собеседник писал последний раз
last_reply: dict[int, float] = {}       # chat_id -> когда бот отвечал последний раз


def human_delay(chat_id: int) -> float:
    """Сколько «не замечать» сообщение: каждый раз по-разному, как у живого человека."""
    lo = state.get("delay_min", REPLY_DELAY_MIN)
    hi = state.get("delay_max", REPLY_DELAY_MAX)
    if time.time() - last_reply.get(chat_id, 0) < ACTIVE_CHAT_SECONDS:
        # уже переписываемся — отвечаем быстрее, но тоже не мгновенно
        return random.uniform(5, max(10, lo * 1.5))
    if random.random() < LONG_PAUSE_CHANCE:
        return random.uniform(hi, max(hi, LONG_PAUSE_MAX))
    # короткие паузы чаще длинных
    return lo + (hi - lo) * random.random() ** 2


def typing_seconds(text: str) -> float:
    # на телефоне люди набирают примерно 4–7 символов в секунду
    return max(2.0, min(len(text) / random.uniform(4, 7), 45))


def split_parts(answer: str) -> list[str]:
    parts = [p.strip() for p in answer.split("\n\n") if p.strip()]
    if len(parts) > 3:
        parts = parts[:2] + ["\n\n".join(parts[2:])]
    return parts or [answer]


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
        delay = human_delay(chat_id)
        log.info("Отвечу %s примерно через %d с", key, delay)
        await asyncio.sleep(delay)
        # если человек ещё пишет, даём ему договорить
        while time.time() - last_incoming.get(chat_id, 0) < DEBOUNCE_SECONDS:
            await asyncio.sleep(DEBOUNCE_SECONDS)

        contact = state["contacts"].get(key)
        if not contact or not state["enabled"]:
            return

        history = await load_history(chat_id)
        if not history or history[-1]["role"] != "user":
            return  # последнее слово уже за нами

        name = " ".join(filter(None, [user.first_name, user.last_name])) or key
        system = build_system_prompt(contact["goal"], name, contact.get("memory", ""))
        await client.send_read_acknowledge(chat_id)
        await asyncio.sleep(random.uniform(2, 8))  # прочитал, думает
        answer = strip_smileys(await ask_ai(system, history))
        if not answer:
            return

        if HANDOFF in answer:
            await client.send_message(
                "me",
                nbsp(f"⚠️ Нужен ты: @{user.username or user.id} ждёт ответа. "
                "Последнее сообщение:\n") + history[-1]["content"],
            )
            return

        for i, part in enumerate(split_parts(answer)):
            if i:
                await asyncio.sleep(random.uniform(1, 4))
            async with client.action(chat_id, "typing"):
                await asyncio.sleep(typing_seconds(part))
            bot_sending.add(chat_id)
            try:
                sent = await client.send_message(chat_id, part)
                bot_sent_ids.add(sent.id)
            finally:
                bot_sending.discard(chat_id)
        last_reply[chat_id] = time.time()
        log.info("Ответил %s: %s", key, answer[:80])
        history.append({"role": "assistant", "content": answer})
        asyncio.create_task(update_memory(key, contact["goal"], history))
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

    last_incoming[event.chat_id] = time.time()
    # ответ уже запланирован — он сам подождёт, пока человек допишет
    if event.chat_id not in pending:
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
    cmd, rest = (re.split(r"\s", text, maxsplit=1) + [""])[:2]
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
    elif cmd == "/memory":
        if key not in contacts:
            out = "Такого нет в списке"
        else:
            out = f"🧠 @{key}:\n{contacts[key].get('memory') or 'пока ничего не запомнил'}"
    elif cmd == "/forget":
        if key not in contacts:
            out = "Такого нет в списке"
        else:
            contacts[key].pop("memory", None)
            out = f"🧹 Память о @{key} стёрта"
    elif cmd == "/delay":
        nums = rest.split()
        if len(nums) == 2 and all(n.isdigit() for n in nums) and int(nums[0]) <= int(nums[1]):
            state["delay_min"], state["delay_max"] = float(nums[0]), float(nums[1])
            out = f"✅ Пауза перед ответом: от {nums[0]} до {nums[1]} с, иногда дольше"
        else:
            lo = state.get("delay_min", REPLY_DELAY_MIN)
            hi = state.get("delay_max", REPLY_DELAY_MAX)
            out = f"Сейчас: от {lo:.0f} до {hi:.0f} с. Поменять: /delay 20 180"
    elif cmd == "/style":
        if rest.strip():
            state["style"] = rest.strip()
            out = "✅ Стиль запомнил"
        else:
            out = f"Твой стиль:\n{state.get('style') or 'не задан'}\n\nЗадать: /style текст"
    elif cmd == "/example":
        lines = [l.strip() for l in rest.splitlines() if l.strip()]
        if not lines:
            out = "Формат: /example твоё сообщение (можно несколько строк)"
        else:
            state.setdefault("style_examples", []).extend(lines)
            state["style_examples"] = state["style_examples"][-80:]
            out = f"✅ Добавил {len(lines)}, всего примеров: {len(state['style_examples'])}"
    elif cmd == "/examples":
        if rest.strip().lower() == "clear":
            state["style_examples"] = []
            out = "🧹 Примеры удалены"
        else:
            ex = state.get("style_examples", [])
            out = "\n".join(f"— {e}" for e in ex[-40:]) or "Примеров пока нет. Добавь: /example текст"
    elif cmd == "/model":
        if rest.strip():
            state["model"] = rest.strip()
            out = f"✅ Модель: {state['model']}"
        else:
            out = (f"Сейчас: {state.get('model') or AI_MODEL}\n"
                   "Например: /model anthropic/claude-opus-5.5")
    elif cmd in ("/on", "/off"):
        state["enabled"] = cmd == "/on"
        out = "Бот включён" if state["enabled"] else "Бот выключен"
    else:
        return

    save_state()
    await event.reply(nbsp(out))


async def main():
    global me
    await client.connect()
    if not await client.is_user_authorized():
        # на сервере без терминала вход делается через login.py
        await client.start()
    me = await client.get_me()
    log.info("Вошёл как @%s. Команды — в Избранном, начни с /help", me.username)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
