"""تحليل الصور بنموذج الذكاء الاصطناعي (Gemini) بدل OpenCV/Tesseract — بدون شبكة (عميل وهمي)."""
import io, json, unittest
from decimal import Decimal as D
from types import SimpleNamespace

from PIL import Image

from daftari.ocr.engine import OcrUnavailable
from daftari.ocr.preprocess import ImageError
from daftari.ocr.scanner import InvoiceScanner
from daftari.ocr.vision import extract_json, invoice_from_json


def jpeg(w=1000, h=1400) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), (240, 240, 240)).save(b, "JPEG")
    return b.getvalue()


class FakeClient:
    def __init__(self, text, ready=True):
        self.text, self.ready, self.calls = text, ready, []
        self.cfg = SimpleNamespace(model="gemini-test")

    async def generate(self, contents, system=None, tools=None, google_search=None):
        self.calls.append((contents, system, tools))
        return SimpleNamespace(text=self.text)


INVOICE = {
    "supplier": "شركة النور", "invoice_number": "INV-77", "date": "2026-10-01", "total": 273.5,
    "items": [
        {"name": "فلتر زيت", "code": "TY-100", "qty": 10, "unit_price": 6.5, "line_total": 65},
        {"name": "زيت محرك", "code": "OIL530", "qty": "٦", "unit_price": "21.25", "line_total": "127.50"},
        {"name": "بواجي ايريديوم", "code": "SP-9", "qty": 9, "unit_price": 9, "line_total": 81},
    ],
}


class Parsing(unittest.TestCase):
    def test_extract_json_variants(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('تفضل:\n{"a": [1, 2]}\nانتهى'), {"a": [1, 2]})
        with self.assertRaises(ValueError):
            extract_json("لا شيء هنا")

    def test_clean_invoice(self):
        inv = invoice_from_json(INVOICE)
        self.assertEqual((inv.supplier, inv.invoice_number), ("شركة النور", "INV-77"))
        self.assertEqual(len(inv.rows), 3)
        self.assertEqual((inv.rows[1].qty, inv.rows[1].cost), (D(6), D("21.25")))   # أرقام عربية
        self.assertEqual(inv.computed_total, D("273.50"))
        self.assertEqual(inv.warnings, [])
        self.assertTrue(all(not r.needs_review for r in inv.rows))

    def test_wrong_qty_is_repaired_and_flagged(self):
        d = json.loads(json.dumps(INVOICE))
        d["items"][1]["qty"] = 8                      # النموذج قرأ 6 كـ 8
        inv = invoice_from_json(d)
        self.assertEqual(inv.rows[1].qty, D(6))       # صُحّحت من المجموع 127.50 ÷ 21.25
        self.assertTrue(inv.rows[1].needs_review)
        self.assertFalse(inv.rows[0].needs_review)

    def test_total_mismatch_and_missing_numbers(self):
        d = json.loads(json.dumps(INVOICE))
        d["total"] = 999
        d["items"].append({"name": "صنف باهت", "code": "", "qty": None, "unit_price": None, "line_total": None})
        d["items"].append({"name": "", "code": "", "qty": None, "unit_price": None, "line_total": None})   # فارغ يُتجاهل
        inv = invoice_from_json(d)
        self.assertEqual(len(inv.rows), 4)
        self.assertTrue(inv.rows[3].needs_review)
        self.assertTrue(any("لا يطابق" in w for w in inv.warnings))
        raw = inv.rows[0].to_raw()                    # يغذّي مسار الاستيراد الموجود
        self.assertEqual((raw.name, raw.code), ("فلتر زيت", "TY-100"))

    def test_empty_items_warns(self):
        self.assertTrue(invoice_from_json({"items": []}).warnings)

    def test_bare_list_accepted(self):
        self.assertEqual(len(invoice_from_json(INVOICE["items"]).rows), 3)


class Scanning(unittest.IsolatedAsyncioTestCase):
    async def test_scan_end_to_end(self):
        client = FakeClient("```json\n" + json.dumps(INVOICE, ensure_ascii=False) + "\n```")
        sc = InvoiceScanner(client_factory=lambda: client)
        self.assertTrue(sc.uses_ai)
        png, q = await sc.preview(jpeg())
        self.assertTrue(png[:3] == b"\xff\xd8\xff" and q.ok)
        res = await sc.scan(jpeg())
        self.assertEqual((res.variant, len(res.invoice.rows)), ("vision", 3))
        self.assertIn("Gemini", res.engine)
        part = client.calls[0][0][0]["parts"][0]
        self.assertEqual(part["inlineData"]["mimeType"], "image/jpeg")      # الصورة أُرسلت فعلاً للنموذج

    async def test_no_key_gives_clear_arabic_error(self):
        sc = InvoiceScanner(client_factory=lambda: FakeClient("", ready=False))
        with self.assertRaises(OcrUnavailable) as cm:
            await sc.scan(jpeg())
        self.assertIn("مفتاح Gemini", str(cm.exception))

    async def test_garbage_reply_and_bad_image(self):
        sc = InvoiceScanner(client_factory=lambda: FakeClient("آسف لا أستطيع"))
        with self.assertRaises(OcrUnavailable):
            await sc.scan(jpeg())
        with self.assertRaises(ImageError):
            await InvoiceScanner(client_factory=lambda: FakeClient("{}")).scan(b"not an image")

    async def test_small_image_hint(self):
        sc = InvoiceScanner(client_factory=lambda: FakeClient("{}"))
        _, q = await sc.preview(jpeg(300, 400))
        self.assertTrue(q.hints)

    async def test_barcode_via_model(self):
        sc = InvoiceScanner(client_factory=lambda: FakeClient('{"codes": ["6291041500213", " 6291 0415 00213 ", ""]}'))
        self.assertEqual(await sc.read_barcodes(jpeg(800, 600)), ["6291041500213"])
        sc = InvoiceScanner(client_factory=lambda: FakeClient("لا يوجد"))
        self.assertEqual(await sc.read_barcodes(jpeg(800, 600)), [])


if __name__ == "__main__":
    unittest.main()
