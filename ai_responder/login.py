"""
Вход в аккаунт без терминала, в два запуска:
  python login.py start        — Telegram присылает код
  python login.py code 12345   — вход по этому коду
  python login.py status       — проверить, выполнен ли вход
Номер берётся из TG_PHONE, облачный пароль (если включён) — из TG_2FA_PASSWORD.
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

load_dotenv(os.getenv("ENV_FILE", ".env"))

DATA_DIR = os.getenv("DATA_DIR", "data")
HASH_FILE = os.path.join(DATA_DIR, "login_hash.txt")
PHONE = os.environ["TG_PHONE"]


async def main(mode: str, code: str = ""):
    os.makedirs(DATA_DIR, exist_ok=True)
    client = TelegramClient(
        os.path.join(DATA_DIR, "account"), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]
    )
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Уже вошли как @{me.username or me.id}")
            return
        if mode == "status":
            print("Ещё не вошли в аккаунт")
            sys.exit(2)

        if mode == "start":
            sent = await client.send_code_request(PHONE)
            with open(HASH_FILE, "w") as f:
                f.write(sent.phone_code_hash)
            print("Код отправлен в Telegram")
            return

        with open(HASH_FILE) as f:
            phone_code_hash = f.read().strip()
        try:
            await client.sign_in(PHONE, code.strip(), phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            password = os.getenv("TG_2FA_PASSWORD")
            if not password:
                sys.exit("Включён облачный пароль: добавь секрет TG_2FA_PASSWORD и повтори ai-login")
            await client.sign_in(password=password)
        os.remove(HASH_FILE)
        me = await client.get_me()
        print(f"Готово, вошли как @{me.username or me.id}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
