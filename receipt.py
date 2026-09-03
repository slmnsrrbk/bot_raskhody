"""Чек по QR-коду.

QR на российском кассовом чеке содержит строку вида
  t=20260903T1842&s=517.77&fn=9960440300123456&i=12345&fp=1234567890&n=1
(дата, сумма, номер ФН, номер документа, фискальный признак). Позиции в QR нет —
их отдаёт сервис проверки чеков proverkacheka.com (нужен токен PROVERKACHEKA_TOKEN,
https://proverkacheka.com). Без токена из QR берём дату и сумму.
"""
import datetime
import io
import logging
import os
import re
from urllib.parse import parse_qsl

import requests

import ai
import storage

logger = logging.getLogger("receipt")

PROVERKACHEKA_TOKEN = os.getenv("PROVERKACHEKA_TOKEN", "")
PROVERKACHEKA_URL = "https://proverkacheka.com/api/v1/check/get"
QR_RE = re.compile(r"t=\d{8}T\d{4,6}&s=[\d.,]+&fn=\d+&i=\d+&fp=\d+&n=\d", re.I)


def find_qr_text(text: str):
    m = QR_RE.search(text or "")
    return m.group(0) if m else None


def decode_qr(image_bytes: bytes):
    """Ищет QR-код кассового чека на фото. Возвращает строку QR или None."""
    try:
        import zxingcpp
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img).convert("L")
        for candidate in (img, ImageOps.autocontrast(img), img.resize((img.width * 2, img.height * 2))):
            for r in zxingcpp.read_barcodes(candidate):
                if r.text and find_qr_text(r.text):
                    return find_qr_text(r.text)
    except Exception as e:  # noqa: BLE001
        logger.warning("QR decode: %s", e)
    return None


def parse_qr(qrraw: str) -> dict:
    """-> {"date": ISO, "total": int, "fn", "i", "fp", "raw"}"""
    p = dict(parse_qsl(qrraw))
    date = None
    try:
        date = datetime.datetime.strptime(p.get("t", "")[:13], "%Y%m%dT%H%M").date().isoformat()
    except ValueError:
        pass
    try:
        total = int(round(float(p.get("s", "0").replace(",", "."))))
    except ValueError:
        total = None
    return {"date": date, "total": total, "fn": p.get("fn"), "i": p.get("i"), "fp": p.get("fp"), "raw": qrraw}


def fetch_by_qr(qrraw: str):
    """Позиции чека через proverkacheka.com. -> {"store","date","total","items"} или None."""
    if not PROVERKACHEKA_TOKEN:
        return None
    try:
        r = requests.post(PROVERKACHEKA_URL, data={"token": PROVERKACHEKA_TOKEN, "qrraw": qrraw}, timeout=40)
        body = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("proverkacheka: %s", e)
        return None
    code = body.get("code")
    if code != 1:
        logger.warning("proverkacheka: code=%s data=%.200s", code, body.get("data"))
        return None
    doc = body.get("data", {})
    if isinstance(doc, dict):
        doc = doc.get("json", doc)
    if not isinstance(doc, dict):
        return None
    items = []
    for it in doc.get("items", [])[:80]:
        name = str(it.get("name") or "").strip()
        try:
            amount = int(round(float(it.get("sum", 0)) / 100))
        except (TypeError, ValueError):
            continue
        if name and amount > 0:
            items.append({"name": name[:100], "amount": amount})
    date = None
    try:
        date = datetime.datetime.fromisoformat(str(doc.get("dateTime", ""))[:19]).date().isoformat()
    except ValueError:
        pass
    try:
        total = int(round(float(doc.get("totalSum", 0)) / 100)) or None
    except (TypeError, ValueError):
        total = None
    store = (doc.get("user") or doc.get("retailPlace") or "").strip()[:60] or None
    return {"store": store, "date": date, "total": total, "items": items}


def clean_item_name(name: str) -> str:
    """Убирает мусор из названий с касс: коды, звёздочки, лишние пробелы."""
    n = re.sub(r"^\s*[\d*#]+\s+", "", name)
    n = re.sub(r"\s{2,}", " ", n).strip(" *")
    return (n[:1].upper() + n[1:]) if n else name


def resolve(image_bytes: bytes = None, qr_text: str = None, mime: str = "image/jpeg"):
    """Полный разбор чека: QR → сервис проверки чеков → AI по фото → только сумма из QR.

    -> {"store","date","total","items":[{name,amount,category}], "source": "qr"|"ai"|"qr-sum"} или None
    """
    qrraw = qr_text or (decode_qr(image_bytes) if image_bytes else None)
    qr = parse_qr(qrraw) if qrraw else None

    if qrraw:
        data = fetch_by_qr(qrraw)
        if data and data["items"]:
            names = [clean_item_name(it["name"]) for it in data["items"]]
            cats = ai.classify_many(names)
            data["items"] = [{"name": n, "amount": it["amount"], "category": c} for n, it, c in zip(names, data["items"], cats)]
            data["date"] = data["date"] or (qr and qr["date"])
            data["source"] = "qr"
            return data

    if image_bytes:
        data = ai.parse_receipt(image_bytes, mime)
        if data and data["items"]:
            if qr:
                data["date"] = data["date"] or qr["date"]
                data["total"] = data["total"] or qr["total"]
            data["source"] = "ai"
            return data

    if qr and qr["total"]:
        return {"store": None, "date": qr["date"], "total": qr["total"], "source": "qr-sum",
                "items": [{"name": "Покупка по чеку", "amount": qr["total"], "category": "Другое"}]}
    return None
