import hashlib
import hmac
import json
import os
import tempfile
import unittest
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

    async def test_receipt_requires_key(self):
        webapp.ai.POLZA_API_KEY = ""
        r = await self.client.post("/api/receipt", data={"image": b"x"})
        self.assertEqual(r.status, 503)

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
