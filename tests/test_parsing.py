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

    def test_thousand_separators(self):
        """«1.000₽» — тысяча: точка, пробел и апостроф разделяют разряды."""
        items, unparsed = parsing.parse_free_text("Развлечение 1.000₽\nКомпы 1.000₽\nНоут 1'500\nМясо 12.500 руб\nМашина 1 234 567", T)
        self.assertEqual(unparsed, [])
        self.assertEqual([(i["name"], i["amount"]) for i in items],
                         [("Развлечение", 1000), ("Компы", 1000), ("Ноут", 1500), ("Мясо", 12500), ("Машина", 1234567)])

    def test_short_amounts(self):
        items, unparsed = parsing.parse_free_text("такси 1,5к\n2 тыс продукты\nРемонт 3 млн", T)
        self.assertEqual(unparsed, [])
        self.assertEqual([(i["name"], i["amount"]) for i in items], [("Такси", 1500), ("Продукты", 2000), ("Ремонт", 3000000)])

    def test_multiplier_is_not_eaten_from_the_name(self):
        items, _ = parsing.parse_free_text("350 кофе\n700 кино", T)
        self.assertEqual([(i["name"], i["amount"]) for i in items], [("Кофе", 350), ("Кино", 700)])

    def test_currency_from_text(self):
        items, _ = parsing.parse_free_text("Отель 100$\nУжин 25 €\nТакси 2 000₸\nХлеб 40", T)
        self.assertEqual([(i["amount"], i.get("currency")) for i in items],
                         [(100, "USD"), (25, "EUR"), (2000, "KZT"), (40, None)])

    def test_to_amount(self):
        for raw, want in [("1.000", 1000), ("1 000", 1000), ("12.500", 12500), ("1,5к", 1500), ("2 тыс", 2000),
                          ("150,50", 150), ("1.000.000", 1000000), ("1,234.56", 1235), ("1.234,56", 1235),
                          (1000, 1000), ("0", None), ("-5", None), ("три", None)]:
            self.assertEqual(parsing.to_amount(raw), want, raw)


if __name__ == "__main__":
    unittest.main()
