"""Курсы валют: open.er-api.com (база USD, ~160 валют), запасной источник — ЦБ РФ (cbr-xml-daily.ru).

Курсы кэшируются в таблице meta и в памяти; сеть трогается не чаще раза в TTL. Все функции конвертации
работают только с кэшем, поэтому их можно вызывать из любого места без задержек.
"""
import datetime
import json
import logging
import threading
import time

import requests

logger = logging.getLogger("rates")

PRIMARY_URL = "https://open.er-api.com/v6/latest/USD"
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
TTL = 6 * 3600           # как часто обновлять
TIMEOUT = 15

_mem = {"data": None, "loaded": False}
_lock = threading.Lock()


def _load_cached():
    """Курсы из meta (без сети)."""
    import storage  # локальный импорт: storage импортирует rates
    try:
        with storage.connect() as con:
            r = con.execute("SELECT value FROM meta WHERE key='rates'").fetchone()
        return json.loads(r["value"]) if r else None
    except Exception as e:  # noqa: BLE001
        logger.warning("Кэш курсов недоступен: %s", e)
        return None


def _save(data):
    import storage
    with storage.connect() as con:
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('rates', ?)", (json.dumps(data),))


def _fetch_primary():
    r = requests.get(PRIMARY_URL, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("result") != "success" or not isinstance(j.get("rates"), dict):
        raise ValueError("bad response")
    rates = {k.upper(): float(v) for k, v in j["rates"].items() if v}
    rates["USD"] = 1.0
    return {"base": "USD", "rates": rates, "date": j.get("time_last_update_utc", "")[:16], "source": "open.er-api.com", "ts": time.time()}


def _fetch_cbr():
    r = requests.get(CBR_URL, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    # ЦБ: сколько рублей за Nominal единиц валюты. Переводим в базу USD.
    rub_per = {"RUB": 1.0}
    for code, v in j.get("Valute", {}).items():
        try:
            rub_per[code.upper()] = float(v["Value"]) / float(v.get("Nominal") or 1)
        except (TypeError, ValueError, KeyError):
            continue
    usd = rub_per.get("USD")
    if not usd:
        raise ValueError("no USD in CBR data")
    rates = {code: usd / rub for code, rub in rub_per.items() if rub}
    rates["USD"] = 1.0
    return {"base": "USD", "rates": rates, "date": str(j.get("Date", ""))[:10], "source": "cbr.ru", "ts": time.time()}


def refresh(force: bool = False) -> dict:
    """Обновляет курсы из сети, если кэш старше TTL. Возвращает актуальные данные (или старые при ошибке сети)."""
    with _lock:
        data = _mem["data"] if _mem["loaded"] else _load_cached()
        _mem["loaded"] = True
        if data and not force and time.time() - float(data.get("ts") or 0) < TTL:
            _mem["data"] = data
            return data
        for fetch in (_fetch_primary, _fetch_cbr):
            try:
                fresh = fetch()
                _save(fresh)
                _mem["data"] = fresh
                logger.info("Курсы обновлены: %s, %s валют", fresh["source"], len(fresh["rates"]))
                return fresh
            except Exception as e:  # noqa: BLE001
                logger.warning("Курсы (%s): %s", getattr(fetch, "__name__", "fetch"), e)
        _mem["data"] = data
        return data or {"base": "USD", "rates": {"USD": 1.0}, "date": "", "source": "none", "ts": 0}


def get() -> dict:
    """Курсы из кэша (память → meta), без сети."""
    if not _mem["loaded"]:
        with _lock:
            if not _mem["loaded"]:
                _mem["data"] = _load_cached()
                _mem["loaded"] = True
    return _mem["data"] or {"base": "USD", "rates": {"USD": 1.0}, "date": "", "source": "none", "ts": 0}


def is_stale() -> bool:
    return time.time() - float(get().get("ts") or 0) >= TTL


def rate(frm: str, to: str) -> float:
    """Сколько единиц `to` за одну единицу `frm`; 1.0, если курс неизвестен."""
    frm, to = (frm or "").upper(), (to or "").upper()
    if frm == to:
        return 1.0
    r = get()["rates"]
    if frm in r and to in r and r[frm]:
        return r[to] / r[frm]
    return 1.0


def known(code: str) -> bool:
    return (code or "").upper() in get()["rates"]


def convert(amount, frm: str, to: str) -> float:
    return float(amount) * rate(frm, to)


def info() -> dict:
    d = get()
    try:
        updated = datetime.datetime.fromtimestamp(float(d.get("ts") or 0), datetime.timezone.utc).isoformat(timespec="minutes") if d.get("ts") else None
    except (OverflowError, ValueError, OSError):
        updated = None
    return {"source": d.get("source", "none"), "date": d.get("date", ""), "count": len(d.get("rates", {})), "updated": updated}


def reset_cache():
    with _lock:
        _mem["data"], _mem["loaded"] = None, False
