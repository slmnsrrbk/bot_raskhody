import datetime
import os
import tempfile
import unittest

os.environ.setdefault("TELEGRAM_TOKEN", "test")
import main  # noqa: E402
import storage  # noqa: E402


class ParseExpenseTests(unittest.TestCase):
    def setUp(self):
        self.today = main.today()

    def test_simple(self):
        name, amount, date = main.parse_expense("хлеб 200")
        self.assertEqual((name, amount, date), ("Хлеб", 200, self.today))

    def test_amount_first(self):
        self.assertEqual(main.parse_expense("350 такси")[:2], ("Такси", 350))

    def test_yesterday_and_currency(self):
        name, amount, date = main.parse_expense("вчера такси 350 руб")
        self.assertEqual((name, amount), ("Такси", 350))
        self.assertEqual(date, self.today - datetime.timedelta(days=1))

    def test_explicit_date(self):
        name, amount, date = main.parse_expense("01.05.2025 кофе 150₽")
        self.assertEqual((name, amount, date), ("Кофе", 150, datetime.date(2025, 5, 1)))

    def test_invalid(self):
        self.assertIsNone(main.parse_expense("31.02 кофе 150"))
        self.assertIsNone(main.parse_expense("просто текст"))
        self.assertIsNone(main.parse_expense("кофе 0"))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        storage.DB_FILE = storage.Path(self.tmp.name) / "t.db"
        storage.LEGACY_EXPENSES = storage.Path(self.tmp.name) / "expenses.json"
        storage.LEGACY_LIMITS = storage.Path(self.tmp.name) / "limits.json"
        storage.init_db()
        storage.upsert_user(1, "A")
        storage.upsert_user(2, "B")

    def tearDown(self):
        self.tmp.cleanup()

    def test_users_are_isolated(self):
        t = datetime.date(2026, 9, 3)
        a = storage.add_expense(1, "кофе", 250, "Еда", t)
        storage.add_expense(2, "такси", 500, "Транспорт", t)
        self.assertEqual([x["name"] for x in storage.list_expenses(1)], ["Кофе"])
        self.assertEqual([x["name"] for x in storage.list_expenses(2)], ["Такси"])
        # пользователь 2 не может удалить или прочитать чужую запись
        self.assertIsNone(storage.delete_expense(2, a["id"]))
        self.assertIsNone(storage.get_expense(2, a["id"]))
        self.assertEqual(storage.delete_expense(1, a["id"])["id"], a["id"])
        self.assertEqual(storage.list_expenses(1), [])

    def test_totals_and_windows(self):
        t = datetime.date(2026, 9, 3)
        storage.add_expense(1, "a", 100, "Еда", t)
        storage.add_expense(1, "b", 200, "Еда", t - datetime.timedelta(days=1))
        storage.add_expense(1, "c", 400, "Другое", t - datetime.timedelta(days=7))
        self.assertEqual(storage.totals(1, 1, t)[0], 100)
        self.assertEqual(storage.totals(1, 7, t), (300, {"Еда": 300}, 2))
        self.assertEqual(storage.totals(1, 30, t)[0], 700)
        self.assertEqual(storage.users_with_expenses(1, t), [1])

    def test_limits_and_report(self):
        self.assertEqual(storage.set_limits(1, daily="2500", weekly=""), {"daily": 2500, "weekly": None, "monthly": None})
        self.assertEqual(storage.get_limits(2)["daily"], None)
        storage.add_expense(1, "кофе", 250, "Еда", main.today())
        text, total, cats = main.build_report(1, 1)
        self.assertEqual(total, 250)
        self.assertIn("Еда", text)
        self.assertIn("осталось 2 250", main.limit_status(1))

    def test_legacy_migration(self):
        storage.LEGACY_EXPENSES.write_text('[["Кофе", 250, "Еда", "03.09.2026"], ["x", "bad", "Еда", "zz"]]', encoding="utf-8")
        storage.LEGACY_LIMITS.write_text('{"daily": 2500, "weekly": null, "monthly": 80000}', encoding="utf-8")
        self.assertTrue(storage.legacy_pending())
        self.assertEqual(storage.migrate_legacy(1), 1)
        self.assertFalse(storage.legacy_pending())
        self.assertEqual(storage.list_expenses(1)[0]["iso"], "2026-09-03")
        self.assertEqual(storage.get_limits(1)["monthly"], 80000)
        self.assertEqual(storage.migrate_legacy(1), 0)  # повторно не импортирует


if __name__ == "__main__":
    unittest.main()
