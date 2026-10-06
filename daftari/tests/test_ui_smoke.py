"""تشغيل كل شاشة بمكتبة flet وهمية على بيانات حقيقية، ثم محاكاة النقرات الرئيسية.
يمسك الأخطاء البرمجية (NameError/AttributeError/منطق) — ولا يغني عن تجربة Flet الحقيقي على جهازك."""
import asyncio, tempfile, unittest
from decimal import Decimal as D
from pathlib import Path

from daftari.tests import fake_flet
fake_flet.install()

from daftari.data.drafts import DraftStore
from daftari.data.repository import Repository
from daftari.data.store import MemoryStore
from daftari.ledger import InvoiceDraft, Ledger, PurchaseDraft
from daftari.ui.app import App, MAIN_NAV, MORE_NAV, BOTTOM_TABS


def click(ctl):
    """يشغّل on_click لعنصر (يدعم الدوال المتزامنة وغير المتزامنة)."""
    return ctl.on_click(None)


def walk(ctl, depth=0):
    if depth > 60 or ctl is None:
        return
    yield ctl
    kids = []
    for attr in ("content", "controls"):
        v = getattr(ctl, attr, None)
        if isinstance(v, (list, tuple)):
            kids += list(v)
        elif v is not None:
            kids.append(v)
    for k in kids:
        if hasattr(k, "kw"):
            yield from walk(k, depth + 1)


class Smoke(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.led = Ledger(Repository(MemoryStore(), cache_dir=home / "c"))
        await self.led.load()
        it = await self.led.add_item("فلتر زيت تويوتا", "TY-100", 6, 10, 20, 9, 9.5)
        await self.led.add_item("زيت محرك 5W30", "OIL530", 30, 25, 3)  # خسارة + مخزون منخفض
        inv, _ = await self.led.finalize_invoice(InvoiceDraft(cart={it.id: 4}, customer="أبو أحمد", paid=D(10), discount=D(2)))
        self.inv = inv
        await self.led.finalize_purchase(PurchaseDraft(cart={it.id: 5}, supplier="مورد 1", paid=D(10), costs={it.id: D(7)}))
        c = self.led.customers[0]
        await self.led.record_customer_payment(c.id, 5)
        await self.led.add_voucher("payment", "شخص", 15, "ملاحظة")
        await self.led.add_expense("rent", 100, "شهر 9", "شهري")
        self.page = fake_flet.Page()
        self.app = App(self.page, self.led, DraftStore(home / "d"), home / "x")
        self.app.unlocked = True

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_every_screen_builds(self):
        for key in [k for k, _, _ in BOTTOM_TABS] + [k for k, _, _ in MAIN_NAV + MORE_NAV if k != "scan"]:
            self.app.tab, self.app.sub = key, {}
            ctl = await self.app._build_current()
            self.assertIsNotNone(ctl, key)
            self.assertGreater(len(list(walk(ctl))), 1, key)

    async def test_sub_views_and_documents(self):
        pur = self.led.purchases[0]
        for tab, sub in [("purchases", {"pricing": pur.id}), ("profit", {"day": __import__("datetime").datetime.now().strftime("%Y-%m-%d")})]:
            self.app.tab, self.app.sub = tab, sub
            await self.app._build_current()
        from daftari.printing.render import invoice_doc
        await self.app.show_document(invoice_doc(self.inv, self.led.settings), "INV", back=("history", {}))
        self.assertIn("doc", self.app.sub)
        ctl = await self.app._build_current()
        self.assertGreater(len(list(walk(ctl))), 3)

    async def test_click_flows(self):
        # فاتورة: إضافة صنف للسلة ثم إنهاء → مستند
        self.app.tab, self.app.sub = "invoice", {}
        d = self.app.invoice_draft
        it = self.led.inventory[0]
        self.assertTrue(d.inc(it))
        from daftari.ui.tabs import invoice as invoice_tab
        await invoice_tab.finalize(self.app)
        self.assertEqual(len(self.led.invoices), 2)
        self.assertIn("doc", self.app.sub)

        # سجل الفواتير: ثلاث نقرات لكل بطاقة (عرض، تعديل، حالة)
        self.app.tab, self.app.sub = "history", {}
        ctl = await self.app._build_current()
        buttons = [c for c in walk(ctl) if getattr(c, "on_click", None) and getattr(c, "args", None) and c.args[0] == "تعديل"]
        self.assertTrue(buttons)
        await buttons[0].on_click(None)
        self.assertTrue(self.app.invoice_draft.editing_id)
        self.assertEqual(self.app.tab, "invoice")
        # بناء شاشة الفاتورة أثناء التعديل ثم تراجع
        await self.app._build_current()
        await self.led.cancel_edit_invoice(self.app.invoice_draft)

    async def test_lock_screen(self):
        await self.led.set_password("1234")
        self.app.unlocked = False
        await self.app.render()
        self.assertIsNotNone(self.app.body.content)
        self.assertTrue(self.led.check_password("1234"))

    async def test_draft_persistence_roundtrip(self):
        self.app.invoice_draft.inc(self.led.inventory[0])
        self.app.drafts.save_invoice(self.app.invoice_draft)
        again = App(self.page, self.led, self.app.drafts, self.app.work_dir)
        self.assertTrue(again.recovered)
        self.assertEqual(again.invoice_draft.cart, self.app.invoice_draft.cart)


if __name__ == "__main__":
    unittest.main()


def find_btn(ctl, label):
    return next(c for c in walk(ctl) if getattr(c, "on_click", None) and getattr(c, "args", None) and c.args[0] == label)


class DialogFlows(Smoke):
    async def submit(self, values: list, which=-1):
        """يملأ حقول آخر حوار بالترتيب ثم يضغط زر التأكيد (آخر action)."""
        dlg = self.page.dialogs[-1]
        fields = [c for c in walk(dlg.content) if hasattr(c, "kw") and "label" in c.kw and "keyboard_type" in c.kw]
        for f, v in zip(fields, values):
            f.value = v
        await dlg.actions[which].on_click(None)

    async def test_add_item_via_form(self):
        self.app.tab, self.app.sub = "inventory", {}
        ctl = await self.app._build_current()
        await find_btn(ctl, "➕ صنف جديد").on_click(None) if asyncio.iscoroutinefunction(find_btn(ctl, "➕ صنف جديد").on_click) \
            else find_btn(ctl, "➕ صنف جديد").on_click(None)
        await self.submit(["بواجي", "SP-1", "٣", "٥", "", "", "٧"])
        it = next(i for i in self.led.inventory if i.name == "بواجي")
        self.assertEqual((it.cost, it.price, it.price_wholesale, it.stock), (D(3), D(5), D(5), 7))  # الأرقام العربية + الافتراضي = المفرق

    async def test_form_error_keeps_dialog_open(self):
        self.app.tab, self.app.sub = "inventory", {}
        ctl = await self.app._build_current()
        find_btn(ctl, "➕ صنف جديد").on_click(None)
        n = len(self.led.inventory)
        await self.submit(["", "", "1", "2", "", "", "1"])   # اسم فارغ
        self.assertEqual(len(self.led.inventory), n)
        self.assertEqual(len(self.page.dialogs), 1)           # بقي الحوار مفتوحاً مع رسالة الخطأ

    async def test_customer_payment_and_edit(self):
        self.app.tab, self.app.sub = "customers", {}
        ctl = await self.app._build_current()
        c = self.led.customers[0]
        before = c.balance
        find_btn(ctl, "سند قبض").on_click(None)
        await self.submit(["2"])
        self.assertEqual(self.led.customers[0].balance, before - 2)
        self.assertTrue(self.page.tasks)                       # عرض مستند السند مجدول بعد الإغلاق
        for fn, a in list(self.page.tasks):                    # كل المهام المجدولة (عرض السند + حركات الدخول)
            await fn(*a)
        self.assertIn("doc", self.app.sub)

    async def test_expense_voucher_supplier_forms(self):
        self.app.tab, self.app.sub = "vouchers", {}
        ctl = await self.app._build_current()
        find_btn(ctl, "🧾 سند قبض").on_click(None)
        await self.submit(["زبون جديد", "50", "", "200", "دفعة"])
        v = self.led.vouchers[0]
        self.assertEqual((v.person, v.amount, v.previous_balance, v.remaining_balance), ("زبون جديد", D(50), D(200), D(150)))
        self.app.tab, self.app.sub = "suppliers", {}
        ctl = await self.app._build_current()
        before = self.led.suppliers[0].balance
        find_btn(ctl, "تسجيل دفعة").on_click(None)
        await self.submit(["3"])
        self.assertEqual(self.led.suppliers[0].balance, before - 3)
        self.assertEqual(self.led.vouchers[0].type, "payment")

    async def test_purchase_new_item_and_finalize(self):
        self.app.tab, self.app.sub = "purchases", {}
        ctl = await self.app._build_current()
        find_btn(ctl, "+ صنف جديد (غير موجود بالمخزون)").on_click(None)
        await self.submit(["وايبر", "W1", "4", "2.5", "", "", "6"])
        d = self.app.purchase_draft
        self.assertEqual(sum(d.cart.values()), 4)
        d.supplier = "مورد 2"
        ctl = await self.app._build_current()
        await find_btn(ctl, "✔ تم").on_click(None)
        self.assertEqual(self.led.purchases[0].supplier, "مورد 2")
        self.assertEqual(next(i for i in self.led.inventory if i.name == "وايبر").stock, 4)
        self.assertFalse(any(q > 0 for q in self.app.purchase_draft.cart.values()))

    async def test_settings_backup_roundtrip_through_ui(self):
        self.app.tab, self.app.sub = "settings", {}
        ctl = await self.app._build_current()
        find_btn(ctl, "تغيير / إلغاء كلمة المرور").on_click(None)
        await self.submit(["secret"])
        self.assertTrue(self.led.check_password("secret"))
        await find_btn(ctl, "⬇ تصدير نسخة").on_click(None)


if __name__ == "__main__":
    unittest.main()
