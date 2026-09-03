import datetime
import os
import tempfile
import unittest

os.environ.setdefault("TELEGRAM_TOKEN", "test")
import main  # noqa: E402
import crypto  # noqa: E402
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
        crypto.KEY_FILE = storage.Path(self.tmp.name) / ".k"; crypto.reset_cache()
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


class BotBuildTests(StorageTests):
    def test_all_handlers_resolve(self):
        app = main.build_app("123456:TESTTOKEN")
        names = {h.callback.__name__ for group in app.handlers.values() for h in group}
        for expected in ("start", "handle_message", "handle_qr_text", "handle_photo", "ask_delete", "ask_export", "on_callback"):
            self.assertIn(expected, names)

    def test_cache_reset_on_category_version_change(self):
        storage.cache_set("мясо в дом", "Еда")
        with storage.connect() as con:
            con.execute("UPDATE meta SET value='1' WHERE key='categories_version'")
        storage.init_db()
        self.assertIsNone(storage.cache_get("мясо в дом"))
        storage.cache_set("мясо в дом", "Продукты")
        storage.init_db()
        self.assertEqual(storage.cache_get("мясо в дом"), "Продукты")  # без смены версии кэш живёт

    def test_double_wrapped_rows_removed(self):
        # запись чужой схемы, случайно «обёрнутая» текущим ключом: сумма не разбирается -> удаляем при старте
        wrapped = crypto.encrypt(1, "enc2:garbage")
        with storage.connect() as con:
            con.execute("INSERT INTO expenses(user_id,name,amount,category,date,created_at) VALUES(1,?,?,?,'2026-09-01','x')",
                        (crypto.encrypt(1, "Кофе"), wrapped, crypto.encrypt(1, "Еда")))
        storage.add_expense(1, "Норм", 10, "Еда", "2026-09-02")
        storage.init_db()
        self.assertEqual([i["name"] for i in storage.list_expenses(1)], ["Норм"])

    def test_foreign_scheme_rows_removed(self):
        import sqlite3
        raw = sqlite3.connect(storage.DB_FILE)
        raw.execute("INSERT INTO expenses(user_id,name,amount,category,date,created_at) VALUES(1,'enc2:xx','enc2:yy','enc2:zz','2026-09-01','x')")
        raw.commit(); raw.close()
        storage.init_db()
        self.assertEqual(storage.list_expenses(1), [])


class ProcessExpenseTests(StorageTests):
    def test_multi_line_message_adds_all(self):
        import asyncio
        from unittest import mock
        sent = []

        class FakeMsg:
            async def edit_text(self, text, reply_markup=None):
                sent.append(("edit", text))

        class FakeBot:
            async def send_message(self, chat_id, text, **kw):
                sent.append(("send", text))
                return FakeMsg()

        class Ctx:
            user_data = {}

        text = "Вчера\n\nБургер 2800\nМойка 700\n\n01.09.2026\n\nМясо 7500 продукты\nПодстричься 1300₽"
        with mock.patch.object(main.ai, "POLZA_API_KEY", ""), mock.patch.object(main.ai, "CHAD_API_KEY", ""):
            asyncio.run(main.process_expense(FakeBot(), 1, text, Ctx()))
        items = storage.list_expenses(1)
        self.assertEqual(len(items), 4)
        self.assertEqual({i["category"] for i in items if i["name"] == "Мойка"}, {"Машина"})
        self.assertIn("Добавлено 4 траты", sent[0][1])
        self.assertIn("01.09.2026", sent[0][1])
        self.assertEqual(len(Ctx.user_data["receipts"]), 1)


    def test_voice_message_goes_through_model_first(self):
        import asyncio
        from unittest import mock
        sent = []

        class FakeMsg:
            async def edit_text(self, text, reply_markup=None):
                sent.append(("edit", text))

        class FakeBot:
            async def send_message(self, chat_id, text, **kw):
                sent.append(("send", text))
                return FakeMsg()

        class Ctx:
            user_data = {}

        def fake_parse(text, today, spoken=False, categories=None):
            self.assertTrue(spoken)
            return [{"name": "такси", "amount": 350, "date": today.isoformat()},
                    {"name": "кофе", "amount": 200, "date": today.isoformat()}]

        with mock.patch.object(main.ai, "POLZA_API_KEY", "k"), mock.patch.object(main.ai, "parse_expenses_text", side_effect=fake_parse), \
                mock.patch.object(main.ai, "classify_many", return_value=["Транспорт", "Еда"]):
            asyncio.run(main.process_expense(FakeBot(), 5, "потратил на такси триста пятьдесят и на кофе двести", Ctx(), spoken=True))
        names = {i["name"] for i in storage.list_expenses(5)}
        self.assertEqual({n.lower() for n in names}, {"такси", "кофе"})
        self.assertIn("Добавлено 2 траты", sent[0][1])

    def test_free_text_goes_to_model_first_with_category(self):
        import asyncio
        from unittest import mock
        sent = []

        class FakeMsg:
            async def edit_text(self, text, reply_markup=None):
                sent.append(("edit", text))

        class FakeBot:
            async def send_message(self, chat_id, text, **kw):
                sent.append(("send", text))
                return FakeMsg()

        class Ctx:
            user_data = {}

        seen = {}

        def fake_parse(text, today, spoken=False, categories=None):
            seen["categories"] = categories
            return [{"name": "такси", "amount": 250, "date": "2026-09-02", "category": "Транспорт"}]

        with mock.patch.object(main.ai, "POLZA_API_KEY", "k"), mock.patch.object(main.ai, "parse_expenses_text", side_effect=fake_parse), \
                mock.patch.object(main.ai, "classify_many", side_effect=AssertionError("модель уже дала категорию")):
            asyncio.run(main.process_expense(FakeBot(), 7, "250 на такси 2 сентября", Ctx()))
        items = storage.list_expenses_between(7, "2026-09-02", "2026-09-02")
        self.assertEqual([(i["name"], i["amount"], i["category"]) for i in items], [("Такси", 250, "Транспорт")])
        self.assertIn("Другое", seen["categories"])
        self.assertIn("02.09.2026", sent[0][1])

    def test_model_failure_falls_back_to_strict_parser(self):
        from unittest import mock
        with mock.patch.object(main.ai, "POLZA_API_KEY", "k"), mock.patch.object(main.ai, "parse_expenses_text", side_effect=RuntimeError("down")):
            items = main.parse_expenses("такси 350", user_id=1)
        self.assertEqual([(i["name"], i["amount"]) for i in items], [("Такси", 350)])

    def test_voice_handler_registered(self):
        app = main.build_app("123:abc")
        names = {getattr(h.callback, "__name__", "") for group in app.handlers.values() for h in group}
        self.assertIn("handle_voice", names)


class NoteMigrationTests(unittest.TestCase):
    def test_old_database_gets_note_column(self):
        import sqlite3
        from pathlib import Path
        tmp = tempfile.mkdtemp()
        old_db, old_key = storage.DB_FILE, crypto.KEY_FILE
        storage.DB_FILE, crypto.KEY_FILE = Path(tmp) / "old.db", Path(tmp) / ".k"
        crypto.reset_cache()
        try:
            con = sqlite3.connect(storage.DB_FILE)
            con.executescript("""CREATE TABLE expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                name TEXT NOT NULL, amount TEXT NOT NULL, category TEXT NOT NULL, date TEXT NOT NULL, created_at TEXT NOT NULL);""")
            con.commit(); con.close()
            storage.init_db()
            item = storage.add_expense(3, "кофе", 200, "Еда", "2026-09-01", note="утром")
            self.assertEqual(item["note"], "утром")
            self.assertEqual(storage.update_expense(3, item["id"], note=None)["note"], "")
            # заметка хранится зашифрованной
            con = sqlite3.connect(storage.DB_FILE)
            raw = con.execute("SELECT note FROM expenses WHERE id=?", (storage.add_expense(3, "чай", 100, "Еда", "2026-09-01", note="секрет")["id"],)).fetchone()[0]
            con.close()
            self.assertTrue(raw.startswith("enc") and "секрет" not in raw)
        finally:
            storage.DB_FILE, crypto.KEY_FILE = old_db, old_key
            crypto.reset_cache()


class CustomCategoryTests(StorageTests):
    def test_user_categories(self):
        self.assertEqual(storage.add_user_category(1, "  дети "), "Дети")
        self.assertEqual(storage.add_user_category(1, "ДЕТИ"), "Дети")          # без дублей
        self.assertEqual(storage.add_user_category(1, "еда"), "Еда")            # базовая не дублируется
        self.assertIn("Дети", storage.all_categories(1))
        self.assertNotIn("Дети", storage.all_categories(2))                     # у другого пользователя нет
        it = storage.add_expense(1, "Садик", 5000, "Дети", "2026-09-03")
        self.assertEqual(it["category"], "Дети")
        self.assertEqual(storage.add_expense(2, "Садик", 5000, "Дети", "2026-09-03")["category"], "Другое")
        storage.cache_set("садик", "Дети", 1)
        self.assertEqual(main.ai.classify_many(["Садик"], 1), ["Дети"])
        self.assertEqual(main.ai.classify_many(["Садик"], 2), ["Другое"])
        with self.assertRaises(ValueError):
            storage.add_user_category(1, "   ")
        kb = main._category_kb(1, 5, "-")
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("Дети", labels)
        self.assertIn("➕ Своя категория", labels)
