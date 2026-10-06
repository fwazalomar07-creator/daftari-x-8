import tempfile, unittest
from decimal import Decimal as D
from pathlib import Path

from daftari.core.models import Customer, HistoryEntry, InventoryItem, Invoice, InvoiceLine, Purchase, PurchaseLine
from daftari.core.reports import (customer_statement, is_invoice_paid, item_movement, loss_items, low_stock_items,
                                  supplier_statement, top_selling)
from daftari.core.security import hash_password, verify_password
from daftari.data.drafts import DraftStore
from daftari.data.repository import Repository
from daftari.data.store import MemoryStore
from daftari.ledger import InvoiceDraft, Ledger, LedgerError, PurchaseDraft


class ReportTests(unittest.TestCase):
    def test_loss_and_low_stock(self):
        items = [InventoryItem(id="1", name="a", cost=D(10), price=D(8), price_wholesale=D(12), price_distribution=D(9), stock=2),
                 InventoryItem(id="2", name="b", cost=D(5), price=D(8), price_wholesale=D(6), price_distribution=D(7), stock=50)]
        loss = loss_items(items)
        self.assertEqual([l.item.id for l in loss], ["1"])
        self.assertEqual(dict(loss[0].bad_tiers), {"توزيع": D(1), "مفرق": D(2)})
        self.assertEqual([i.id for i in low_stock_items(items)], ["1"])

    def test_statement_and_paid_flag(self):
        inv1 = Invoice(id="a", number="INV-1", date="2025-01-01T00:00:00Z", customer=" زبون ", total=D(100), paid=D(40), remaining=D(60))
        inv2 = Invoice(id="b", number="INV-2", date="2025-02-01T00:00:00Z", customer="زبون", total=D(50), paid=D(50), remaining=D(0))
        inv3 = Invoice(id="c", number="INV-3", date="2025-03-01T00:00:00Z", customer="آخر", total=D(9), paid=D(9))
        c = Customer(id="c1", name="زبون", balance=D(60), history=[
            HistoryEntry(date="2025-01-05", type="payment", amount=D(10)),
            HistoryEntry(date="2025-01-02", type="invoice_debt", amount=D(60))])
        st = customer_statement(c, [inv1, inv2, inv3])
        self.assertEqual((len(st.invoices), st.total_sales, st.total_paid, st.total_remaining), (2, D(150), D(90), D(60)))
        self.assertEqual([h.type for h in st.transactions], ["payment"])   # فواتير الدين لا تُعرض كحركات
        self.assertEqual(st.invoices[0].number, "INV-2")                  # الأحدث أولاً
        self.assertEqual([i.number for i in customer_statement(c, [inv1, inv2], limit=1).invoices], ["INV-2"])
        self.assertFalse(is_invoice_paid(inv1)); self.assertTrue(is_invoice_paid(inv2))
        inv1.payment_status_manual = "paid"                                # الحالة اليدوية تتقدم
        self.assertTrue(is_invoice_paid(inv1))

    def test_top_selling_and_movement(self):
        mk = lambda iid, q, custom=False: InvoiceLine(id=iid, name=iid, qty=q, is_custom=custom)
        invs = [Invoice(id="1", number="I1", date="2025-01-01", items=[mk("x", 3), mk("y", 1), mk("z", 99, True)]),
                Invoice(id="2", number="I2", date="2025-01-03", items=[mk("x", 2)])]
        top = top_selling(invs)
        self.assertEqual([(t.name, t.qty) for t in top], [("x", 5), ("y", 1)])  # اليدوي مستثنى
        pur = [Purchase(id="p", number="P1", date="2025-01-02", items=[PurchaseLine(id="x", qty=10)])]
        mv = item_movement(InventoryItem(id="x", name="x"), invs, pur)
        self.assertEqual([(m.kind, m.qty) for m in mv], [("بيع", -2), ("شراء", 10), ("بيع", -3)])

    def test_supplier_statement(self):
        from daftari.core.models import Supplier
        s = Supplier(id="s", name="مورد", balance=D(30), history=[HistoryEntry(date="2025-01-01", type="initial", amount=D(30))])
        pur = [Purchase(id="p", number="P1", date="2025-01-02", supplier="مورد", subtotal=D(70), paid=D(40), remaining=D(30))]
        st = supplier_statement(s, pur)
        self.assertEqual((st.total_purchases, st.total_paid, len(st.transactions)), (D(70), D(40), 1))


class SecurityTests(unittest.TestCase):
    def test_hash(self):
        h = hash_password("1234")
        self.assertTrue(verify_password("1234", h)); self.assertFalse(verify_password("12345", h))
        self.assertNotEqual(h, hash_password("1234"))      # ملح عشوائي
        self.assertFalse(verify_password("1234", "garbage"))


class DraftTests(unittest.TestCase):
    def test_roundtrip_and_clear(self):
        with tempfile.TemporaryDirectory() as t:
            ds = DraftStore(Path(t))
            d = InvoiceDraft(cart={"a": 2}, price_overrides={"a": D("9.5")}, customer="س", discount=D(3), paid=D(10),
                             custom_lines=[InvoiceLine(id="c", name="خدمة", price=D(5), qty=1, is_custom=True)])
            ds.save_invoice(d)
            r = ds.load_invoice()
            self.assertEqual((r.cart, r.price_overrides["a"], r.paid, r.custom_lines[0].name), ({"a": 2}, D("9.5"), D(10), "خدمة"))
            ds.save_invoice(InvoiceDraft())                    # فارغة -> تُحذف المسودة
            self.assertIsNone(ds.load_invoice())
            ds.save_purchase(PurchaseDraft(cart={"a": 1}, supplier="م"))
            self.assertEqual(ds.load_purchase().supplier, "م"); ds.clear_purchase(); self.assertIsNone(ds.load_purchase())


class AdminLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore(); self.led = Ledger(Repository(self.store)); await self.led.load()
        self.item = await self.led.add_item("فلتر", "F1", cost=6, price=10, stock=20)

    async def test_discard_editing_invoice_does_not_double_restore_stock(self):
        inv, _ = await self.led.finalize_invoice(InvoiceDraft(cart={self.item.id: 5}, customer="س", paid=D(10)))
        draft, _ = await self.led.begin_edit_invoice(inv.id)
        self.assertEqual(self.led.inventory[0].stock, 20)
        await self.led.discard_editing_invoice(draft)
        self.assertEqual((self.led.inventory[0].stock, self.led.customers[0].balance, len(self.led.invoices)), (20, D(0), 0))
        fresh = Ledger(Repository(self.store)); await fresh.load()
        self.assertEqual(fresh.invoices, [])                  # لا يعود من السحابة

    async def test_edit_customer_logs_signed_adjustment_and_survives_recompute(self):
        c = await self.led.add_customer("زبون", "123")
        await self.led.add_manual_customer_debt(c.id, 100)
        await self.led.edit_customer(c.id, "زبون 2", "999", 70)
        cust = self.led.customers[0]
        self.assertEqual((cust.name, cust.phone, cust.balance), ("زبون 2", "999", D(70)))
        self.assertEqual(cust.history[-1].type, "manual_adjustment"); self.assertEqual(cust.history[-1].amount, D(-30))
        from daftari.core.calc import CUSTOMER_SIGN, recompute_balance
        self.assertEqual(recompute_balance(cust.history, CUSTOMER_SIGN), D(70))
        with self.assertRaises(LedgerError): await self.led.edit_customer(c.id, "", "", 5)
        with self.assertRaises(LedgerError): await self.led.edit_supplier("nope", "x", -1)

    async def test_edit_item_and_pricing_validation(self):
        await self.led.edit_item(self.item.id, "فلتر جديد", "F9", 7)
        self.assertEqual((self.led.inventory[0].name, self.led.inventory[0].stock), ("فلتر جديد", 7))
        with self.assertRaises(LedgerError): await self.led.edit_item(self.item.id, "x", "", -1)
        await self.led.edit_item_pricing(self.item.id, "5", "9", "9.5", "11")
        it = self.led.inventory[0]
        self.assertEqual((it.cost, it.price_wholesale, it.price_distribution, it.price), (D(5), D(9), D("9.5"), D(11)))
        with self.assertRaises(LedgerError): await self.led.edit_item_pricing(self.item.id, "5", "", "1", "1")

    async def test_payment_status_manual(self):
        inv, _ = await self.led.finalize_invoice(InvoiceDraft(cart={self.item.id: 1}, customer="س", paid=D(2)))
        await self.led.set_invoice_payment_status(inv.id, "paid")
        self.assertEqual(self.led.invoices[0].payment_status_manual, "paid")
        fresh = Ledger(Repository(self.store)); await fresh.load()
        self.assertEqual(fresh.invoices[0].payment_status_manual, "paid")

    async def test_update_purchase_pricing(self):
        p = await self.led.finalize_purchase(PurchaseDraft(cart={self.item.id: 3}, supplier="م"))
        up, miss = await self.led.update_purchase_pricing(p.id, {self.item.id: ("12", None, "11")})
        it = self.led.inventory[0]
        self.assertEqual((up, miss, it.price, it.price_wholesale, it.price_distribution), (1, 0, D(12), D(12), D(11)))
        with self.assertRaises(LedgerError): await self.led.update_purchase_pricing(p.id, {self.item.id: ("", None, None)})

    async def test_cancel_edit_purchase_restores_state(self):
        p = await self.led.finalize_purchase(PurchaseDraft(cart={self.item.id: 10}, costs={self.item.id: D(7)}, supplier="م", paid=D(40)))
        self.assertEqual((self.led.inventory[0].stock, self.led.suppliers[0].balance), (30, D(30)))
        draft, _ = await self.led.begin_edit_purchase(p.id)
        self.assertEqual((self.led.inventory[0].stock, self.led.suppliers[0].balance), (20, D(0)))
        await self.led.cancel_edit_purchase(draft)
        self.assertEqual((self.led.inventory[0].stock, self.led.suppliers[0].balance), (30, D(30)))
        draft, _ = await self.led.begin_edit_purchase(p.id)
        await self.led.discard_editing_purchase(draft)
        self.assertEqual((self.led.inventory[0].stock, len(self.led.purchases)), (20, 0))

    async def test_password_flow_and_legacy_upgrade(self):
        self.assertFalse(self.led.has_password)
        self.led.settings["appPassword"] = "old"                       # كلمة مرور نصية قديمة من نسخة الويب
        self.assertTrue(self.led.check_password("old")); self.assertFalse(self.led.check_password("x"))
        await self.led.upgrade_legacy_password("old")
        self.assertNotIn("appPassword", self.led.settings)
        self.assertTrue(self.led.check_password("old"))
        await self.led.set_password("")                                 # إلغاء
        self.assertFalse(self.led.has_password)

    async def test_backup_roundtrip_replaces_everything(self):
        await self.led.finalize_invoice(InvoiceDraft(cart={self.item.id: 2}, customer="س", paid=D(5)))
        snap = self.led.export_backup()
        await self.led.add_item("صنف إضافي", "X", 1, 2, 3)
        await self.led.delete_invoice(self.led.invoices[0].id)
        counts = await self.led.import_backup(snap)
        self.assertEqual((counts["inventory"], counts["invoices"]), (1, 1))
        fresh = Ledger(Repository(self.store)); await fresh.load()
        self.assertEqual((len(fresh.inventory), len(fresh.invoices), fresh.inventory[0].stock), (1, 1, 18))
        with self.assertRaises(LedgerError): await self.led.import_backup({"x": 1})


if __name__ == "__main__":
    unittest.main()
