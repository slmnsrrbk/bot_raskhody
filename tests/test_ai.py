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
        self.assertEqual(ai.by_keywords("Аптека"), "Здоровье")
        self.assertEqual(ai.by_keywords("Мясо продукты"), "Продукты")
        self.assertIsNone(ai.by_keywords("Штука-дрюка"))

    def test_llm_then_cache(self):
        with mock.patch.object(ai, "POLZA_API_KEY", "k"), mock.patch.object(ai, "_polza_chat", return_value="Другое.") as m:
            self.assertEqual(ai.detect_category("Штука-дрюка"), "Другое")
            self.assertEqual(ai.detect_category("штука-дрюка "), "Другое")  # второй раз из кэша
            self.assertEqual(m.call_count, 1)

    def test_fallback_model_and_default(self):
        calls = []
        def fake(messages, model, *a, **k):
            calls.append(model)
            if model == ai.POLZA_MODEL:
                raise RuntimeError("down")
            return "Транспорт"
        with mock.patch.object(ai, "POLZA_API_KEY", "k"), mock.patch.object(ai, "_polza_chat", side_effect=fake):
            self.assertEqual(ai.detect_category("Яндекс.Штука"), "Транспорт")
        self.assertEqual(calls, [ai.POLZA_MODEL, ai.POLZA_FALLBACK_MODEL])
        with mock.patch.object(ai, "POLZA_API_KEY", ""), mock.patch.object(ai, "CHAD_API_KEY", ""):
            self.assertEqual(ai.detect_category("Нечто"), "Другое")

    def test_bad_answer_not_cached(self):
        with mock.patch.object(ai, "POLZA_API_KEY", "k"), mock.patch.object(ai, "_polza_chat", return_value="Не знаю"):
            self.assertEqual(ai.detect_category("Штука"), "Другое")
        self.assertIsNone(storage.cache_get("штука"))


if __name__ == "__main__":
    unittest.main()


class TranscribeTests(unittest.TestCase):
    def test_transcribe_uses_first_model_then_fallback(self):
        from unittest import mock

        class Resp:
            def __init__(self, text):
                self._t = text

            def raise_for_status(self):
                pass

            def json(self):
                return {"text": self._t}

        calls = []

        def fake_post(url, **kw):
            calls.append(kw["data"]["model"])
            self.assertTrue(url.endswith("/audio/transcriptions"))
            self.assertEqual(kw["files"]["file"][0], "voice.ogg")
            return Resp("" if len(calls) == 1 else "такси триста пятьдесят")

        with mock.patch.object(ai, "POLZA_API_KEY", "k"), mock.patch.object(ai._session, "post", side_effect=fake_post):
            self.assertEqual(ai.transcribe(b"OggS...", "voice.ogg", "audio/ogg"), "такси триста пятьдесят")
        self.assertEqual(calls, [ai.POLZA_STT_MODEL, ai.POLZA_STT_FALLBACK_MODEL])
        with mock.patch.object(ai, "POLZA_API_KEY", ""):
            self.assertIsNone(ai.transcribe(b"x"))

    def test_spoken_prompt_mentions_words_for_numbers(self):
        import datetime
        from unittest import mock
        seen = {}

        def fake(messages, *a, **kw):
            seen["system"] = messages[0]["content"]
            return '[{"name": "такси", "amount": 350, "date": "2026-09-03"}]'

        with mock.patch.object(ai, "POLZA_API_KEY", "k"), mock.patch.object(ai, "_polza_chat", side_effect=fake):
            out = ai.parse_expenses_text("потратил на такси триста пятьдесят", datetime.date(2026, 9, 3), spoken=True)
        self.assertEqual(out, [{"name": "такси", "amount": 350, "date": "2026-09-03", "category": None, "currency": None}])
        self.assertIn("триста пятьдесят", seen["system"])
