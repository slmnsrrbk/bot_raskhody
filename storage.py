"""Общее хранилище бота и мини-приложения: SQLite, данные привязаны к Telegram-аккаунту.

Оба процесса (main.py и webapp.py) работают с одним файлом data.db; режим WAL
позволяет читать и писать параллельно.
"""
import datetime
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

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
    amount      INTEGER NOT NULL,
    category    TEXT NOT NULL,
    date        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_expenses_user_date ON expenses(user_id, date);
CREATE TABLE IF NOT EXISTS limits (
    user_id     INTEGER PRIMARY KEY,
    daily       INTEGER,
    weekly      INTEGER,
    monthly     INTEGER
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
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
    return {"id": r["id"], "name": r["name"], "amount": r["amount"], "category": r["category"],
            "iso": r["date"], "date": to_display(r["date"])}


def add_expense(user_id: int, name: str, amount: int, category: str, date) -> dict:
    iso = to_iso(date)
    name = name.strip()
    name = name[:1].upper() + name[1:]
    if category not in CATEGORIES:
        category = "Другое"
    with connect() as con:
        cur = con.execute(
            "INSERT INTO expenses(user_id, name, amount, category, date, created_at) VALUES(?,?,?,?,?,?)",
            (user_id, name[:100], int(amount), category, iso, _now()),
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
    sets = ", ".join(f"{k}=?" for k in allowed)
    with connect() as con:
        con.execute(f"UPDATE expenses SET {sets} WHERE id=? AND user_id=?", (*allowed.values(), expense_id, user_id))
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
    return {"daily": r["daily"], "weekly": r["weekly"], "monthly": r["monthly"]} if r else \
        {"daily": None, "weekly": None, "monthly": None}


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
            (user_id, cur["daily"], cur["weekly"], cur["monthly"]),
        )
    return cur


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
