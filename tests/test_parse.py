import datetime
import unittest
from unittest import mock

import main


class ParseExpenseTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(main, "detect_category", return_value="Еда")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.today = main.datetime.datetime.now(main.pytz.timezone("Asia/Krasnoyarsk")).date()

    def test_simple(self):
        name, amount, category, date = main.parse_expense("хлеб 200")
        self.assertEqual(name, "Хлеб")
        self.assertEqual(amount, 200)
        self.assertEqual(category, "Еда")
        self.assertEqual(date, self.today.strftime("%d.%m.%Y"))

    def test_yesterday_and_currency(self):
        _, amount, _, date = main.parse_expense("вчера такси 350 руб")
        self.assertEqual(amount, 350)
        expected = (self.today - datetime.timedelta(days=1)).strftime("%d.%m.%Y")
        self.assertEqual(date, expected)

    def test_explicit_date(self):
        name, amount, _, date = main.parse_expense("01.05.2025 кофе 150₽")
        self.assertEqual((name, amount, date), ("Кофе", 150, "01.05.2025"))

    def test_garbage_returns_none(self):
        self.assertIsNone(main.parse_expense("просто текст"))


if __name__ == "__main__":
    unittest.main()


class LocalStorageTests(unittest.TestCase):
    def test_add_and_report(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.object(main, "EXPENSES_FILE", os.path.join(d, "e.json")):
            today = main.datetime.datetime.now(main.pytz.timezone("Asia/Krasnoyarsk")).date().strftime("%d.%m.%Y")
            main.add_expense(["Хлеб", 200, "Еда", today])
            main.add_expense(["Такси", 300, "Транспорт", today])
            text, total, cats = main.build_report(1)
            self.assertEqual(total, 500)
            self.assertEqual(cats, {"Еда": 200, "Транспорт": 300})
            self.assertIn("Общая сумма: 500", text)
