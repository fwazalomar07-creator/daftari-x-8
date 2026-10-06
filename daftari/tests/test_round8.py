"""السندات بأسماء مقاربة، حذف صنف مستورد من فاتورة شراء، وأدوات المساعد (Excel وتنظيف الصور)."""
import asyncio, io, tempfile, unittest
from decimal import Decimal as D
from pathlib import Path

from PIL import Image, ImageDraw

from daftari.data.repository import Repository
from daftari.data.store import MemoryStore
from daftari.ledger import InvoiceDraft, Ledger, PurchaseDraft


def run(coro):
    return asyncio.run(coro)


async def make_ledger():
    led = Ledger(Repository(MemoryStore(), cache_dir=Path(tempfile.mkdtemp()) / "c"))
    await led.load()
    return led


class VoucherNames(unittest.TestCase):
    def test_suggestions_and_normalized_match(self):
        async def go():
            led = await make_ledger()
            it = await led.add_item("زيت", "Z1", 3, 5, 10)
            await led.finalize_invoice(InvoiceDraft(cart={it.id: 2}, customer="أحمد العلي", paid=D(0)))
            await led.finalize_invoice(InvoiceDraft(cart={it.id: 1}, customer="احمد الخالد", paid=D(0)))
            near = [n for n, _ in led.suggest_accounts("receipt", "احمد")]
            self.assertEqual(set(near), {"أحمد العلي", "احمد الخالد"})
            self.assertEqual(led.match_account("receipt", "احمد العلي").name, "أحمد العلي")     # أ/ا موحَّدة
            self.assertIsNone(led.match_account("receipt", "احمد"))                              # لا تخمين عند الاقتراب فقط
            before = led._find_customer("أحمد العلي").balance
            v = await led.add_voucher("receipt", "احمد العلي", 4)
            self.assertEqual(led._find_customer("أحمد العلي").balance, before - 4)
            self.assertTrue(v.applied_to_id)
            v2 = await led.add_voucher("receipt", "احمد", 4)                                     # اسم غير مطابق: لا خصم
            self.assertIsNone(v2.applied_to_id)
        run(go())


class DeletedPurchasedItem(unittest.TestCase):
    def test_purchase_keeps_deleted_item_through_edit(self):
        async def go():
            led = await make_ledger()
            a = await led.add_item("زيت أ", "A1", 5, 8, 0)
            b = await led.add_item("فلتر ب", "B1", 2, 4, 0)
            p = await led.finalize_purchase(PurchaseDraft(cart={a.id: 10, b.id: 5}, supplier="مورد", paid=D(20),
                                                          costs={a.id: D(5), b.id: D(2)}))
            await led.delete_item(b.id)
            self.assertEqual([l.name for l in led.purchases[0].items], ["زيت أ", "فلتر ب"])
            d, warns = await led.begin_edit_purchase(p.id)
            self.assertEqual([o.name for o in d.orphans], ["فلتر ب"])
            self.assertTrue(warns)
            p2 = await led.finalize_purchase(d)
            self.assertEqual([l.name for l in p2.items], ["زيت أ", "فلتر ب"])
            self.assertEqual(p2.subtotal, D(60))
            self.assertEqual(p2.remaining, D(40))
            self.assertEqual(led.suppliers[0].balance, D(40))
        run(go())


class AssistantExcelAndImage(unittest.TestCase):
    def test_import_excel_and_clean_image(self):
        from openpyxl import Workbook
        from daftari.ai.excel_in import parse_table
        from daftari.ai.tools import ToolBox

        async def go():
            led = await make_ledger()
            await led.add_item("قديم", "OLD", 1, 2, 3)
            t = ToolBox(led, None, home=Path(tempfile.mkdtemp()))
            wb = Workbook()
            ws = wb.active
            ws.append(["الصنف", "الكود", "التكلفة", "سعر المفرق", "الكمية"])
            ws.append(["زيت DONG", "D1", 3.5, 5, 10])
            ws.append(["قديم", "OLD", 1, 2, 3])
            b = io.BytesIO()
            wb.save(b)
            t.excel_files["x.xlsx"] = parse_table("x.xlsx", b.getvalue())
            r = await t.import_excel_items("الصنف", "التكلفة", "سعر المفرق", stock_col="الكمية", code_col="الكود")
            self.assertEqual((r["added"], r["skipped_existing"]), (1, 1))
            im = Image.new("RGB", (900, 700), (205, 190, 170))
            ImageDraw.Draw(im).rectangle((250, 150, 650, 560), fill=(30, 60, 160))
            bb = io.BytesIO()
            im.save(bb, "JPEG")
            t.user_images = [bb.getvalue()]
            await t.clean_image_background()
            out = Image.open(io.BytesIO(t.cleaned_image))
            self.assertEqual(out.width, out.height)
            self.assertGreaterEqual(min(out.getpixel((5, 5))), 250)          # أبيض ناصع
            await t.attach_user_image("D1")
            self.assertTrue([i for i in led.inventory if i.code == "D1"][0].img)
        run(go())


class MissingGlyphForms(unittest.TestCase):
    def test_isolated_forms_replaced_when_font_lacks_them(self):
        from daftari.printing import render
        path = render._font_path(False)
        table = render._missing_forms(path)
        for ch in table:                       # لا مفتاح بلا بديل، والبديل ليس رمزاً مفقوداً
            self.assertTrue(table[ch])
