import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("TELEGRAM_TOKEN", "test")
import ai  # noqa: E402
import crypto  # noqa: E402
import storage  # noqa: E402


class AiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        storage.DB_FILE = storage.Path(self.tmp.name) / "t.db"
        crypto.KEY_FILE = storage.Path(self.tmp.name) / ".k"; crypto.reset_cache()
        storage.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_keywords(self):
        self.assertEqual(ai.by_keywords("Кофе с собой"), "Еда")
        self.assertEqual(ai.by_keywords("такси домой"), "Транспорт")
        self.assertEqual(ai.by_keywords("Кроссовки Nike"), "Одежда")
        self.assertEqual(ai.by_keywords("Билет в кино"), "Развлечения")
        self.assertIsNone(ai.by_keywords("Аптека"))

    def test_llm_then_cache(self):
        with mock.patch.object(ai, "POLZA_API_KEY", "k"), mock.patch.object(ai, "_polza_chat", return_value="Другое.") as m:
            self.assertEqual(ai.detect_category("Аптека"), "Другое")
            self.assertEqual(ai.detect_category("аптека "), "Другое")  # второй раз из кэша
            self.assertEqual(m.call_count, 1)

    def test_fallback_model_and_default(self):
        calls = []
        def fake(messages, model, *a, **k):
            calls.append(model)
            if model == ai.POLZA_MODEL:
                raise RuntimeError("down")
            return "Транспорт"
        with mock.patch.object(ai, "POLZA_API_KEY", "k"), mock.patch.object(ai, "_polza_chat", side_effect=fake):
            self.assertEqual(ai.detect_category("Яндекс.Драйв"), "Транспорт")
        self.assertEqual(calls, [ai.POLZA_MODEL, ai.POLZA_FALLBACK_MODEL])
        with mock.patch.object(ai, "POLZA_API_KEY", ""), mock.patch.object(ai, "CHAD_API_KEY", ""):
            self.assertEqual(ai.detect_category("Нечто"), "Другое")

    def test_bad_answer_not_cached(self):
        with mock.patch.object(ai, "POLZA_API_KEY", "k"), mock.patch.object(ai, "_polza_chat", return_value="Не знаю"):
            self.assertEqual(ai.detect_category("Штука"), "Другое")
        self.assertIsNone(storage.cache_get("штука"))


if __name__ == "__main__":
    unittest.main()
