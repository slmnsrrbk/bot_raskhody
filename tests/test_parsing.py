import datetime
import unittest

import parsing

T = datetime.date(2026, 9, 4)


class FreeTextTests(unittest.TestCase):
    def test_user_message(self):
        text = ("Вчера\n\nБургер 2800\nМойка 700\n\n01.09.2026\n\nНепредвиденные расходы (котел починить)\n12500\n"
                "Мясо в дом 7500\n\n02.09.2026\n\nПодстричься 1300₽\nПродукты 1500₽")
        items, unparsed = parsing.parse_free_text(text, T)
        self.assertEqual(unparsed, [])
        self.assertEqual([(i["name"], i["amount"], i["date"].isoformat()) for i in items], [
            ("Бургер", 2800, "2026-09-03"), ("Мойка", 700, "2026-09-03"),
            ("Непредвиденные расходы (котел починить)", 12500, "2026-09-01"), ("Мясо в дом", 7500, "2026-09-01"),
            ("Подстричься", 1300, "2026-09-02"), ("Продукты", 1500, "2026-09-02")])

    def test_variants(self):
        items, unparsed = parsing.parse_free_text("вчера такси 350 руб\n2 сентября: кофе 150, обед 620₽\n1 200 р - продукты\nсегодня 05.09 хлеб 40", T)
        self.assertEqual(unparsed, [])
        self.assertEqual([(i["name"], i["amount"], i["date"].day) for i in items],
                         [("Такси", 350, 3), ("Кофе", 150, 2), ("Обед", 620, 2), ("Продукты", 1200, 2), ("Хлеб", 40, 5)])

    def test_leftovers_and_invalid(self):
        items, unparsed = parsing.parse_free_text("31.02 кофе 150\nпросто текст\nкофе 0", T)
        self.assertEqual(items, [])
        self.assertEqual(unparsed, ["31.02 кофе 150", "кофе 0"])

    def test_amount_in_the_middle(self):
        items, unparsed = parsing.parse_free_text("01.09.2026\nМясо 7500 продукты\nМясо продукты 7500", T)
        self.assertEqual(unparsed, [])
        self.assertEqual([(i["name"], i["amount"], i["date"].day) for i in items], [("Мясо продукты", 7500, 1), ("Мясо продукты", 7500, 1)])

    def test_decimal_in_name(self):
        items, _ = parsing.parse_free_text("Молоко 2,5% 930мл 80", T)
        self.assertEqual((items[0]["name"], items[0]["amount"]), ("Молоко 2,5% 930мл", 80))


if __name__ == "__main__":
    unittest.main()
