"""
ИИ-автоответчик для личного Telegram-аккаунта (юзербот на Telethon).

Отвечает только тем, кого ты добавил в список, учитывая цель для каждого человека.
Управление — командами в «Избранном» (Saved Messages), см. /help.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import os
import random
import re
import time

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events, utils
from telethon.tl.functions.channels import (
    CreateChannelRequest, CreateForumTopicRequest, EditAdminRequest, InviteToChannelRequest,
    TogglePreHistoryHiddenRequest)
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import ChatAdminRights, MessageActionTopicCreate, UpdateNewChannelMessage, MessageEntityMentionName, MessageEntityTextUrl, MessageEntityUrl, User

load_dotenv(os.getenv("ENV_FILE", ".env"))

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
AI_PROVIDER = os.getenv("AI_PROVIDER", "chadgpt")
AI_API_KEY = os.environ["AI_API_KEY"]
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_FALLBACK_MODEL = os.getenv("AI_FALLBACK_MODEL", "anthropic/claude-haiku-5.5")
AI_STT_MODEL = os.getenv("AI_STT_MODEL", "openai/gpt-4o-mini-transcribe")
# основной аккаунт, с которого ты управляешь ботом (по умолчанию — Избранное)
CONTROL_USER = os.getenv("CONTROL_USER", "").strip().lstrip("@")
DEBOUNCE_SECONDS = float(os.getenv("DEBOUNCE_SECONDS", "8"))
# всё, что меняется командой /set: ключ -> (по умолчанию, описание)
SETTINGS = {
    "pause_min": (20, "пауза перед ответом, от, сек"),
    "pause_max": (180, "пауза перед ответом, до, сек"),
    "fast_min": (5, "в живой переписке пауза от, сек"),
    "fast_max": (30, "в живой переписке пауза до, сек"),
    "active_min": (5, "сколько минут после ответа переписка считается живой"),
    "long_chance": (0, "как часто ответить позже обычного, % (0 = никогда)"),
    "long_max": (600, "такая поздняя пауза до, сек"),
    "typing_speed": (5, "скорость набора, символов в секунду"),
    "history": (100, "сколько последних сообщений переписки читать перед ответом"),
    "lock_min": (30, "через сколько минут снова закрыть группу после кодового слова (0 = не закрывать)"),
    "quiet_min": (0, "сколько минут молчать в чате, если ты написал сам (0 = не молчать)"),
    "nudge_after_min": (180, "через сколько минут тишины предложить вопрос (0 = выключено)"),
    "nudge_max": (1, "сколько раз подряд можно напомнить о себе без ответа"),
    "nudge_from": (10, "предлагать вопросы не раньше этого часа"),
    "nudge_to": (22, "и не позже этого часа"),
    "tz": (7, "твой часовой пояс, UTC+"),
}


def setting(key: str) -> float:
    return float(state.get("settings", {}).get(key, SETTINGS[key][0]))

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
/reply @username [контекст] — ответить прямо сейчас, сразу отправит. Например /reply @anya предложи субботу в 15
/nudge @username [контекст] — придумать сообщение прямо сейчас и прислать тебе на согласование
/lockmode on, /lockmode off — включить или выключить замок на группу (по умолчанию выключен)
/lockword слово — задать кодовое слово для группы (сообщение сразу удалится); потом просто пиши это слово, чтобы открыть
/lock — закрыть группу сейчас (она пропадёт из чатов); /set lock_min 30 — через сколько минут закрывать самой
/group — создать группу с темой на каждый диалог; /group off — перестать дублировать
/control @username — присылать всё на твой основной аккаунт и принимать команды оттуда; /control off — обратно в Избранное
/matchbot @имя — из какого бота знакомств ловить взаимные лайки (сейчас Дайвинчик @leomatchbot)
/matchgoal текст — цель по умолчанию для новых знакомств оттуда
/first @username [подсказка] — придумать первое сообщение, например /first @anya познакомились на выставке
/ok номер, /no номер — согласовать предложенный вопрос (или ответь на него «да» / «нет» / своим текстом)
/undo @username [сколько] — удалить у обоих последнее сообщение (или несколько), отправленное этому человеку
/show @username [сколько] — прислать тебе последние сообщения переписки (по умолчанию 10), с кружками и голосовыми
/memory @username — что бот помнит о человеке
/forget @username — стереть эту память
/persona текст — общая инструкция: кто ты, чем занимаешься, тон
/delay 20 180 — пауза перед ответом, от и до, в секундах
/settings — все паузы и их значения
/set ключ число — поменять, например /set fast_max 60
/style текст — как ты пишешь (коротко, с маленькой буквы, без точек…)
/example текст — добавить пример твоего сообщения, можно несколько строк сразу
/examples — показать примеры; /examples clear — удалить их
/lessons — что бот вынес из прошлых разговоров; /learn — обновить сейчас; /lessons clear — стереть; /lessons off, /lessons on — не учитывать или снова учитывать
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
    raw = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", raw.strip())
    return raw.lstrip("@").lower()


def contact_for(user: User):
    """Возвращает (ключ, настройки) если человек в списке."""
    if user.id == state.get("control_id"):
        return None, None  # основной аккаунт — это ты, а не собеседник
    keys = [str(user.id)]
    if user.username:
        keys.append(user.username.lower())
    for k in keys:
        if k in state["contacts"]:
            return k, state["contacts"][k]
    for k, c in state["contacts"].items():
        if c.get("id") == user.id:  # юзернейм поменялся, а id тот же
            return k, c
    return None, None


# ---------- нейросеть ----------

EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]+")


# «О,», «Ого!», «Вау» и прочее показное удивление в начале фразы
INTERJECTION = re.compile(
    r"(^|(?<=[.!?\n])\s*)(о+|ого+|оу|ох|ах|вау|ух ты|ничего себе|ого-го|воу)\s*[,!.…]+\s*",
    re.IGNORECASE)


def clean_reply(text: str) -> str:
    """Чистит ответ: смайлики, показное удивление, лишние восклицательные знаки."""
    text = re.sub(r"^\s*\[(сегодня|вчера|\d\d\.\d\d), \d\d:\d\d\]\s*", "", text)
    text = re.sub(r"^\s*\(в ответ на «[^»]*»\)\s*", "", text)
    text = re.sub(r"\(написал владелец сам\)\s*", "", text)
    text = INTERJECTION.sub(lambda m: m.group(1), strip_smileys(text))
    text = re.sub(r"!{2,}", "!", text)
    return text.strip()


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
    if state.get("lessons") and not state.get("lessons_off"):
        out += f"Выводы из прошлых разговоров (общие правила, соблюдай их):\n{state['lessons']}\n\n"
    return out


def remember_owner_text(text: str, draft: str = ""):
    """Твои настоящие сообщения — лучшие примеры стиля; правки бота — материал для выводов."""
    text = text.strip()
    if not text or text.startswith("/") or len(text) > 300:
        return
    examples = state.setdefault("style_examples", [])
    if text not in examples:
        examples.append(text)
        state["style_examples"] = examples[-80:]
    if draft and draft.strip() != text:
        state.setdefault("corrections", []).append({"bot": draft.strip(), "owner": text})
        state["corrections"] = state["corrections"][-50:]
    save_state()


LEARN_PROMPT = (
    "Ты помогаешь ассистенту, который переписывается в Telegram от имени владельца, становиться лучше. "
    "Ниже: заметки по разным диалогам (чем всё закончилось), правки владельца (что предлагал бот и как "
    "владелец написал на самом деле) и прежние выводы. Сформулируй общие правила общения: что работает, "
    "что нет, как владелец пишет иначе, чем бот. Только обобщения, без имён, мест, дат и личных фактов "
    "о конкретных людях. Не больше 12 коротких пунктов. Старые выводы сохраняй, если новые данные их не "
    "опровергают. Выведи только пункты."
)


async def learn() -> str:
    notes = [c["memory"] for c in state["contacts"].values() if c.get("memory")]
    fixes = state.get("corrections", [])
    if not notes and not fixes:
        return "Пока не на чем учиться: нужны разговоры или твои правки"
    parts = ["Прежние выводы:\n" + (state.get("lessons") or "(пока нет)")]
    if notes:
        parts.append("Заметки по диалогам:\n" + "\n---\n".join(notes))
    if fixes:
        parts.append("Правки владельца:\n" + "\n".join(f"бот: {f['bot']}\nвладелец: {f['owner']}" for f in fixes))
    lessons = (await ask_ai(LEARN_PROMPT, [{"role": "user", "content": "\n\n".join(parts)}])).strip()
    if lessons:
        state["lessons"] = lessons
        state["lessons_at"] = time.time()
        save_state()
        log.info("Обновил выводы: %d диалогов, %d правок", len(notes), len(fixes))
    return f"🧠 Выводы из разговоров:\n{state.get('lessons') or 'пока нет'}"


def build_system_prompt(goal: str, name: str, memory: str = "") -> str:
    notes = f"Что ты уже знаешь об этом собеседнике из прошлых разговоров:\n{memory}\n\n" if memory else ""
    return (
        f"{state['persona']}\n\n"
        f"{style_block()}"
        f"Собеседник: {name}.\n"
        f"Твоя цель в этом диалоге: {goal}\n\n"
        f"{notes}"
        "Правила:\n"
        "- Сообщения с пометкой «(написал владелец сам)» владелец написал лично. Они главнее всего: "
        "никогда им не противоречь, не меняй договорённости, время, места, планы и факты о нём, "
        "которые он назвал, продолжай в их русле. Если не уверен, как это согласуется, ответь "
        f"{HANDOFF}. Пометку сам не пиши.\n"
        "- Не противоречь и своим прошлым сообщениям в этой переписке.\n"
        "- Перед ответом перечитай всю переписку целиком, а не только последнее сообщение: "
        "что уже обсудили, о чём договорились, на какие вопросы уже ответили. Не переспрашивай "
        "известное и не повторяй то, что уже писал.\n"
        "- Если собеседник написал несколько сообщений подряд, ответь на всё важное в них, "
        "а не только на последнее. Если он ответил на твой вопрос, учти ответ.\n"
        "- Метки времени в квадратных скобках показывают паузы в разговоре; сам их не пиши.\n"
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
        "- Реагируй спокойно, как взрослый человек. Не удивляйся на каждую мелочь и не начинай "
        "с междометий: никаких «О,», «Ого», «Вау», «Ух ты», «Ничего себе». Не повторяй за "
        "собеседником его слова с восторгом. Восклицательный знак максимум один и редко.\n"
        "- Не задавай вопрос в каждом сообщении: иногда просто отреагируй или поделись своим.\n"
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
            models = [state.get("model") or AI_MODEL, AI_FALLBACK_MODEL]
            last_error = None
            for attempt in range(4):
                # сервис иногда отвечает 5xx — ждём и пробуем снова, в конце запасной моделью
                model = models[1] if attempt == 3 and models[1] else models[0]
                try:
                    r = await http.post(
                        f"{AI_BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {AI_API_KEY}"},
                        json={
                            "model": model,
                            "messages": [{"role": "system", "content": system}, *history],
                            "temperature": 0.7,
                        },
                    )
                    if r.status_code < 500 and r.status_code != 429:
                        r.raise_for_status()
                        return r.json()["choices"][0]["message"]["content"].strip()
                    last_error = f"{model}: HTTP {r.status_code}"
                except httpx.TransportError as e:
                    last_error = f"{model}: {e!r}"
                log.warning("Нейросеть не ответила (%s), попытка %d", last_error, attempt + 1)
                await asyncio.sleep(5 * (attempt + 1))
            raise RuntimeError(f"Нейросеть недоступна: {last_error}")

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
    """Случайная пауза строго в заданном диапазоне, каждый раз новая."""
    if time.time() - last_reply.get(chat_id, 0) < setting("active_min") * 60:
        lo, hi = setting("fast_min"), setting("fast_max")
    elif random.random() * 100 < setting("long_chance"):
        lo, hi = setting("pause_max"), setting("long_max")
    else:
        lo, hi = setting("pause_min"), setting("pause_max")
    return random.uniform(min(lo, hi), max(lo, hi))


def typing_seconds(text: str) -> float:
    speed = max(1.0, setting("typing_speed")) * random.uniform(0.75, 1.3)
    return max(2.0, min(len(text) / speed, 45))


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


def control_id() -> int | None:
    return state.get("control_id")


def group_id() -> int | None:
    return state.get("group_id")


async def notify(text: str, key: str | None = None):
    """Пишет тебе: в личку на основной аккаунт; без него — в группу или в Избранное."""
    if control_id():
        return await client.send_message(control_id(), text)
    if group_id():
        contact = state["contacts"].get(key) if key else None
        tid = await topic_for(key, contact) if contact else None
        try:
            return await client.send_message(group_id(), text, reply_to=tid)
        except Exception:
            log.exception("Не удалось написать в группу")
    return await client.send_message(control_id() or "me", text)


def contact_by_id(uid: int):
    for k, c in state["contacts"].items():
        if c.get("id") == uid:
            return k, c
    return None, None


def contact_by_topic(tid: int):
    for k, c in state["contacts"].items():
        if c.get("topic") == tid:
            return k, c
    return None, None


async def topic_for(key: str, contact: dict) -> int | None:
    """Тема в группе для этого человека; создаёт её при первом сообщении."""
    if not group_id() or not contact:
        return None
    if contact.get("topic"):
        return contact["topic"]
    title = f"{contact.get('name') or key} (@{key})"[:120]
    res = await client(CreateForumTopicRequest(channel=group_id(), title=title, random_id=random.getrandbits(62)))
    for u in res.updates:
        if isinstance(u, UpdateNewChannelMessage) and isinstance(u.message.action, MessageActionTopicCreate):
            contact["topic"] = u.message.id
    save_state()
    if contact.get("topic"):
        intro = f"Цель: {contact['goal']}"
        if contact.get("memory"):
            intro += f"\n\nЧто бот уже знает:\n{contact['memory']}"
        await client.send_message(group_id(), intro, reply_to=contact["topic"])
    return contact.get("topic")


async def mirror(key: str, contact: dict, text: str, media_msg=None):
    """Дублирует сообщение в тему человека; без группы кружки, голосовые и фото идут тебе в личку."""
    tid = await topic_for(key, contact)
    has_media = media_msg is not None and media_msg.media and not media_msg.web_preview
    if tid:
        target = group_id()
    elif has_media and control_id():
        target, text = control_id(), f"@{key} · {text}"
    else:
        return
    try:
        if has_media:
            caption = "" if media_msg.video_note else text[:1000]
            await client.send_file(target, media_msg.media, caption=caption, reply_to=tid,
                                   voice_note=bool(media_msg.voice), video_note=bool(media_msg.video_note))
            if media_msg.video_note and text:
                # у кружков нет подписи — расшифровку присылаем отдельным сообщением
                await client.send_message(target, text, reply_to=tid)
        else:
            await client.send_message(target, text, reply_to=tid)
    except Exception:
        log.exception("Не удалось продублировать в группу")


async def show_dialog(key: str, contact: dict, n: int):
    """Присылает тебе последние сообщения переписки: текстом, а кружки, голосовые и фото — файлами."""
    msgs = list(reversed(await client.get_messages(contact["id"], limit=n)))
    name = contact.get("name") or key
    lines = []
    for m in msgs:
        who = ("🤖" if m.id in bot_sent_ids else "Ты") if m.out else name
        text = describe(m)
        if (m.voice or m.video_note) and (heard := await transcribe(m)):
            text = f"{'🎤' if m.voice else '🎥'} {heard}"
        lines.append(f"[{when(m.date)}] {who}: {text}")
    await notify(f"📜 Последние {len(msgs)} сообщений с @{key}:\n\n" + "\n".join(lines)[:3900], key=key)
    for m in msgs:
        if not m.out and (m.voice or m.video_note or m.photo or m.video):
            target = group_id() or control_id() or "me"
            tid = contact.get("topic") if group_id() else None
            await client.send_file(target, m.media, caption="" if m.video_note else f"{name}, {when(m.date)}", reply_to=tid,
                                   voice_note=bool(m.voice), video_note=bool(m.video_note))


OWNER_RIGHTS = dict(change_info=True, delete_messages=True, ban_users=True, invite_users=True,
                    pin_messages=True, manage_topics=True)
lock_task: asyncio.Task | None = None


def word_hash(word: str) -> str:
    return hashlib.sha256(("ai_responder:" + word.strip().lower()).encode()).hexdigest()


async def lock_group() -> str:
    """Убирает тебя из группы — она пропадает из списка чатов, переписка в ней остаётся."""
    if not group_id() or not control_id():
        return "Группы нет"
    owner = await client.get_input_entity(control_id())
    try:
        await client(EditAdminRequest(group_id(), owner, ChatAdminRights(), rank=""))
    except Exception:
        pass
    try:
        await client.kick_participant(group_id(), owner)
    except Exception as e:
        log.exception("Не удалось закрыть группу")
        return f"⚠️ Не смог закрыть группу: {e}"
    state["group_locked"] = True
    save_state()
    return "🔒 Группа закрыта"


async def unlock_group() -> str:
    global lock_task
    owner = await client.get_input_entity(control_id())
    try:
        await client(TogglePreHistoryHiddenRequest(group_id(), enabled=False))  # видна вся история
    except Exception:
        pass
    await client(InviteToChannelRequest(group_id(), [owner]))
    await client(EditAdminRequest(group_id(), owner, ChatAdminRights(**OWNER_RIGHTS), rank="владелец"))
    state["group_locked"] = False
    save_state()
    if lock_task:
        lock_task.cancel()
    minutes = setting("lock_min") if state.get("lock_enabled") else 0
    if minutes > 0:
        async def relock():
            await asyncio.sleep(minutes * 60)
            await lock_group()
        lock_task = asyncio.create_task(relock())
        return f"🔓 Группа открыта на {minutes:g} мин"
    return "🔓 Группа открыта"


async def say_and_vanish(event, text: str, after: int = 30):
    """Отвечает и через полминуты стирает и свой ответ, и твоё сообщение."""
    reply = await client.send_message(event.chat_id, nbsp(text))
    async def vanish():
        await asyncio.sleep(after)
        try:
            await client.delete_messages(event.chat_id, [reply.id], revoke=True)
        except Exception:
            pass
    asyncio.create_task(vanish())


async def setup_group() -> str:
    if group_id():
        return "Группа уже есть. Чтобы создать новую: /group off, потом /group"
    if not control_id():
        return "Сначала задай основной аккаунт: /control @username"
    res = await client(CreateChannelRequest(
        title="Переписки", about="Каждая тема — диалог. Пиши в тему, и сообщение уйдёт человеку.",
        megagroup=True, forum=True))
    chan = res.chats[0]
    state["group_id"] = utils.get_peer_id(chan)
    save_state()
    owner = await client.get_input_entity(control_id())
    try:
        await client(InviteToChannelRequest(chan, [owner]))
        await client(EditAdminRequest(chan, owner, ChatAdminRights(**OWNER_RIGHTS), rank="владелец"))
        await client(TogglePreHistoryHiddenRequest(chan, enabled=False))
        joined = "Добавил тебя в группу «Переписки»."
    except Exception:
        log.exception("Не удалось добавить в группу")
        link = (await client(ExportChatInviteRequest(chan))).link
        joined = f"Не смог добавить тебя сам, вот ссылка на группу «Переписки»: {link}"
    for k, c in state["contacts"].items():
        if c.get("id"):
            c.pop("topic", None)
            await topic_for(k, c)
    await client.send_message(group_id(), nbsp(
        "Здесь общая тема: сюда приходят команды и уведомления. У каждого человека своя тема. "
        "Пиши в неё текстом или голосовым, и это уйдёт ему от имени аккаунта. Ответы бота помечены 🤖"))
    return joined


async def transcribe(msg) -> str | None:
    """Голосовое → текст через Polza (OpenAI-совместимый /audio/transcriptions)."""
    if AI_PROVIDER != "openai" or not (msg.voice or msg.video_note):
        return None
    cache = state.setdefault("voices", {})
    key = f"{msg.chat_id}:{msg.id}"
    if key in cache:
        return cache[key]
    try:
        audio = await client.download_media(msg, bytes)
        async with httpx.AsyncClient(timeout=90) as http:
            r = await http.post(
                f"{AI_BASE_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {AI_API_KEY}"},
                files={"file": ("voice.ogg", audio, "audio/ogg") if msg.voice else ("note.mp4", audio, "video/mp4")},
                data={"model": AI_STT_MODEL, "language": "ru", "response_format": "json"},
            )
        r.raise_for_status()
        text = (r.json().get("text") or "").strip()
    except Exception:
        log.exception("Не удалось распознать голосовое")
        return None
    if text:
        cache[key] = text
        if len(cache) > 500:
            for old in list(cache)[:100]:
                cache.pop(old)
        save_state()
    return text or None


def when(dt: datetime.datetime) -> str:
    """Дата по твоему времени: «сегодня, 21:40», «вчера, 21:40», «12.10, 21:40»."""
    tz = datetime.timezone(datetime.timedelta(hours=setting("tz")))
    local, today = dt.astimezone(tz), datetime.datetime.now(tz).date()
    day = {0: "сегодня", 1: "вчера"}.get((today - local.date()).days, local.strftime("%d.%m"))
    return f"{day}, {local:%H:%M}"


async def load_history(chat_id: int) -> list[dict]:
    """Вся недавняя переписка: с датами после пауз и с тем, на что отвечали цитатой."""
    msgs = await client.get_messages(chat_id, limit=int(setting("history")))
    by_id = {m.id: m for m in msgs}
    history = []
    prev = None
    for m in reversed(msgs):
        role = "assistant" if m.out else "user"
        content = describe(m)
        if (m.voice or m.video_note) and (heard := await transcribe(m)):
            content = f"[{'голосовое' if m.voice else 'кружок'}] {heard}"
        if written_by_owner(chat_id, m):
            content = f"(написал владелец сам) {content}"
        if len(content) > 1500:
            content = content[:1500] + "…"
        quoted = by_id.get(m.reply_to_msg_id) if m.reply_to_msg_id else None
        if quoted:
            content = f"(в ответ на «{describe(quoted)[:120]}») {content}"
        if m.date and (prev is None or (m.date - prev).total_seconds() > 3600):
            content = f"[{when(m.date)}] {content}"
        prev = m.date or prev
        if history and history[-1]["role"] == role:
            history[-1]["content"] += "\n" + content
        else:
            history.append({"role": role, "content": content})
    return history


def mark_bot_msg(chat_id: int, msg_id: int):
    msgs = state.setdefault("bot_msgs", {}).setdefault(str(chat_id), [])
    msgs.append(msg_id)
    del msgs[:-300]
    save_state()


def written_by_bot(chat_id: int, msg) -> bool:
    return msg.id in bot_sent_ids or msg.id in state.get("bot_msgs", {}).get(str(chat_id), [])


def written_by_owner(chat_id: int, msg) -> bool:
    """Исходящее, которое писал ты сам. Старые сообщения (до учёта) не размечаем."""
    since = state.get("bot_msgs_since", time.time())
    return msg.out and not written_by_bot(chat_id, msg) and msg.date and msg.date.timestamp() >= since


async def you_wrote_last(chat_id: int, first: bool) -> bool:
    """Последнее сообщение в чате — твоё (а не бота)? Тогда поверх ничего не пишем."""
    msgs = await client.get_messages(chat_id, limit=1)
    if not msgs or not msgs[0].out:
        return False
    # перед первым сообщением последним должен быть собеседник; дальше — наша же предыдущая часть
    return first or msgs[0].id not in bot_sent_ids


async def send_text(chat_id: int, text: str, forced: bool = False, by_owner: bool = False) -> bool:
    """Отправляет от твоего имени: «печатает…», затем сообщение (или 2–3 подряд).
    Не пишет поверх тебя: если последним в чате стало твоё сообщение, останавливается."""
    for i, part in enumerate(split_parts(text)):
        if i:
            await asyncio.sleep(random.uniform(1, 4))
        async with client.action(chat_id, "typing"):
            await asyncio.sleep(typing_seconds(part))
        if (i or not forced) and await you_wrote_last(chat_id, first=not i):
            log.info("Не отправляю в %s: последним написал ты", chat_id)
            return i > 0
        bot_sending.add(chat_id)
        try:
            sent = await client.send_message(chat_id, part)
            bot_sent_ids.add(sent.id)
            if not by_owner:
                mark_bot_msg(chat_id, sent.id)
        finally:
            bot_sending.discard(chat_id)
        key, contact = contact_by_id(chat_id)
        if contact:
            await mirror(key, contact, f"🤖 {part}")
    last_reply[chat_id] = time.time()
    return True


NUDGE_PROMPT = (
    "Разговор затих: собеседник давно не отвечает. Напиши одно короткое сообщение, "
    "которое естественно продолжит разговор: уточняющий вопрос по теме последних сообщений "
    "или лёгкий вопрос, который приближает к цели. Не упрекай за молчание, не пиши "
    "«ты пропал(а)?», не повторяй вопросы, которые уже задавал. Выведи только текст сообщения."
)
YES = {"да", "+", "ок", "ok", "давай", "го", "отправь", "отправляй", "yes", "ага"}
NO = {"нет", "-", "не", "не надо", "no", "неа", "отмена"}


def local_hour() -> int:
    return int((time.gmtime().tm_hour + setting("tz")) % 24)


async def propose_nudge(key: str, contact: dict, force: bool = False, hint: str = "") -> str | None:
    """Готовит вопрос для затихшего разговора и присылает его тебе на согласование."""
    cid = contact.get("id")
    msgs = await client.get_messages(cid, limit=1)
    if not msgs:
        return "С этим человеком ещё нет переписки"
    last = msgs[0]
    if not last.out and not force:
        return None  # ждём не мы, а нас — это обычный ответ
    history = await load_history(cid)
    silence = (time.time() - last.date.timestamp()) / 3600
    note = f" Владелец просит учесть: {hint}" if hint else ""
    history.append({"role": "user", "content": f"[служебно: собеседник молчит {silence:.0f} ч. Напиши сообщение-продолжение.{note}]"})
    system = build_system_prompt(contact["goal"], key, contact.get("memory", "")) + "\n\n" + NUDGE_PROMPT
    text = clean_reply(await ask_ai(system, history))
    if not text or HANDOFF in text:
        return "Не придумал, что написать: тут лучше решить тебе"
    contact["nudged_msg"] = last.id
    await offer(key, cid, text, last.id, f"@{key} молчит {silence:.0f} ч. Предлагаю написать:")
    return None


async def offer(key: str, cid: int, text: str, last_id: int, headline: str):
    """Присылает тебе в Избранное готовое сообщение; уйдёт оно только после твоего «да»."""
    state["nudge_seq"] = state.get("nudge_seq", 0) + 1
    nid = str(state["nudge_seq"])
    head = nbsp(f"💬 №{nid}. {headline}")
    tail = nbsp(f"Напиши «да», чтобы отправить, или «нет». Свой вариант — ответом на это сообщение. "
                f"Если предложений несколько: /ok {nid} или /no {nid}")
    sent = await notify(f"{head}\n\n{text}\n\n{tail}", key=key)
    state.setdefault("nudges", {})[nid] = dict(
        key=key, chat_id=cid, text=text, msg_id=sent.id, last_id=last_id, status="wait", created=time.time())
    save_state()
    log.info("Предложил сообщение №%s для %s", nid, key)


FIRST_PROMPT = (
    "Нужно написать этому человеку первым и начать разговор. Придумай одно короткое "
    "первое сообщение: живое, без шаблонов вроде «Привет, как дела?» и без подкатов. "
    "Зацепись за подсказку владельца, если она есть, и мягко веди к цели. "
    "Если переписка уже была, учти её и не делай вид, что вы незнакомы. Выведи только текст сообщения."
)


async def propose_first(key: str, contact: dict, hint: str = "") -> str | None:
    """Придумывает первое сообщение и присылает тебе на согласование."""
    cid = contact["id"]
    msgs = await client.get_messages(cid, limit=1)
    history = await load_history(cid) if msgs else []
    note = f" Подсказка владельца: {hint}" if hint else ""
    history.append({"role": "user", "content": f"[служебно: напиши первое сообщение.{note}]"})
    system = build_system_prompt(contact["goal"], key, contact.get("memory", "")) + "\n\n" + FIRST_PROMPT
    text = clean_reply(await ask_ai(system, history))
    if not text or HANDOFF in text:
        return "Не придумал, с чего начать: подскажи, например /first @" + key + " познакомились на выставке"
    await offer(key, cid, text, msgs[0].id if msgs else 0, f"Первое сообщение для @{key}:")
    return None


async def resolve_nudge(nid: str, approve: bool, custom: str = "") -> str:
    n = state.get("nudges", {}).get(nid)
    if not n or n["status"] != "wait":
        return "Это предложение уже неактуально"
    msgs = await client.get_messages(n["chat_id"], limit=1)
    if msgs and msgs[0].id != n["last_id"]:
        n["status"] = "stale"
        return f"Не отправляю: в чате с @{n['key']} уже появились новые сообщения"
    if not approve:
        n["status"] = "no"
        return "Ок, не пишу"
    text = custom.strip() or n["text"]
    if custom.strip():
        remember_owner_text(custom, draft=n["text"])
    await send_text(n["chat_id"], text, forced=True, by_owner=bool(custom.strip()))
    n["status"] = "sent"
    contact = state["contacts"].get(n["key"])
    if contact:
        contact["nudges_in_row"] = contact.get("nudges_in_row", 0) + 1
    log.info("Отправил согласованный вопрос №%s для %s", nid, n["key"])
    return f"✅ Отправил @{n['key']}"


async def check_nudges():
    if not state["enabled"] or setting("nudge_after_min") <= 0:
        return
    if not setting("nudge_from") <= local_hour() < setting("nudge_to"):
        return
    waiting = {n["chat_id"] for n in state.get("nudges", {}).values() if n["status"] == "wait"}
    for key, c in list(state["contacts"].items()):
        cid = c.get("id")
        if not cid or cid == control_id() or cid in pending or cid in waiting or c.get("paused_until", 0) > time.time():
            continue
        if c.get("nudges_in_row", 0) >= setting("nudge_max"):
            continue
        msgs = await client.get_messages(cid, limit=1)
        if not msgs or not msgs[0].out or c.get("nudged_msg") == msgs[0].id:
            continue
        if time.time() - msgs[0].date.timestamp() < setting("nudge_after_min") * 60:
            continue
        await propose_nudge(key, c)


async def nudge_loop():
    while True:
        await asyncio.sleep(300)
        try:
            await check_nudges()
        except Exception:
            log.exception("Не удалось проверить затихшие разговоры")
        if not state.get("lessons_off") and time.time() - state.get("lessons_at", 0) > 86400:
            try:
                await learn()
            except Exception:
                log.exception("Не удалось обновить выводы")


async def reply_later(chat_id: int, user: User, key: str, now: bool = False, hint: str = ""):
    try:
        delay = random.uniform(2, 6) if now else human_delay(chat_id)
        log.info("Отвечу %s примерно через %d с", key, delay)
        await asyncio.sleep(delay)
        # если человек ещё пишет, даём ему договорить
        while time.time() - last_incoming.get(chat_id, 0) < DEBOUNCE_SECONDS:
            await asyncio.sleep(DEBOUNCE_SECONDS)

        contact = state["contacts"].get(key)
        if not contact or not state["enabled"]:
            return

        history = await load_history(chat_id)
        if not history or (history[-1]["role"] != "user" and not now):
            return  # последнее слово уже за нами

        name = " ".join(filter(None, [user.first_name, user.last_name])) or key
        system = build_system_prompt(contact["goal"], name, contact.get("memory", ""))
        if hint:
            system += f"\n\nУказание владельца для этого сообщения (учти, но не цитируй): {hint}"
        if history[-1]["role"] != "user":
            # последним писали мы — просят написать ещё раз, самим
            history.append({"role": "user", "content": "[служебно: напиши следующее сообщение сам, продолжи разговор]"})
        await client.send_read_acknowledge(chat_id)
        await asyncio.sleep(random.uniform(2, 8))  # прочитал, думает
        answer = clean_reply(await ask_ai(system, history))
        if not answer:
            return

        if HANDOFF in answer:
            await notify(key=key, text=
                nbsp(f"⚠️ Нужен ты: @{user.username or user.id} ждёт ответа. "
                "Последнее сообщение:\n") + history[-1]["content"],
            )
            return

        if not await send_text(chat_id, answer, forced=now):
            return
        log.info("Ответил %s: %s", key, answer[:80])
        history.append({"role": "assistant", "content": answer})
        real = [m for m in history if not m["content"].startswith("[служебно")]
        asyncio.create_task(update_memory(key, contact["goal"], real))
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.exception("Не удалось ответить %s", key)
        try:
            await notify(nbsp(f"⚠️ Не смог ответить @{key}: {e}. Ответь сам или /reply @{key}"), key=key)
        except Exception:
            pass
    finally:
        if pending.get(chat_id) is asyncio.current_task():
            pending.pop(chat_id, None)


DEFAULT_MATCH_GOAL = "познакомиться поближе и договориться о встрече"
USER_LINK = re.compile(r"(?:https?://)?t\.me/([A-Za-z]\w{3,31})|tg://user\?id=(\d+)|@([A-Za-z]\w{3,31})")


def match_bots() -> set[str]:
    return set(state.get("match_bots", ["leomatchbot"]))


async def find_person(msg):
    """Достаёт человека из сообщения бота знакомств: упоминание, ссылка t.me или tg://user."""
    for ent in msg.entities or []:
        if isinstance(ent, MessageEntityMentionName):
            return await client.get_entity(ent.user_id)
    links = [e.url for e in msg.entities or [] if isinstance(e, MessageEntityTextUrl)]
    links += [msg.raw_text[e.offset:e.offset + e.length] for e in msg.entities or [] if isinstance(e, MessageEntityUrl)]
    for text in links + [msg.raw_text or ""]:
        for m in USER_LINK.finditer(text):
            name, uid, at = m.groups()
            target = int(uid) if uid else (name or at)
            if (name or at or "").lower() in match_bots():
                continue
            try:
                ent = await client.get_entity(target)
            except Exception:
                continue
            if isinstance(ent, User) and not ent.bot:
                return ent
    return None


@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def on_match(event):
    sender = await event.get_sender()
    if not isinstance(sender, User) or not sender.bot or (sender.username or "").lower() not in match_bots():
        return
    person = await find_person(event.message)
    if not person:
        return
    if any(c.get("id") == person.id for c in state["contacts"].values()):
        return
    who = " ".join(filter(None, [person.first_name, person.last_name])) or "без имени"
    handle = f"@{person.username}" if person.username else f"id {person.id}"
    head = nbsp(f"❤️ Взаимный лайк: {who} ({handle}).")
    tail = nbsp("Ответь на это сообщение «да», и я добавлю человека и предложу первое сообщение. "
                "Можно ответить своей целью вместо «да». «Нет», если не интересно.")
    sent = await notify(f"{head}\n\n{tail}")
    state.setdefault("matches", {})[str(sent.id)] = dict(
        user_id=person.id, username=person.username, name=who, status="wait", created=time.time())
    save_state()
    log.info("Взаимный лайк: %s", handle)


async def accept_match(mid: str, goal: str) -> str | None:
    m = state["matches"][mid]
    if m["status"] != "wait":
        return "Уже обработано"
    m["status"] = "added"
    key = (m.get("username") or str(m["user_id"])).lower()
    state["contacts"][key] = {"goal": goal or state.get("match_goal", DEFAULT_MATCH_GOAL), "id": m["user_id"]}
    save_state()
    if not m.get("username"):
        try:
            await client.get_entity(m["user_id"])
        except Exception:
            return f"Добавил {m['name']}, но без юзернейма не могу написать первым. Напиши сам, дальше бот подхватит"
    return await propose_first(key, state["contacts"][key], "познакомились в Дайвинчике, взаимный лайк")


@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def on_incoming(event):
    user = await event.get_sender()
    if control_id() and event.chat_id == control_id():
        await handle_command(event)  # пишешь с основного аккаунта — это команды
        return
    if not isinstance(user, User) or user.bot:
        return
    key, contact = contact_for(user)
    if not contact:
        return
    name = " ".join(filter(None, [user.first_name, user.last_name])) or key
    if contact.get("name") != name:
        contact["name"] = name
    if group_id() or (control_id() and event.message.media):
        msg = event.message
        text = msg.raw_text or ""
        if (msg.voice or msg.video_note) and (heard := await transcribe(msg)):
            text = f"{'🎤' if msg.voice else '🎥'} {heard}"
        await mirror(key, contact, f"{name}: {text}" if text else name, msg if msg.media else None)
    if not state["enabled"]:
        return
    if contact.get("paused_until", 0) > time.time():
        log.info("Пропускаю %s: на паузе", key)
        return
    if takeover_until.get(event.chat_id, 0) > time.time():
        log.info("Пропускаю %s: ты недавно писал сам", key)
        return

    last_incoming[event.chat_id] = time.time()
    if contact.pop("nudges_in_row", None):
        save_state()
    # ответ уже запланирован — он сам подождёт, пока человек допишет
    if event.chat_id not in pending:
        pending[event.chat_id] = asyncio.create_task(reply_later(event.chat_id, user, key))


def is_proposal(mid) -> bool:
    return str(mid) in state.get("matches", {}) or any(
        n["msg_id"] == mid for n in state.get("nudges", {}).values())


@client.on(events.NewMessage(incoming=True, func=lambda e: bool(group_id()) and e.chat_id == group_id()))
async def on_group(event):
    """Пишешь в тему — сообщение уходит человеку от имени аккаунта."""
    if event.sender_id != control_id():
        return
    text = event.raw_text or ""
    rt = event.message.reply_to
    tid = (rt.reply_to_top_id or rt.reply_to_msg_id) if rt and rt.forum_topic else None
    if text.startswith("/") or not tid or (rt and is_proposal(rt.reply_to_msg_id)):
        await handle_command(event)
        return
    key, contact = contact_by_topic(tid)
    if not contact or not contact.get("id"):
        return
    cid = contact["id"]
    if task := pending.pop(cid, None):
        task.cancel()  # ты отвечаешь сам — бот не дублирует
    bot_sending.add(cid)
    try:
        msg = event.message
        if msg.media and not msg.web_preview:
            sent = await client.send_file(cid, msg.media, caption=text, voice_note=bool(msg.voice),
                                         video_note=bool(msg.video_note))
        else:
            sent = await client.send_message(cid, text)
            remember_owner_text(text)
        bot_sent_ids.add(sent.id)
    except Exception as e:
        log.exception("Не удалось отправить из группы")
        await event.reply(f"⚠️ Не отправилось: {e}")
    finally:
        bot_sending.discard(cid)
    last_reply[cid] = time.time()
    if setting("quiet_min") > 0:
        takeover_until[cid] = time.time() + setting("quiet_min") * 60


@client.on(events.NewMessage(outgoing=True, func=lambda e: e.is_private))
async def on_outgoing(event):
    if event.chat_id == me.id:
        await handle_command(event)
        return
    if event.id in bot_sent_ids or event.chat_id in bot_sending or event.chat_id == control_id():
        return
    key, contact = contact_by_id(event.chat_id)
    if contact:
        await mirror(key, contact, f"✍️ {event.raw_text or ''}", event.message if event.message.media else None)
    # ты написал сам — бот не перебивает
    if setting("quiet_min") > 0:
        takeover_until[event.chat_id] = time.time() + setting("quiet_min") * 60
    if task := pending.pop(event.chat_id, None):
        task.cancel()


async def handle_command(event):
    text = (event.raw_text or "").strip()
    if event.is_reply and text and not text.startswith("/"):
        mid = str(event.reply_to_msg_id)
        if mid in state.get("matches", {}):
            word = text.lower().strip(" .!")
            if word in NO:
                state["matches"][mid]["status"] = "no"
                out = "Ок, пропускаю"
            else:
                out = await accept_match(mid, "" if word in YES else text)
            save_state()
            if out:
                await event.reply(nbsp(out))
            return
        nid = next((k for k, n in state.get("nudges", {}).items() if n["msg_id"] == event.reply_to_msg_id), None)
        if nid:
            word = text.lower().strip(" .!")
            if word in YES:
                out = await resolve_nudge(nid, True)
            elif word in NO:
                out = await resolve_nudge(nid, False)
            else:
                out = await resolve_nudge(nid, True, custom=text)
            save_state()
            await event.reply(nbsp(out))
        return
    if (state.get("lock_enabled") and state.get("lock_hash") and text and not text.startswith("/")
            and word_hash(text) == state["lock_hash"]):
        try:
            await client.delete_messages(event.chat_id, [event.id], revoke=True)
        except Exception:
            pass
        if not group_id():
            await say_and_vanish(event, "Группы ещё нет: /group")
        else:
            await say_and_vanish(event, await unlock_group())
        return
    word = text.lower().strip(" .!")
    if word in YES or word in NO:
        # «да» / «нет» без ответа на сообщение — к последнему предложению
        waiting = [(n["created"], "nudge", k) for k, n in state.get("nudges", {}).items() if n["status"] == "wait"]
        waiting += [(m["created"], "match", k) for k, m in state.get("matches", {}).items() if m["status"] == "wait"]
        if not waiting:
            await event.reply("Сейчас нечего подтверждать")
            return
        _, kind, k = max(waiting)
        if kind == "match":
            if word in NO:
                state["matches"][k]["status"] = "no"
                out = "Ок, пропускаю"
            else:
                out = await accept_match(k, "")
        else:
            out = await resolve_nudge(k, word in YES)
        save_state()
        if out:
            await event.reply(nbsp(out))
        return
    if not text.startswith("/"):
        return
    cmd, rest = (re.split(r"\s", text, maxsplit=1) + [""])[:2]
    cmd = cmd.lower()
    log.info("Команда: %s", cmd)
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
            try:
                ent = await client.get_entity(int(key) if key.isdigit() else key)
                contacts[key]["id"] = ent.id
                takeover_until.pop(ent.id, None)
                who = " ".join(filter(None, [ent.first_name, ent.last_name]))
                contacts[key]["name"] = who or key
                out = f"✅ @{key} ({who}): {tail.strip()}"
                if group_id():
                    await topic_for(key, contacts[key])
            except Exception:
                out = (f"⚠️ Добавил @{key}, но в Telegram такого не нашёл. "
                       "Нужен именно @username из профиля человека, не имя. Если ошибся, /del и добавь заново")
    elif cmd == "/undo":
        if key not in contacts or not contacts[key].get("id"):
            out = f"Такого нет в списке: @{key or 'username'}"
        else:
            cid = contacts[key]["id"]
            n = min(int(tail) if tail.strip().isdigit() else 1, 20)
            if task := pending.pop(cid, None):
                task.cancel()  # и не отправлять то, что бот ещё только собирался написать
            mine = [m for m in await client.get_messages(cid, limit=50) if m.out][:n]
            if not mine:
                out = f"Нечего удалять: в чате с @{key} нет твоих сообщений"
            else:
                await client.delete_messages(cid, [m.id for m in mine], revoke=True)
                gone = "\n".join(f"— {describe(m)[:100]}" for m in reversed(mine))
                out = f"🗑 Удалил у обоих в чате с @{key}:\n{gone}"
    elif cmd == "/show":
        if key not in contacts or not contacts[key].get("id"):
            out = f"Такого нет в списке. Добавь: /add @{key or 'username'} цель"
        else:
            n = min(int(tail) if tail.strip().isdigit() else 10, 50)
            await show_dialog(key, contacts[key], n)
            return
    elif cmd == "/reply":
        if key not in contacts:
            out = f"Сначала добавь: /add @{key or 'username'} цель"
        else:
            try:
                ent = await client.get_entity(contacts[key].get("id") or (int(key) if key.isdigit() else key))
                takeover_until.pop(ent.id, None)
                if task := pending.pop(ent.id, None):
                    task.cancel()
                hint = tail.strip()
                pending[ent.id] = asyncio.create_task(reply_later(ent.id, ent, key, now=True, hint=hint))
                out = f"✍️ Отвечаю @{key}" + (f", учитываю: {hint}" if hint else "")
            except Exception:
                out = f"⚠️ Не нашёл @{key} в Telegram"
    elif cmd in ("/ok", "/no"):
        out = await resolve_nudge(arg, cmd == "/ok") if arg else f"Формат: {cmd} номер"
    elif cmd == "/nudge":
        if key not in contacts or not contacts[key].get("id"):
            out = f"Сначала добавь: /add @{key or 'username'} цель"
        else:
            await event.reply("Придумываю…")
            out = await propose_nudge(key, contacts[key], force=True, hint=tail.strip())
            if out is None:
                return
    elif cmd == "/group":
        if key == "off":
            state.pop("group_id", None)
            for c in contacts.values():
                c.pop("topic", None)
            out = "✅ Группа отключена, уведомления снова в личку"
        else:
            out = await setup_group()
    elif cmd == "/lockword":
        try:
            await client.delete_messages(event.chat_id, [event.id], revoke=True)
        except Exception:
            pass
        if not rest.strip():
            await say_and_vanish(event, "Формат: /lockword слово")
        else:
            state["lock_hash"] = word_hash(rest)
            save_state()
            if not state.get("lock_enabled"):
                note = "Замок сейчас выключен, включить: /lockmode on"
            else:
                note = await lock_group() if group_id() else "Группы пока нет, создай её: /group"
            await say_and_vanish(event, f"✅ Кодовое слово сохранено. {note}. Чтобы открыть, просто напиши это слово")
        return
    elif cmd == "/lock":
        out = await lock_group() if state.get("lock_enabled") else "Замок выключен. Включить: /lockmode on"
    elif cmd == "/lockmode":
        if key == "on":
            state["lock_enabled"] = True
            if not state.get("lock_hash"):
                out = "✅ Замок включён. Задай кодовое слово: /lockword слово"
            else:
                out = "✅ Замок включён. " + (await lock_group() if group_id() else "Группы пока нет: /group")
        elif key == "off":
            state["lock_enabled"] = False
            if lock_task:
                lock_task.cancel()
            out = "✅ Замок выключен"
            if group_id() and state.get("group_locked"):
                out += ". " + (await unlock_group())
        else:
            out = f"Замок {'включён' if state.get('lock_enabled') else 'выключен'}. /lockmode on или /lockmode off"
    elif cmd == "/control":
        if key in ("off", "me"):
            state.pop("control_id", None)
            out = "✅ Управление снова через Избранное"
        elif key:
            try:
                ent = await client.get_entity(int(key) if key.isdigit() else key)
                state["control_id"] = ent.id
                out = f"✅ Управление теперь с @{key}"
                await client.send_message(ent.id, "Привет, теперь я пишу сюда. Команды присылай в этот чат, начни с /help")
            except Exception:
                out = f"⚠️ Не нашёл @{key} в Telegram"
        else:
            out = f"Сейчас управление: {'основной аккаунт, id ' + str(control_id()) if control_id() else 'Избранное'}"
    elif cmd == "/matchbot":
        if key:
            state["match_bots"] = [key]
            out = f"✅ Ловлю взаимные лайки из @{key}"
        else:
            out = "Сейчас: " + ", ".join(f"@{b}" for b in match_bots())
    elif cmd == "/matchgoal":
        if rest.strip():
            state["match_goal"] = rest.strip()
            out = "✅ Цель для новых знакомств запомнил"
        else:
            out = f"Сейчас: {state.get('match_goal', DEFAULT_MATCH_GOAL)}"
    elif cmd == "/first":
        if key not in contacts or not contacts[key].get("id"):
            out = f"Сначала добавь: /add @{key or 'username'} цель"
        else:
            await event.reply("Придумываю…")
            out = await propose_first(key, contacts[key], tail.strip())
            if out is None:
                return
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
            takeover_until.pop(contacts[key].get("id"), None)
            out = f"▶️ @{key} снова на автоответе"
    elif cmd == "/list":
        if not contacts:
            out = "Список пуст. Добавь: /add @username цель"
        else:
            now = time.time()
            lines = []
            for k, c in contacts.items():
                quiet = takeover_until.get(c.get("id"), 0) - now
                paused = c.get("paused_until", 0) > now
                line = f"{'⏸' if paused else '🤫' if quiet > 0 else '▶️'} @{k}: {c['goal']}"
                if not paused and quiet > 0:
                    line += f"\n   молчит ещё {quiet / 60:.0f} мин, ты писал сам. Снять: /resume @{k}"
                if not c.get("id"):
                    line += "\n   ⚠️ не нашёл в Telegram, проверь @username"
                lines.append(line)
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
            state.setdefault("settings", {}).update(pause_min=float(nums[0]), pause_max=float(nums[1]))
            out = f"✅ Пауза перед ответом: случайно от {nums[0]} до {nums[1]} с"
        else:
            lo, hi = setting("pause_min"), setting("pause_max")
            out = f"Сейчас: от {lo:.0f} до {hi:.0f} с. Поменять: /delay 20 180"
    elif cmd == "/settings":
        out = "Настройки (поменять: /set ключ число):\n\n" + "\n".join(
            f"{k} = {setting(k):g}: {desc}" for k, (_, desc) in SETTINGS.items())
    elif cmd == "/set":
        try:
            value = float(tail.replace(",", "."))
        except ValueError:
            value = None
        if key not in SETTINGS or value is None or value < 0:
            out = "Формат: /set ключ число. Ключи смотри в /settings"
        else:
            state.setdefault("settings", {})[key] = value
            out = f"✅ {key} = {value:g}: {SETTINGS[key][1]}"
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
    elif cmd == "/learn":
        await event.reply("Перечитываю разговоры…")
        out = await learn()
    elif cmd == "/lessons":
        sub = rest.strip().lower()
        if sub == "clear":
            for k in ("lessons", "corrections", "lessons_at"):
                state.pop(k, None)
            out = "🧹 Выводы и правки стёрты"
        elif sub in ("off", "on"):
            state["lessons_off"] = sub == "off"
            out = "Выводы не учитываю" if sub == "off" else "Снова учитываю выводы"
        else:
            out = (f"🧠 Выводы из разговоров:\n{state.get('lessons') or 'пока нет, будут через сутки или по /learn'}"
                   f"\n\nТвоих правок накоплено: {len(state.get('corrections', []))}")
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


async def catch_up():
    """После перезапуска отвечаем тем, чьи сообщения остались без ответа."""
    for k, c in state["contacts"].items():
        if not c.get("id"):
            try:
                c["id"] = (await client.get_entity(int(k) if k.isdigit() else k)).id
            except Exception:
                log.warning("Не нашёл в Telegram @%s", k)
    save_state()
    if not state["enabled"]:
        return
    async for dialog in client.iter_dialogs(limit=100):
        user = dialog.entity
        if not isinstance(user, User) or user.bot or user.is_self or user.id == control_id():
            continue
        key, contact = contact_for(user)
        if not contact or contact.get("paused_until", 0) > time.time():
            continue
        if dialog.message and not dialog.message.out and dialog.id not in pending:
            log.info("%s ждёт ответа, отвечу", key)
            pending[dialog.id] = asyncio.create_task(reply_later(dialog.id, user, key))


async def main():
    global me
    await client.connect()
    if not await client.is_user_authorized():
        # на сервере без терминала вход делается через login.py
        await client.start()
    me = await client.get_me()
    log.info("Вошёл как @%s. Команды — в Избранном, начни с /help", me.username)
    if CONTROL_USER and not control_id():
        try:
            state["control_id"] = (await client.get_entity(CONTROL_USER)).id
            save_state()
            await notify("Привет, теперь я пишу сюда. Команды присылай в этот чат, начни с /help")
        except Exception:
            log.warning("Не нашёл основной аккаунт @%s", CONTROL_USER)
    state.setdefault("bot_msgs_since", time.time())
    if group_id() and state.get("group_locked") and not state.get("lock_enabled"):
        try:
            await unlock_group()
        except Exception:
            log.exception("Не удалось вернуть тебя в группу")
    save_state()
    await catch_up()
    asyncio.create_task(nudge_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
