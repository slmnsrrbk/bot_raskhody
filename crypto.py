"""Шифрование данных паролем пользователя: сервер не хранит ни пароль, ни ключ.

Схема:
  • у каждого пользователя случайный ключ данных (DEK, 32 байта);
  • DEK хранится в базе только в «обёрнутом» виде: зашифрован ключом KEK = scrypt(пароль, соль);
  • сам пароль нигде не сохраняется; после ввода DEK живёт только в оперативной памяти
    (каталог в tmpfs /dev/shm, очищается при перезагрузке) и удаляется после UNLOCK_TTL_DAYS без активности;
  • записи шифруются AES-256-GCM ключом DEK.
Итог: копия базы, бэкап или доступ к диску без пароля пользователя ничего не дают — в том числе владельцу сервера.
Забытый пароль восстановить невозможно.
"""
import base64
import os
import secrets
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

PREFIX = "enc2:"
UNLOCK_DIR = Path(os.getenv("UNLOCK_DIR", "/dev/shm/bot_raskhody"))
UNLOCK_TTL = int(os.getenv("UNLOCK_TTL_DAYS", "30")) * 86400
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 16, 8, 1       # ~0.3 с и 64 МБ на попытку: перебор пароля дорог
MIN_PIN_LEN, MAX_PIN_LEN = 6, 64
MAX_ATTEMPTS, ATTEMPT_WINDOW = 5, 15 * 60          # защита от перебора онлайн

_mem: dict = {}   # user_id -> (key, время последнего использования)


class NoPin(Exception):
    """Пароль ещё не задан."""


class Locked(Exception):
    """Данные закрыты: нужно ввести пароль."""


class WrongPin(Exception):
    """Неверный пароль."""


class TooManyAttempts(Exception):
    """Слишком много неверных попыток."""


def _con():
    import storage
    return storage.connect()


def _kek(pin: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(pin.encode("utf-8"))


def _aad(user_id: int) -> bytes:
    return str(user_id).encode()


# ---------------------------------------------------------------------------
# Хранение разблокированного ключа только в памяти (tmpfs)
# ---------------------------------------------------------------------------
def _key_path(user_id: int) -> Path:
    return UNLOCK_DIR / f"{user_id}.key"


def _remember(user_id: int, key: bytes):
    _mem[user_id] = (key, time.time())
    try:
        UNLOCK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        p = _key_path(user_id)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    except OSError:
        pass


def _forget(user_id: int):
    _mem.pop(user_id, None)
    try:
        _key_path(user_id).unlink()
    except OSError:
        pass


def _load_key(user_id: int):
    """Ключ из памяти процесса или из tmpfs (общий для бота и веб-части). None — заблокировано."""
    now = time.time()
    hit = _mem.get(user_id)
    if hit and now - hit[1] < UNLOCK_TTL:
        _mem[user_id] = (hit[0], now)
        return hit[0]
    p = _key_path(user_id)
    try:
        st = p.stat()
        if now - st.st_mtime > UNLOCK_TTL:
            _forget(user_id)
            return None
        key = p.read_bytes()
        if len(key) != 32:
            return None
        os.utime(p, None)
        _mem[user_id] = (key, now)
        return key
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Пароль
# ---------------------------------------------------------------------------
def validate_pin(pin: str):
    if not isinstance(pin, str) or not (MIN_PIN_LEN <= len(pin) <= MAX_PIN_LEN) or any(c.isspace() for c in pin):
        raise ValueError(f"Пароль: от {MIN_PIN_LEN} до {MAX_PIN_LEN} символов без пробелов. Лучше не только цифры.")


def has_pin(user_id: int) -> bool:
    with _con() as con:
        return con.execute("SELECT 1 FROM user_keys WHERE user_id=?", (user_id,)).fetchone() is not None


def is_unlocked(user_id: int) -> bool:
    return _load_key(user_id) is not None


def status(user_id: int) -> str:
    """'nopin' | 'locked' | 'unlocked'"""
    if not has_pin(user_id):
        return "nopin"
    return "unlocked" if is_unlocked(user_id) else "locked"


def _check_attempts(user_id: int):
    now = time.time()
    with _con() as con:
        r = con.execute("SELECT count, first_ts FROM pin_attempts WHERE user_id=?", (user_id,)).fetchone()
        if r and now - r["first_ts"] < ATTEMPT_WINDOW and r["count"] >= MAX_ATTEMPTS:
            raise TooManyAttempts


def _record_attempt(user_id: int, ok: bool):
    now = time.time()
    with _con() as con:
        if ok:
            con.execute("DELETE FROM pin_attempts WHERE user_id=?", (user_id,))
            return
        r = con.execute("SELECT count, first_ts FROM pin_attempts WHERE user_id=?", (user_id,)).fetchone()
        if r and now - r["first_ts"] < ATTEMPT_WINDOW:
            con.execute("UPDATE pin_attempts SET count=count+1 WHERE user_id=?", (user_id,))
        else:
            con.execute("INSERT OR REPLACE INTO pin_attempts(user_id, count, first_ts) VALUES(?,1,?)", (user_id, now))


def setup_pin(user_id: int, pin: str):
    """Создаёт ключ данных и защищает его паролем. Повторный вызов — ошибка (см. change_pin)."""
    validate_pin(pin)
    if has_pin(user_id):
        raise ValueError("Пароль уже задан")
    dek, salt, nonce = secrets.token_bytes(32), secrets.token_bytes(16), secrets.token_bytes(12)
    wrapped = AESGCM(_kek(pin, salt)).encrypt(nonce, dek, _aad(user_id))
    with _con() as con:
        con.execute("INSERT INTO user_keys(user_id, salt, nonce, wrapped, kdf, created_at) VALUES(?,?,?,?,?,?)",
                    (user_id, salt, nonce, wrapped, f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}", time.time()))
    _remember(user_id, dek)


def _unwrap(user_id: int, pin: str) -> bytes:
    with _con() as con:
        r = con.execute("SELECT salt, nonce, wrapped, kdf FROM user_keys WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        raise NoPin
    _check_attempts(user_id)
    _, n, rr, p = r["kdf"].split(":")
    kek = Scrypt(salt=r["salt"], length=32, n=int(n), r=int(rr), p=int(p)).derive(pin.encode("utf-8"))
    try:
        dek = AESGCM(kek).decrypt(r["nonce"], r["wrapped"], _aad(user_id))
    except InvalidTag:
        _record_attempt(user_id, False)
        raise WrongPin
    _record_attempt(user_id, True)
    return dek


def unlock(user_id: int, pin: str):
    _remember(user_id, _unwrap(user_id, pin))


def change_pin(user_id: int, old_pin: str, new_pin: str):
    validate_pin(new_pin)
    dek = _unwrap(user_id, old_pin)
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    wrapped = AESGCM(_kek(new_pin, salt)).encrypt(nonce, dek, _aad(user_id))
    with _con() as con:
        con.execute("UPDATE user_keys SET salt=?, nonce=?, wrapped=?, kdf=? WHERE user_id=?",
                    (salt, nonce, wrapped, f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}", user_id))
    _remember(user_id, dek)


def lock(user_id: int):
    _forget(user_id)


def remove_pin(user_id: int):
    """Удаляет ключ (вместе с ним теряются все данные пользователя — вызывать после их удаления)."""
    with _con() as con:
        con.execute("DELETE FROM user_keys WHERE user_id=?", (user_id,))
        con.execute("DELETE FROM pin_attempts WHERE user_id=?", (user_id,))
    _forget(user_id)


# ---------------------------------------------------------------------------
# Шифрование записей
# ---------------------------------------------------------------------------
def user_key(user_id: int) -> bytes:
    key = _load_key(user_id)
    if key is None:
        if has_pin(user_id):
            raise Locked
        raise NoPin
    return key


def encrypt(user_id: int, value) -> str:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(user_key(user_id)).encrypt(nonce, str(value).encode("utf-8"), _aad(user_id))
    return PREFIX + base64.b64encode(nonce + ct).decode()


def decrypt(user_id: int, value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    raw = base64.b64decode(value[len(PREFIX):])
    return AESGCM(user_key(user_id)).decrypt(raw[:12], raw[12:], _aad(user_id)).decode("utf-8")


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def reset_cache():
    _mem.clear()
