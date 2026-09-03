import hashlib
import hmac
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import urlencode

from aiohttp.test_utils import AioHTTPTestCase

import crypto
import webapp


def sign_init_data(token: str, user: dict, auth_date: int) -> str:
    pairs = {"auth_date": str(auth_date), "user": json.dumps(user, separators=(",", ":"))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class WebAppApiTests(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        webapp.storage.DB_FILE = base / "t.db"
        crypto.KEY_FILE = base / ".k"; crypto.reset_cache()
        webapp.storage.init_db()
        webapp.DEV_MODE = True
        webapp._buckets.clear()
        webapp.storage.add_expense(0, "Кофе", 250, "Еда", "03.09.2026")
        webapp.storage.add_expense(0, "Такси", 480, "Транспорт", "02.09.2026")
        webapp.storage.add_expense(7, "Чужая", 999, "Еда", "03.09.2026")  # другой пользователь
        return webapp.make_app()

    async def tearDownAsync(self):
        self.tmp.cleanup()

    async def test_state(self):
        r = await self.client.get("/api/state")
        self.assertEqual(r.status, 200)
        d = await r.json()
        self.assertEqual(len(d["expenses"]), 2)  # чужая запись не видна
        self.assertEqual(d["expenses"][0]["name"], "Кофе")  # новее — первым
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(d["expenses"][0]["iso"], "2026-09-03")
        self.assertEqual(d["limits"], {"daily": None, "weekly": None, "monthly": None})

    async def test_add_update_delete(self):
        r = await self.client.post("/api/expenses", json={"name": "обед", "amount": "620,0", "category": "Еда", "date": "2026-09-01"})
        self.assertEqual(r.status, 201)
        item = await r.json()
        self.assertEqual((item["name"], item["amount"], item["date"]), ("Обед", 620, "01.09.2026"))

        r = await self.client.put(f"/api/expenses/{item['id']}", json={"amount": 700, "category": "Другое"})
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["category"], "Другое")

        r = await self.client.delete(f"/api/expenses/{item['id']}")
        self.assertEqual(r.status, 200)
        self.assertEqual(len(webapp.storage.list_expenses(0)), 2)

    async def test_note_is_optional_and_editable(self):
        r = await self.client.post("/api/expenses", json={"name": "обед", "amount": 300, "category": "Еда", "date": "2026-09-01", "note": "  с коллегами  "})
        item = await r.json()
        self.assertEqual(item["note"], "с коллегами")
        r = await self.client.put(f"/api/expenses/{item['id']}", json={"note": ""})
        self.assertEqual((await r.json())["note"], "")
        r = await self.client.put(f"/api/expenses/{item['id']}", json={"note": "x" * 500})
        self.assertEqual(len((await r.json())["note"]), 300)
        state = await (await self.client.get("/api/state")).json()
        self.assertIn("note", state["expenses"][0])

    async def test_validation(self):
        r = await self.client.post("/api/expenses", json={"name": "x", "amount": "abc"})
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/expenses", json={"name": "", "amount": 10})
        self.assertEqual(r.status, 400)
        r = await self.client.delete("/api/expenses/99")
        self.assertEqual(r.status, 404)
        foreign = webapp.storage.list_expenses(7)[0]["id"]
        r = await self.client.delete(f"/api/expenses/{foreign}")
        self.assertEqual(r.status, 404)  # чужую запись удалить нельзя
        r = await self.client.put(f"/api/expenses/{foreign}", json={"amount": 1})
        self.assertEqual(r.status, 404)

    async def test_rate_limit(self):
        webapp.RATE_LIMIT = 3
        try:
            statuses = [(await self.client.get("/api/state")).status for _ in range(5)]
        finally:
            webapp.RATE_LIMIT = 120
        self.assertEqual(statuses, [200, 200, 200, 429, 429])

    async def test_limits(self):
        r = await self.client.put("/api/limits", json={"daily": "2 500", "weekly": "", "monthly": 80000})
        self.assertEqual(await r.json(), {"daily": 2500, "weekly": None, "monthly": 80000})

    async def test_bulk_and_export(self):
        r = await self.client.post("/api/expenses/bulk", json={"items": [
            {"name": "Молоко", "amount": 80, "category": "Еда", "date": "2026-09-03"},
            {"name": "Пакет", "amount": 7, "date": "2026-09-03"}]})
        self.assertEqual(r.status, 201)
        d = await r.json()
        self.assertEqual((len(d["added"]), d["total"]), (2, 87))
        r = await self.client.post("/api/export", json={"period": "all"})
        self.assertEqual(r.status, 200)
        self.assertIn("spreadsheetml", r.headers["Content-Type"])
        self.assertGreater(len(await r.read()), 2000)
        r = await self.client.post("/api/expenses/bulk", json={"items": []})
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/export", json={"from": "2026-09-03", "to": "2026-09-01"})
        self.assertEqual(r.status, 200)
        self.assertIn("2026-09-01_2026-09-03", r.headers["Content-Disposition"])

    async def test_receipt_unreadable(self):
        webapp.ai.POLZA_API_KEY = ""
        r = await self.client.post("/api/receipt", data={"image": b"x"})
        self.assertEqual(r.status, 422)
        r = await self.client.post("/api/receipt/qr", json={"qr": "такси 350"})
        self.assertEqual(r.status, 400)
        with mock.patch.object(webapp.receipt, "PROVERKACHEKA_TOKEN", ""):
            r = await self.client.post("/api/receipt/qr", json={"qr": "t=20260903T1842&s=517.77&fn=9960440300123456&i=12345&fp=1234567890&n=1"})
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["items"][0]["amount"], 518)

    async def test_custom_category_via_edit(self):
        r = await self.client.get("/api/state")
        first = (await r.json())["expenses"][0]
        r = await self.client.put(f"/api/expenses/{first['id']}", json={"category": "Спорт"})
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["category"], "Спорт")
        r = await self.client.get("/api/state")
        d = await r.json()
        self.assertIn("Спорт", d["categories"])
        self.assertEqual(webapp.storage.all_categories(7)[-1], "Другое")   # у другого пользователя нет
        r = await self.client.post("/api/expenses", json={"name": "Кофе", "amount": 100, "category": "Спорт"})
        self.assertEqual((await r.json())["category"], "Спорт")
        r = await self.client.delete("/api/categories/Еда")
        self.assertEqual(r.status, 400)
        r = await self.client.delete("/api/categories/Спорт")
        self.assertNotIn("Спорт", (await r.json())["categories"])

    async def test_index_served(self):
        r = await self.client.get("/")
        self.assertEqual(r.status, 200)
        self.assertIn("Расходы", await r.text())


class InitDataTests(unittest.TestCase):
    def test_signature(self):
        import time
        webapp.TELEGRAM_TOKEN = "123456:TESTTOKEN"
        user = {"id": 42, "first_name": "Test"}
        good = sign_init_data(webapp.TELEGRAM_TOKEN, user, int(time.time()))
        self.assertEqual(webapp.validate_init_data(good)["id"], 42)
        self.assertIsNone(webapp.validate_init_data(good.replace("hash=", "hash=0")))
        self.assertIsNone(webapp.validate_init_data(""))
        old = sign_init_data(webapp.TELEGRAM_TOKEN, user, int(time.time()) - 3 * 86400)
        self.assertIsNone(webapp.validate_init_data(old))


if __name__ == "__main__":
    unittest.main()


class AdminApiTests(WebAppApiTests):
    async def test_overview_and_block(self):
        # DEV-режим: пользователь 0 — владелец (OWNER_ID не задан)
        webapp.storage.upsert_user(42, "Карим", "karim")
        webapp.storage.add_expense(42, "кофе", 200, "Еда", "2026-09-01")
        r = await self.client.get("/api/admin/overview")
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertGreaterEqual(data["stats"]["total"], 2)
        u = next(x for x in data["users"] if x["id"] == 42)
        self.assertEqual((u["first_name"], u["expenses"], u["blocked"]), ("Карим", 1, False))
        self.assertNotIn("name", u)                      # содержимое трат не отдаётся
        self.assertEqual(len(data["series"]), 7)

        r = await self.client.put("/api/admin/users/42", json={"blocked": True})
        self.assertEqual(r.status, 200)
        self.assertTrue(webapp.storage.is_blocked(42))
        r = await self.client.put("/api/admin/users/0", json={"blocked": True})
        self.assertEqual(r.status, 400)                  # себя закрыть нельзя
        r = await self.client.put("/api/admin/users/999", json={"blocked": True})
        self.assertEqual(r.status, 404)

    async def test_blocked_user_gets_403_and_non_owner_no_admin(self):
        from unittest import mock
        webapp.storage.upsert_user(7, "Гость", "")
        webapp.storage.set_blocked(7, True)
        with mock.patch.object(webapp, "OWNER_ID", 1):     # владелец — не dev-пользователь 0
            r = await self.client.get("/api/admin/overview")
            self.assertEqual(r.status, 403)
            state = await (await self.client.get("/api/state")).json()
            self.assertFalse(state["user"]["is_admin"])
        state = await (await self.client.get("/api/state")).json()
        self.assertTrue(state["user"]["is_admin"])
