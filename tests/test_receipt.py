import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("TELEGRAM_TOKEN", "test")
import ai  # noqa: E402
import crypto  # noqa: E402
import receipt  # noqa: E402
import storage  # noqa: E402

QR = "t=20260903T1842&s=517.77&fn=9960440300123456&i=12345&fp=1234567890&n=1"


class ReceiptQrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        crypto.KEY_FILE = storage.Path(self.tmp.name) / ".k"; crypto.reset_cache()
        storage.DB_FILE = storage.Path(self.tmp.name) / "t.db"
        storage.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_and_find(self):
        p = receipt.parse_qr(QR)
        self.assertEqual((p["date"], p["total"], p["fn"]), ("2026-09-03", 518, "9960440300123456"))
        self.assertEqual(receipt.find_qr_text("чек: " + QR + " конец"), QR)
        self.assertIsNone(receipt.find_qr_text("такси 350"))

    def test_resolve_sum_only_without_token(self):
        with mock.patch.object(receipt, "PROVERKACHEKA_TOKEN", ""):
            r = receipt.resolve(None, QR)
        self.assertEqual(r["source"], "qr-sum")
        self.assertEqual(r["items"][0]["amount"], 518)

    def test_resolve_with_service(self):
        payload = {"code": 1, "data": {"json": {"user": 'ООО "Лента"', "dateTime": "2026-09-03T18:42:00", "totalSum": 51777,
                   "items": [{"name": "МОЛОКО 2,5% 930МЛ", "price": 7990, "quantity": 1, "sum": 7990},
                             {"name": "ТАКСИ ЯНДЕКС", "price": 43787, "quantity": 1, "sum": 43787}]}}}
        resp = mock.Mock(); resp.json.return_value = payload
        with mock.patch.object(receipt, "PROVERKACHEKA_TOKEN", "tok"), mock.patch.object(receipt.requests, "post", return_value=resp) as post, \
             mock.patch.object(ai, "POLZA_API_KEY", ""):
            r = receipt.resolve(None, QR)
        self.assertEqual(post.call_args.kwargs["data"]["qrraw"], QR)
        self.assertEqual(r["source"], "qr")
        self.assertEqual(r["store"], 'ООО "Лента"')
        self.assertEqual([(i["name"], i["amount"], i["category"]) for i in r["items"]],
                         [("МОЛОКО 2,5% 930МЛ", 80, "Еда"), ("ТАКСИ ЯНДЕКС", 438, "Транспорт")])

    def test_service_error_falls_back_to_ai_then_sum(self):
        resp = mock.Mock(); resp.json.return_value = {"code": 0, "data": "чек не найден"}
        with mock.patch.object(receipt, "PROVERKACHEKA_TOKEN", "tok"), mock.patch.object(receipt.requests, "post", return_value=resp), \
             mock.patch.object(ai, "parse_receipt", return_value={"store": "Магнит", "date": None, "total": None, "items": [{"name": "Хлеб", "amount": 45, "category": "Еда"}]}), \
             mock.patch.object(receipt, "decode_qr", return_value=QR):
            r = receipt.resolve(b"img", None)
        self.assertEqual(r["source"], "ai")
        self.assertEqual((r["date"], r["total"]), ("2026-09-03", 518))

    def test_classify_many_batches(self):
        with mock.patch.object(ai, "POLZA_API_KEY", "k"), mock.patch.object(ai, "_polza", return_value="1. Другое\n2. Одежда") as m:
            cats = ai.classify_many(["Кофе латте", "Аптека", "Кроссовки Nike", "Штука"])
        self.assertEqual(cats, ["Еда", "Другое", "Одежда", "Одежда"])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(storage.cache_get("аптека"), "Другое")


if __name__ == "__main__":
    unittest.main()
