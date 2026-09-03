"""Шифрование личных данных в базе: AES-256-GCM, у каждого пользователя свой ключ.

Мастер-ключ хранится в файле .data_key рядом с кодом (или в переменной DATA_KEY, base64).
Ключ пользователя = HKDF(мастер-ключ, user_id), поэтому даже с копией базы и ключа одного
пользователя чужие записи прочитать нельзя, а без файла ключа база нечитаема целиком.
Берегите .data_key: без него данные не восстановить.
"""
import base64
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

BASE_DIR = Path(__file__).resolve().parent
KEY_FILE = Path(os.getenv("DATA_KEY_FILE", str(BASE_DIR / ".data_key")))
PREFIX = "enc1:"

_master = None
_user_keys: dict = {}


def master_key() -> bytes:
    global _master
    if _master is not None:
        return _master
    env = os.getenv("DATA_KEY", "")
    if env:
        _master = base64.b64decode(env)
    elif KEY_FILE.exists():
        _master = base64.b64decode(KEY_FILE.read_text().strip())
    else:
        _master = secrets.token_bytes(32)
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(base64.b64encode(_master).decode())
    if len(_master) != 32:
        raise ValueError("DATA_KEY должен быть 32 байта в base64")
    return _master


def user_key(user_id: int) -> bytes:
    k = _user_keys.get(user_id)
    if k is None:
        k = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"user:{user_id}".encode()).derive(master_key())
        if len(_user_keys) > 5000:
            _user_keys.clear()
        _user_keys[user_id] = k
    return k


def encrypt(user_id: int, value) -> str:
    data = str(value).encode("utf-8")
    nonce = secrets.token_bytes(12)
    ct = AESGCM(user_key(user_id)).encrypt(nonce, data, str(user_id).encode())
    return PREFIX + base64.b64encode(nonce + ct).decode()


def decrypt(user_id: int, value):
    """Расшифровывает строку; незашифрованное значение возвращает как есть (для старых записей)."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    raw = base64.b64decode(value[len(PREFIX):])
    return AESGCM(user_key(user_id)).decrypt(raw[:12], raw[12:], str(user_id).encode()).decode("utf-8")


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def reset_cache():
    global _master
    _master = None
    _user_keys.clear()
