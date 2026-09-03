"""Общее хранилище бота и мини-приложения: SQLite, данные привязаны к Telegram-аккаунту.

Оба процесса (main.py и webapp.py) работают с одним файлом data.db; режим WAL
позволяет читать и писать параллельно. Название, сумма и категория каждой траты и лимиты
хранятся зашифрованными ключом пользователя (см. crypto.py); в открытом виде — только
id пользователя и дата (нужна для выборок по периоду).
"""
import datetime
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import crypto

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = Path(os.getenv("DB_FILE", str(BASE_DIR / "data.db")))
LEGACY_EXPENSES = BASE_DIR / "expenses.json"
LEGACY_LIMITS = BASE_DIR / "limits.json"

CATEGORIES = ["Еда", "Транспорт", "Одежда", "Развлечения", "Другое"]
DATE_FMT = "%d.%m.%Y"  # формат для показа в боте; в базе — ISO ГГГГ-ММ-ДД

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    first_name  TEXT,
    username    TEXT,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expenses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    amount      TEXT NOT NULL,
    category    TEXT NOT NULL,
    date        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_expenses_user_date ON expenses(user_id, date);
CREATE TABLE IF NOT EXISTS limits (
    user_id     INTEGER PRIMARY KEY,
    daily       TEXT,
    weekly      TEXT,
    monthly     TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS category_cache (
    name      TEXT PRIMARY KEY,
    category  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
    finally:
        con.close()


def init_db():
    with connect() as con:
        con.executescript(SCHEMA)
        # Записи, зашифрованные другой схемой (другим ключом), прочитать нельзя — удаляем.
        pat = crypto.PREFIX + "%"
        n = con.execute("DELETE FROM expenses WHERE name LIKE 'enc%' AND name NOT LIKE ?", (pat,)).rowcount
        n += con.execute("DELETE FROM limits WHERE daily LIKE 'enc%' AND daily NOT LIKE ?", (pat,)).rowcount
        if n:
            import logging
            logging.getLogger("storage").warning("Удалено %s записей другой схемы шифрования", n)
    crypto.master_key()  # создаёт .data_key при первом запуске
    _encrypt_legacy_rows()


def _encrypt_legacy_rows():
    """Записи, сохранённые до включения шифрования, шифруем на месте."""
    with connect() as con:
        rows = con.execute("SELECT id, user_id, name, amount, category FROM expenses").fetchall()
        for r in rows:
            if not crypto.is_encrypted(r["name"]):
                con.execute("UPDATE expenses SET name=?, amount=?, category=? WHERE id=?",
                            (crypto.encrypt(r["user_id"], r["name"]), crypto.encrypt(r["user_id"], r["amount"]),
                             crypto.encrypt(r["user_id"], r["category"]), r["id"]))
        for r in con.execute("SELECT user_id, daily, weekly, monthly FROM limits").fetchall():
            if any(v is not None and not crypto.is_encrypted(v) for v in (r["daily"], r["weekly"], r["monthly"])):
                con.execute("UPDATE limits SET daily=?, weekly=?, monthly=? WHERE user_id=?",
                            (*[None if v is None else crypto.encrypt(r["user_id"], v) for v in (r["daily"], r["weekly"], r["monthly"])], r["user_id"]))


# ---------------------------------------------------------------------------
# Даты
# ---------------------------------------------------------------------------
def to_iso(value) -> str:
    """'ДД.ММ.ГГГГ' | 'ГГГГ-ММ-ДД' | date -> 'ГГГГ-ММ-ДД'. ValueError при неверной дате."""
    if isinstance(value, datetime.date):
        return value.isoformat()
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", DATE_FMT):
        try:
            return datetime.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Неверная дата: {value}")


def to_display(iso: str) -> str:
    return datetime.date.fromisoformat(iso).strftime(DATE_FMT)


# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------
def upsert_user(user_id: int, first_name: str = "", username: str = ""):
    now = _now()
    with connect() as con:
        con.execute(
            """INSERT INTO users(id, first_name, username, created_at, last_seen) VALUES(?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username, last_seen=excluded.last_seen""",
            (user_id, first_name or "", username or "", now, now),
        )


def all_user_ids():
    with connect() as con:
        return [r["id"] for r in con.execute("SELECT id FROM users")]


# ---------------------------------------------------------------------------
# Траты
# ---------------------------------------------------------------------------
def _row(r) -> dict:
    uid = r["user_id"]
    return {"id": r["id"], "name": crypto.decrypt(uid, r["name"]), "amount": int(crypto.decrypt(uid, r["amount"])),
            "category": crypto.decrypt(uid, r["category"]), "iso": r["date"], "date": to_display(r["date"])}


def _enc(user_id: int, value):
    return None if value is None else crypto.encrypt(user_id, value)


def add_expense(user_id: int, name: str, amount: int, category: str, date) -> dict:
    iso = to_iso(date)
    name = name.strip()
    name = name[:1].upper() + name[1:]
    if category not in CATEGORIES:
        category = "Другое"
    with connect() as con:
        cur = con.execute(
            "INSERT INTO expenses(user_id, name, amount, category, date, created_at) VALUES(?,?,?,?,?,?)",
            (user_id, _enc(user_id, name[:100]), _enc(user_id, int(amount)), _enc(user_id, category), iso, _now()),
        )
        return _row(con.execute("SELECT * FROM expenses WHERE id=?", (cur.lastrowid,)).fetchone())


def get_expense(user_id: int, expense_id: int):
    with connect() as con:
        r = con.execute("SELECT * FROM expenses WHERE id=? AND user_id=?", (expense_id, user_id)).fetchone()
        return _row(r) if r else None


def update_expense(user_id: int, expense_id: int, **fields):
    allowed = {}
    if fields.get("name", "").strip() if isinstance(fields.get("name"), str) else False:
        allowed["name"] = fields["name"].strip()[:100]
    if "amount" in fields and fields["amount"] is not None:
        allowed["amount"] = int(fields["amount"])
    if fields.get("category") in CATEGORIES:
        allowed["category"] = fields["category"]
    if fields.get("date"):
        allowed["date"] = to_iso(fields["date"])
    if not allowed:
        return get_expense(user_id, expense_id)
    values = [v if k == "date" else _enc(user_id, v) for k, v in allowed.items()]
    sets = ", ".join(f"{k}=?" for k in allowed)
    with connect() as con:
        con.execute(f"UPDATE expenses SET {sets} WHERE id=? AND user_id=?", (*values, expense_id, user_id))
    return get_expense(user_id, expense_id)


def delete_expense(user_id: int, expense_id: int):
    """Удаляет запись и возвращает её (для «Вернуть») или None."""
    with connect() as con:
        r = con.execute("SELECT * FROM expenses WHERE id=? AND user_id=?", (expense_id, user_id)).fetchone()
        if not r:
            return None
        con.execute("DELETE FROM expenses WHERE id=? AND user_id=?", (expense_id, user_id))
        return _row(r)


def list_expenses(user_id: int, days=None, today: datetime.date = None, limit=None):
    """Траты пользователя, новые первыми. days — окно «последние N дней» включая сегодня."""
    q = "SELECT * FROM expenses WHERE user_id=?"
    params = [user_id]
    if days is not None:
        today = today or datetime.date.today()
        q += " AND date > ? AND date <= ?"
        params += [(today - datetime.timedelta(days=days)).isoformat(), today.isoformat()]
    q += " ORDER BY date DESC, id DESC"
    if limit:
        q += " LIMIT ?"
        params.append(int(limit))
    with connect() as con:
        return [_row(r) for r in con.execute(q, params)]


def list_expenses_between(user_id: int, date_from: str, date_to: str):
    """Траты за диапазон дат включительно (ISO), новые первыми."""
    with connect() as con:
        return [_row(r) for r in con.execute(
            "SELECT * FROM expenses WHERE user_id=? AND date >= ? AND date <= ? ORDER BY date DESC, id DESC",
            (user_id, date_from, date_to))]


def last_expense(user_id: int):
    """Последняя добавленная (по времени создания) трата пользователя."""
    with connect() as con:
        r = con.execute("SELECT * FROM expenses WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        return _row(r) if r else None


def totals(user_id: int, days: int, today: datetime.date = None):
    """(сумма, {категория: сумма}, количество) за последние days дней."""
    items = list_expenses(user_id, days, today)
    cats = {}
    for it in items:
        cats[it["category"]] = cats.get(it["category"], 0) + it["amount"]
    return sum(it["amount"] for it in items), dict(sorted(cats.items(), key=lambda kv: -kv[1])), len(items)


def users_with_expenses(days: int, today: datetime.date = None):
    today = today or datetime.date.today()
    with connect() as con:
        return [r["user_id"] for r in con.execute(
            "SELECT DISTINCT user_id FROM expenses WHERE date > ? AND date <= ?",
            ((today - datetime.timedelta(days=days)).isoformat(), today.isoformat()))]


# ---------------------------------------------------------------------------
# Лимиты
# ---------------------------------------------------------------------------
def get_limits(user_id: int) -> dict:
    with connect() as con:
        r = con.execute("SELECT daily, weekly, monthly FROM limits WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        return {"daily": None, "weekly": None, "monthly": None}
    dec = lambda v: None if v is None else int(crypto.decrypt(user_id, v))  # noqa: E731
    return {"daily": dec(r["daily"]), "weekly": dec(r["weekly"]), "monthly": dec(r["monthly"])}


def set_limits(user_id: int, **values) -> dict:
    cur = get_limits(user_id)
    for k in ("daily", "weekly", "monthly"):
        if k in values:
            v = values[k]
            cur[k] = None if v in (None, "", 0, "0") else int(v)
    with connect() as con:
        con.execute(
            """INSERT INTO limits(user_id, daily, weekly, monthly) VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET daily=excluded.daily, weekly=excluded.weekly, monthly=excluded.monthly""",
            (user_id, _enc(user_id, cur["daily"]), _enc(user_id, cur["weekly"]), _enc(user_id, cur["monthly"])),
        )
    return cur


# ---------------------------------------------------------------------------
# Кэш «название → категория» (общий для всех пользователей, без личных данных)
# ---------------------------------------------------------------------------
def _cache_key(name: str) -> str:
    return hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()


def cache_get(name: str):
    with connect() as con:
        r = con.execute("SELECT category FROM category_cache WHERE name=?", (_cache_key(name),)).fetchone()
        return r["category"] if r else None


def cache_set(name: str, category: str):
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO category_cache(name, category) VALUES(?,?)", (_cache_key(name), category))


def add_expenses_bulk(user_id: int, items):
    """Добавляет несколько трат за раз; возвращает список записей."""
    return [add_expense(user_id, it["name"], it["amount"], it.get("category", "Другое"), it.get("date") or datetime.date.today())
            for it in items]


def delete_many(user_id: int, ids):
    n = 0
    with connect() as con:
        for i in ids:
            n += con.execute("DELETE FROM expenses WHERE id=? AND user_id=?", (int(i), user_id)).rowcount
    return n


def wipe_all():
    """Удаляет все траты, лимиты и кэш (используется для очистки тестовых данных)."""
    with connect() as con:
        con.executescript("DELETE FROM expenses; DELETE FROM limits; DELETE FROM category_cache; DELETE FROM users;")


# ---------------------------------------------------------------------------
# Перенос старых файлов expenses.json / limits.json первому владельцу
# ---------------------------------------------------------------------------
def migrate_legacy(owner_id: int) -> int:
    """Импортирует старые JSON-файлы в базу под owner_id. Возвращает число записей."""
    with connect() as con:
        if con.execute("SELECT 1 FROM meta WHERE key='legacy_imported'").fetchone():
            return 0
    n = 0
    if LEGACY_EXPENSES.exists():
        try:
            rows = json.loads(LEGACY_EXPENSES.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            rows = []
        for r in rows:
            try:
                add_expense(owner_id, str(r[0]), int(r[1]), str(r[2]), to_iso(r[3]))
                n += 1
            except (ValueError, IndexError, TypeError):
                continue
        LEGACY_EXPENSES.rename(LEGACY_EXPENSES.with_suffix(".json.imported"))
    if LEGACY_LIMITS.exists():
        try:
            lim = json.loads(LEGACY_LIMITS.read_text(encoding="utf-8"))
            set_limits(owner_id, **{k: lim.get(k) for k in ("daily", "weekly", "monthly")})
        except (ValueError, OSError):
            pass
        LEGACY_LIMITS.rename(LEGACY_LIMITS.with_suffix(".json.imported"))
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('legacy_imported', ?)", (str(owner_id),))
    return n


def legacy_pending() -> bool:
    if not (LEGACY_EXPENSES.exists() or LEGACY_LIMITS.exists()):
        return False
    with connect() as con:
        return con.execute("SELECT 1 FROM meta WHERE key='legacy_imported'").fetchone() is None
