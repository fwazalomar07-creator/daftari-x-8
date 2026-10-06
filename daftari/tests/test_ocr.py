import asyncio, shutil, unittest
from decimal import Decimal as D

from daftari.ocr.engine import Word
from daftari.ocr.parser import numeric_value, parse_invoice, reconcile, detect_header, group_rows


def W(text, x, y, conf=92, w=None, h=24):
    return Word(text, conf, x, y, w or 12 * len(text), h)


def arabic_invoice():
    """فاتورة عربية RTL: الأعمدة من اليمين: الصنف، الكود، الكمية، السعر، المجموع."""
    ws = [W("رقم", 700, 40), W("الفاتورة:", 640, 40), W("INV-77", 560, 40),
          W("الصنف", 760, 120), W("الكود", 560, 120), W("الكمية", 420, 120), W("السعر", 300, 120), W("المجموع", 160, 120)]
    data = [("فلتر", "زيت", "TY-100", "١٠", "٦٫٥٠", "٦٥٫٠٠"),
            ("زيت", "محرك", "OIL530", "6", "21.25", "127.50"),
            ("بواجي", "ايريديوم", "SP-9", "9", "9.00", "81.00")]
    y = 180
    for a, b, code, q, u, t in data:
        ws += [W(a, 800, y), W(b, 740, y), W(code, 560, y), W(q, 430, y, w=20), W(u, 305, y, w=50), W(t, 170, y, w=60)]
        y += 50
    ws += [W("المجموع", 300, y + 20), W("الكلي", 240, y + 20), W("273.50", 160, y + 20, w=70)]
    return ws


class NumberTests(unittest.TestCase):
    def test_numeric(self):
        self.assertEqual(numeric_value("١٢٫٥"), D("12.5"))
        self.assertEqual(numeric_value("1,250.00"), D("1250.00"))
        self.assertEqual(numeric_value("1O.5"), D("10.5"))        # O -> 0 في رمز أغلبه أرقام
        self.assertEqual(numeric_value("25 ل.س"), D("25"))
        self.assertIsNone(numeric_value("OIL530"))                # لا نُفسد الأكواد
        self.assertIsNone(numeric_value("5W30"))
        self.assertIsNone(numeric_value("B100"))                  # يبدأ بحرف: كود وليس رقماً

    def test_reconcile(self):
        q, c, t, iss = reconcile(D(10), D("6.5"), D(65));  self.assertEqual(iss, [])
        q, c, t, iss = reconcile(D(1), D("6.5"), D(65));   self.assertEqual(q, D(10)); self.assertTrue(iss)  # كمية مقروءة خطأ
        q, c, t, iss = reconcile(D(10), D("8.5"), D(65));  self.assertEqual(c, D("6.50")); self.assertTrue(iss)  # سعر خطأ
        q, c, t, iss = reconcile(D(3), D(2), None);        self.assertEqual(t, D(6))


class ParserTests(unittest.TestCase):
    def test_arabic_rtl_invoice(self):
        inv = parse_invoice(arabic_invoice(), page_width=900)
        self.assertTrue(inv.header_found)
        self.assertEqual(inv.invoice_number, "INV-77")
        self.assertEqual(len(inv.rows), 3)
        r0 = inv.rows[0]
        self.assertEqual((r0.name, r0.code, r0.qty, r0.cost, r0.total), ("فلتر زيت", "TY-100", D(10), D("6.50"), D("65.00")))
        self.assertEqual(inv.stated_total, D("273.50"))
        self.assertEqual(inv.computed_total, D("273.50"))
        self.assertEqual(inv.warnings, [])
        self.assertTrue(all(not r.needs_review for r in inv.rows))

    def test_total_mismatch_warns(self):
        ws = arabic_invoice()
        ws[-1] = W("999.00", 160, ws[-1].y, w=70)
        inv = parse_invoice(ws, page_width=900)
        self.assertTrue(any("لا يطابق" in w for w in inv.warnings))

    def test_misread_qty_is_repaired_and_flagged(self):
        ws = arabic_invoice()
        qty2 = next(w for w in ws if w.text == "6")   # كمية السطر الثاني: قُرئت 6 كـ 8
        qty2.text = "8"
        inv = parse_invoice(ws, page_width=900)
        row = inv.rows[1]
        self.assertEqual(row.qty, D(6))               # صُحّحت من المجموع 127.50 ÷ 21.25
        self.assertTrue(row.needs_review and row.issues)
        self.assertFalse(inv.rows[0].needs_review)

    def test_no_header_infers_by_arithmetic(self):
        ws = []
        y = 100
        for n, q, u, t in [("Spark Plug", "4", "9.00", "36.00"), ("Wiper Blade", "2", "15.00", "30.00")]:
            ws += [W(n, 50, y, w=200), W(q, 400, y, w=20), W(u, 500, y, w=50), W(t, 620, y, w=50)]
            y += 50
        inv = parse_invoice(ws, page_width=800)
        self.assertFalse(inv.header_found)
        self.assertEqual([(r.qty, r.cost) for r in inv.rows], [(D(4), D(9)), (D(2), D(15))])
        self.assertTrue(any("ترويسة" in w for w in inv.warnings))

    def test_empty(self):
        self.assertTrue(parse_invoice([]).warnings)

    def test_to_raw_feeds_ledger_import(self):
        from daftari.core.search import match_import_rows
        from daftari.core.models import InventoryItem
        inv = parse_invoice(arabic_invoice(), page_width=900)
        res = match_import_rows([r.to_raw() for r in inv.rows], [InventoryItem(id="1", name="x", code="ty-100")])
        self.assertEqual((len(res.matched), len(res.pending)), (1, 2))


@unittest.skipUnless(shutil.which("tesseract"), "Tesseract غير مثبت")
class EndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_photo_of_invoice(self):
        from daftari.tests.make_sample import make
        from daftari.ocr.engine import TesseractEngine
        from daftari.ocr.scanner import InvoiceScanner
        make("/tmp/_c.png", "/tmp/_p.jpg")
        res = await InvoiceScanner(TesseractEngine(lang="eng")).scan(open("/tmp/_p.jpg", "rb").read())
        inv = res.invoice
        self.assertTrue(res.quality.document_found)
        self.assertEqual(len(inv.rows), 4)
        self.assertEqual(inv.computed_total, D("310.50"))
        self.assertEqual(inv.rows[2].code, "OIL530")
        self.assertEqual(inv.warnings, [])


if __name__ == "__main__":
    unittest.main()
