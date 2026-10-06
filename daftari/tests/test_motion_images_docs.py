"""اختبارات: الحركة البطيئة، صور المشتريات الكبيرة، صورة الفاتورة الأكبر، وإنشاء المساعد للفواتير والسندات."""
import io, tempfile, unittest
from decimal import Decimal as D
from pathlib import Path

from daftari.tests.test_ui_smoke import Smoke, walk, find_btn   # يثبّت flet الوهمية أيضاً
from daftari.ai.tools import ToolBox
from daftari.data.repository import Repository
from daftari.data.store import MemoryStore
from daftari.ledger import Ledger
from daftari.ui import widgets as w


def _jpeg(color=(200, 30, 30), size=(300, 200)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


class SlowMotion(Smoke):
    async def test_navigation_uses_fade_and_spec_timing(self):
        sw = self.app._switcher
        self.assertEqual(sw.kw.get("transition") or sw.transition, "FADE")
        self.assertEqual(sw.kw.get("switch_in_curve") or sw.switch_in_curve, "EASE_OUT")
        self.assertEqual(sw.kw.get("switch_out_curve") or sw.switch_out_curve, "EASE_IN")
        await self.app.go("inventory")
        self.assertEqual(sw.duration, w.ms(w.NAV_FADE_MS))
        self.assertEqual(w.NAV_FADE_MS, 120)
        inner = sw.content
        slide = inner.animate_offset.args[0] if hasattr(inner.animate_offset, "args") else inner.animate_offset
        self.assertGreaterEqual(slide, 100)
        self.assertTrue(self.page.tasks)                          # مهمة «الاستقرار» مجدولة بعد الرسم

    async def test_same_section_rerender_is_instant(self):
        await self.app.go("inventory")
        await self.app.after_change()                             # إعادة رسم بعد حفظ: بلا حركة
        self.assertEqual(self.app._switcher.duration, 1)
        self.assertFalse(self.app.entering)

    async def test_reveal_staggers_item_cards_only_on_entry(self):
        for i in range(30):
            await self.led.add_item(f"صنف {i}", f"C{i}", 1, 2, 5)
        await self.app.go("invoice")
        ctl = await self.app._build_current()                     # entering=True
        tiles = [c for c in walk(ctl) if getattr(c, "kw", {}).get("width") == 176]
        hidden = [t for t in tiles if t.opacity == 0]
        self.assertEqual(len(hidden), w.ITEM_REVEAL_MAX)          # أول 8 بطاقات فقط تتحرك
        self.assertTrue(all(t.animate_opacity for t in hidden))

    async def test_no_reveal_when_not_entering(self):
        self.app.entering = False
        ctl = await self.app._build_current()
        self.app.tab, self.app.sub = "invoice", {}
        ctl = await self.app._build_current()
        tiles = [c for c in walk(ctl) if getattr(c, "kw", {}).get("width") == 176]
        self.assertTrue(tiles)
        self.assertFalse([t for t in tiles if t.opacity == 0])

    async def test_motion_scale_applies(self):
        self.assertEqual(w.ms(1000), int(1000 * w.MOTION_SCALE))

    async def test_cards_have_implicit_animation_and_hover(self):
        c = w.card(__import__("flet").Text("x"))
        self.assertTrue(c.animate)
        self.assertTrue(c.on_hover)


class PurchaseImages(Smoke):
    async def test_purchase_cards_are_big_with_view_image_button(self):
        it = self.led.inventory[0]
        await self.app.images.put(it.id, _jpeg())
        self.app.tab, self.app.sub = "purchases", {}
        ctl = await self.app._build_current()
        big = [c for c in walk(ctl) if getattr(c, "kw", {}).get("width") == 124]
        self.assertTrue(big)                                       # صورة 124px (كانت 104 ثم 48)
        zoom = find_btn(ctl, "عرض الصورة")
        zoom.on_click(None)                                        # يفتح نافذة الصورة الكبيرة
        self.assertTrue(self.page.dialogs)
        dlg = self.page.dialogs[-1]
        self.assertTrue([c for c in walk(dlg.content) if getattr(c, "kw", {}).get("width") == 320])

    async def test_view_image_without_image_just_toasts(self):
        it = self.led.inventory[0]
        w.show_image(self.app, it)
        self.assertTrue(self.page.dialogs)                         # snackbar تنبيه، بلا انهيار


class PrintedImages(Smoke):
    async def test_invoice_image_is_larger_than_before(self):
        from daftari.printing.render import PRINT_IMG_SIZE, invoice_doc
        self.assertGreaterEqual(PRINT_IMG_SIZE, 100)
        it = self.led.inventory[0]
        img = _jpeg((255, 0, 0), (320, 320))
        inv = self.led.invoices[0]
        with_img = invoice_doc(inv, self.led.settings, {it.id: img}).pages[0]
        without = invoice_doc(inv, self.led.settings).pages[0]
        red = lambda im: sum(1 for px in im.getdata() if px[0] > 220 and px[1] < 40 and px[2] < 40)
        self.assertGreater(red(with_img), 90 * 90)               # مساحة حمراء ≥ 90×90 بكسل
        self.assertEqual(red(without), 0)

    async def test_small_image_is_scaled_up_to_fill(self):
        from daftari.printing.render import PRINT_IMG_SIZE, invoice_doc
        it = self.led.inventory[0]
        tiny = _jpeg((255, 0, 0), (60, 60))
        page = invoice_doc(self.led.invoices[0], self.led.settings, {it.id: tiny}).pages[0]
        red = sum(1 for px in page.getdata() if px[0] > 200 and px[1] < 60 and px[2] < 60)
        self.assertGreater(red, (PRINT_IMG_SIZE - 20) ** 2 // 2)


class AssistantCreatesDocs(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.led = Ledger(Repository(MemoryStore(), cache_dir=home / "c"))
        await self.led.load()
        self.a = await self.led.add_item("فلتر زيت", "F1", 5, 9, 10, 8, 8.5)
        self.b = await self.led.add_item("شمعات", "S1", 2, 4, 3)
        await self.led.add_customer("أبو أحمد", "0999")
        await self.led.add_supplier("مورد 1", 100)
        self.asked: list[str] = []

        async def confirm(msg):
            self.asked.append(msg)
            return True
        self.tb = ToolBox(self.led, None, home, confirm)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_tools_registered_as_write_tools(self):
        for n in ("create_invoice", "create_voucher"):
            self.assertTrue(self.tb.tools[n].write)

    async def test_create_invoice_reduces_stock_and_records_debt(self):
        r = await self.tb.execute("create_invoice", {
            "items": [{"item": "F1", "qty": 2}, {"item": self.b.id, "qty": 1, "price": 5}],
            "customer": "أبو أحمد", "discount": 1, "paid": 10})
        self.assertTrue(r["ok"], r)
        res = r["result"]
        self.assertEqual(res["total"], 2 * 9 + 5 - 1)              # 22
        self.assertEqual(res["remaining"], 12)
        self.assertEqual(self.led._item(self.a.id).stock, 8)
        self.assertEqual(self.led._item(self.b.id).stock, 2)
        self.assertEqual(self.led.customers[0].balance, 12)
        self.assertEqual(self.led.invoices[0].number, res["number"])
        self.assertEqual(self.tb.documents[-1]["kind"], "invoice")
        self.assertIn("الإجمالي 22", self.asked[0])                # الموافقة تعرض تفاصيل كاملة قبل التنفيذ

    async def test_wholesale_tier_and_full_payment_default(self):
        r = await self.tb.execute("create_invoice", {"items": [{"item": "F1", "qty": 1}], "price_tier": "wholesale"})
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["result"]["total"], r["result"]["remaining"]), (8, 0))

    async def test_rejects_when_stock_not_enough_and_changes_nothing(self):
        r = await self.tb.execute("create_invoice", {"items": [{"item": "S1", "qty": 9}]})
        self.assertFalse(r["ok"])
        self.assertIn("المتوفر", r["error"])
        self.assertEqual(self.led._item(self.b.id).stock, 3)
        self.assertEqual(len(self.led.invoices), 0)

    async def test_rejects_same_item_twice_exceeding_stock(self):
        r = await self.tb.execute("create_invoice", {"items": [{"item": "S1", "qty": 2}, {"item": "S1", "qty": 2}]})
        self.assertFalse(r["ok"])
        self.assertEqual(len(self.led.invoices), 0)

    async def test_rejects_ambiguous_or_empty(self):
        self.assertFalse((await self.tb.execute("create_invoice", {"items": []}))["ok"])
        self.assertFalse((await self.tb.execute("create_invoice", {"items": [{"item": "غير موجود", "qty": 1}]}))["ok"])
        self.assertFalse((await self.tb.execute("create_invoice", {"items": [{"item": "F1", "qty": 0}]}))["ok"])
        self.assertFalse((await self.tb.execute("create_invoice", {"items": [{"item": "F1", "qty": 1}], "paid": -5}))["ok"])

    async def test_user_rejection_creates_nothing(self):
        async def no(_): return False
        tb = ToolBox(self.led, None, Path(self.tmp.name), no)
        r = await tb.execute("create_invoice", {"items": [{"item": "F1", "qty": 1}]})
        self.assertTrue(r.get("rejected"))
        self.assertEqual(len(self.led.invoices), 0)

    async def test_extra_service_line(self):
        r = await self.tb.execute("create_invoice", {"items": [], "extra_lines": [{"name": "أجرة تركيب", "price": 7}]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["total"], 7)

    async def test_receipt_voucher_reduces_customer_debt(self):
        await self.tb.execute("create_invoice", {"items": [{"item": "F1", "qty": 2}], "customer": "أبو أحمد", "paid": 0})
        r = await self.tb.execute("create_voucher", {"voucher_type": "receipt", "person": "أبو أحمد", "amount": 10, "note": "دفعة"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["type"], "سند قبض")
        self.assertEqual(self.led.customers[0].balance, 8)
        self.assertEqual(self.led.vouchers[0].type, "receipt")
        self.assertEqual(self.tb.documents[-1]["kind"], "voucher")

    async def test_payment_voucher_reduces_supplier_debt(self):
        r = await self.tb.execute("create_voucher", {"voucher_type": "payment", "person": "مورد 1", "amount": 40})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["type"], "سند دفع")
        self.assertEqual(self.led.suppliers[0].balance, 60)

    async def test_voucher_validation(self):
        self.assertFalse((await self.tb.execute("create_voucher", {"voucher_type": "xyz", "person": "س", "amount": 5}))["ok"])
        self.assertFalse((await self.tb.execute("create_voucher", {"voucher_type": "receipt", "person": "س", "amount": 0}))["ok"])
        self.assertEqual(len(self.led.vouchers), 0)

    async def test_doc_card_builds_and_opens_document(self):
        from daftari.tests import fake_flet
        from daftari.data.drafts import DraftStore
        from daftari.ui.app import App
        from daftari.ui.tabs import assistant as tab
        await self.tb.execute("create_invoice", {"items": [{"item": "F1", "qty": 1}]})
        await self.tb.execute("create_voucher", {"voucher_type": "receipt", "person": "زيد", "amount": 3})
        page = fake_flet.Page()
        app = App(page, self.led, DraftStore(Path(self.tmp.name) / "d"), Path(self.tmp.name) / "x")
        for d in self.tb.documents:
            card = tab._doc_card(app, f'{d["kind"]}:{d["id"]}')
            btn = find_btn(card, "عرض / طباعة")
            await btn.on_click(None)
            self.assertIn("doc", app.sub)
            self.assertEqual(app.doc_return[0], "assistant")


if __name__ == "__main__":
    unittest.main()
