import unittest
from decimal import Decimal as D

from daftari.core.calc import invoice_totals, next_number, profit_report, recompute_balance, CUSTOMER_SIGN
from daftari.core.models import Customer, HistoryEntry, InventoryItem, Invoice, InvoiceLine
from daftari.core.money import fmt_money, parse_num, q2, to_decimal
from daftari.core.search import RawRow, parse_table_rows, search_inventory, normalize_str
from daftari.data.repository import Repository
from daftari.data.store import MemoryStore
from daftari.ledger import InvoiceDraft, Ledger, LedgerError, PurchaseDraft


class MoneyTests(unittest.TestCase):
    def test_no_float_drift(self):
        self.assertEqual(to_decimal(0.1) + to_decimal(0.2), D("0.3"))

    def test_arabic_digits_and_formats(self):
        self.assertEqual(parse_num("١٢٣٫٥٠"), D("123.50"))
        self.assertEqual(parse_num("1,234.5 ل.س"), D("1234.5"))
        self.assertIsNone(parse_num("abc"))

    def test_rounding_half_up(self):
        self.assertEqual(q2("2.675"), D("2.68"))  # float العادي يعطي 2.67
        self.assertEqual(fmt_money("1234.50"), "$1,234.5")
        self.assertEqual(fmt_money("1234.50", symbol=False), "1,234.5")
        self.assertEqual(fmt_money("-12"), "-$12")


class CalcTests(unittest.TestCase):
    def test_invoice_totals_clamp(self):
        lines = [InvoiceLine(price=D("10"), qty=3, cost=D("6"))]
        t = invoice_totals(lines, discount=D("100"), paid=D("5"))
        self.assertEqual((t.subtotal, t.discount, t.total, t.paid, t.remaining), (D(30), D(30), D(0), D(0), D(0)))
        t = invoice_totals(lines, discount=D("5"), paid=D("10"))
        self.assertEqual((t.total, t.paid, t.remaining), (D(25), D(10), D(15)))
        t = invoice_totals(lines, paid=None)
        self.assertEqual(t.remaining, D(0))

    def test_next_number_never_repeats_after_delete(self):
        self.assertEqual(next_number("INV-", ["INV-1001", "INV-1002", "INV-1003"]), "INV-1004")
        self.assertEqual(next_number("INV-", ["INV-1001", "INV-1003"]), "INV-1004")  # الأصل يعطي 1003 مكرر
        self.assertEqual(next_number("INV-", []), "INV-1001")

    def test_recompute_balance(self):
        h = [HistoryEntry(date="2025-01-01", type="invoice_debt", amount=D(100)),
             HistoryEntry(date="2025-01-02", type="payment", amount=D(30)),
             HistoryEntry(date="2025-01-03", type="manual_debt", amount=D("5.5"))]
        self.assertEqual(recompute_balance(h, CUSTOMER_SIGN), D("75.5"))

    def test_profit_report(self):
        inv = Invoice(date="2025-03-10T10:00:00Z", discount=D(10),
                      items=[InvoiceLine(price=D(50), cost=D(30), qty=2)])
        rep = profit_report([inv], [])
        self.assertEqual((rep.total_revenue, rep.total_cost, rep.total_profit), (D(90), D(60), D(30)))
        self.assertIn("2025-03", rep.months)


class ModelRoundTrip(unittest.TestCase):
    def test_unknown_fields_preserved(self):
        raw = {"id": "a1", "name": "زيت", "code": "X1", "cost": 10.5, "price": 12, "priceWholesale": 11,
               "priceDistribution": 11.5, "stock": 4, "futureField": {"a": 1}}
        item = InventoryItem.from_dict(raw)
        self.assertEqual(item.cost, D("10.5"))
        self.assertEqual(item.to_dict(), raw)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.items = [InventoryItem(id="1", name="فلتر زيت تويوتا", code="TY-100"),
                      InventoryItem(id="2", name="فلتر هواء هيونداي", code="HY-200"),
                      InventoryItem(id="3", name="زيت محرك 5W30", code="OIL530")]

    def test_ranking_and_code(self):
        self.assertEqual(search_inventory(self.items, "ty-100")[0].id, "1")
        self.assertEqual(search_inventory(self.items, "فلتر زيت")[0].id, "1")

    def test_hamza_normalization(self):
        self.assertEqual(normalize_str("أحمد"), normalize_str("احمد"))

    def test_table_parse_with_header(self):
        rows = [["اسم الصنف", "كود", "الكمية", "السعر"], ["زيت", "A1", "٥", "١٢٫٥"]]
        out = parse_table_rows(rows)
        self.assertEqual((out[0].name, out[0].qty, out[0].cost), ("زيت", "٥", "١٢٫٥"))


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.repo = Repository(self.store)
        self.led = Ledger(self.repo)
        await self.led.load()
        self.item = await self.led.add_item("فلتر زيت", "F1", cost="6", price="10", stock=20)

    async def test_sale_with_debt_then_payment_then_delete(self):
        d = InvoiceDraft(cart={self.item.id: 5}, customer="أبو أحمد", paid=D("20"))
        inv, warn = await self.led.finalize_invoice(d)
        self.assertEqual((inv.total, inv.remaining, inv.number), (D(50), D(30), "INV-1001"))
        self.assertEqual(self.led.inventory[0].stock, 15)
        cust = self.led.customers[0]
        self.assertEqual(cust.balance, D(30))

        v = await self.led.record_customer_payment(cust.id, "12.5")
        self.assertEqual((v.number, self.led.customers[0].balance), ("RCV-1001", D("17.5")))

        await self.led.delete_voucher(v.id)
        self.assertEqual(self.led.customers[0].balance, D(30))

        await self.led.delete_invoice(inv.id)
        self.assertEqual(self.led.inventory[0].stock, 20)
        self.assertEqual(self.led.customers[0].balance, D(0))
        # بعد الحذف والمزامنة لا يعود السجل المحذوف من السحابة
        fresh = Ledger(Repository(self.store)); await fresh.load()
        self.assertEqual(fresh.invoices, [])

    async def test_duplicate_number_does_not_touch_stock(self):
        await self.led.finalize_invoice(InvoiceDraft(cart={self.item.id: 1}, custom_number="A-1"))
        stock = self.led.inventory[0].stock
        with self.assertRaises(LedgerError):
            await self.led.finalize_invoice(InvoiceDraft(cart={self.item.id: 3}, custom_number="a-1"))
        self.assertEqual(self.led.inventory[0].stock, stock)  # الأصل كان يخصم 3 ثم يتوقف

    async def test_oversell_warns_and_clamps(self):
        inv, warn = await self.led.finalize_invoice(InvoiceDraft(cart={self.item.id: 25}))
        self.assertTrue(warn)
        self.assertEqual(self.led.inventory[0].stock, 0)

    async def test_edit_and_cancel_keep_books_consistent(self):
        inv, _ = await self.led.finalize_invoice(InvoiceDraft(cart={self.item.id: 4}, customer="س", paid=D("10")))
        self.assertEqual((self.led.inventory[0].stock, self.led.customers[0].balance), (16, D(30)))
        draft, _ = await self.led.begin_edit_invoice(inv.id)
        self.assertEqual((self.led.inventory[0].stock, self.led.customers[0].balance), (20, D(0)))
        await self.led.cancel_edit_invoice(draft)
        self.assertEqual((self.led.inventory[0].stock, self.led.customers[0].balance), (16, D(30)))
        draft, _ = await self.led.begin_edit_invoice(inv.id)
        draft.cart[self.item.id] = 2
        new_inv, _ = await self.led.finalize_invoice(draft)
        self.assertEqual((new_inv.number, new_inv.total, new_inv.remaining), (inv.number, D(20), D(10)))
        self.assertEqual((self.led.inventory[0].stock, self.led.customers[0].balance), (18, D(10)))
        self.assertEqual(len(self.led.invoices), 1)

    async def test_purchase_updates_stock_cost_and_supplier_debt(self):
        d = PurchaseDraft(cart={self.item.id: 10}, costs={self.item.id: D("7")}, supplier="مورد 1", paid=D("40"))
        p = await self.led.finalize_purchase(d)
        self.assertEqual((p.subtotal, p.remaining), (D(70), D(30)))
        self.assertEqual((self.led.inventory[0].stock, self.led.inventory[0].cost), (30, D(7)))
        self.assertEqual(self.led.suppliers[0].balance, D(30))
        await self.led.delete_purchase(p.id)
        self.assertEqual((self.led.inventory[0].stock, self.led.suppliers[0].balance), (20, D(0)))

    async def test_purchase_requires_supplier(self):
        with self.assertRaises(LedgerError):
            await self.led.finalize_purchase(PurchaseDraft(cart={self.item.id: 1}))

    async def test_import_matches_by_code_and_queues_new(self):
        d = PurchaseDraft()
        res = self.led.apply_import(d, [RawRow("x", "f1", "3", "6.5"), RawRow("جديد", "N9", "2", "4")])
        self.assertEqual((len(res.matched), len(res.pending)), (1, 1))
        self.assertEqual(d.cart[self.item.id], 3)
        with self.assertRaises(LedgerError):  # بلا سعر مفرق
            await self.led.confirm_pending(d)
        d.pending[0].price = D(9)
        self.assertEqual(await self.led.confirm_pending(d), 1)
        self.assertEqual(len(self.led.inventory), 2)

    async def test_voucher_without_account_uses_manual_previous_balance(self):
        v = await self.led.add_voucher("receipt", "غريب", 40, previous_balance=100)
        self.assertEqual((v.remaining_balance, v.applied_to_id), (D(60), None))


class RepositoryMergeTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_devices_payments_both_survive(self):
        store = MemoryStore()
        a, b = Ledger(Repository(store)), Ledger(Repository(store))
        await a.load()
        cust = await a.add_customer("زبون")
        await a.add_manual_customer_debt(cust.id, 100)
        await b.load()
        # الجهازان يسجّلان دفعتين مختلفتين على نفس الحساب قبل أن يرى أحدهما الآخر
        await a.record_customer_payment(cust.id, 10)
        await b.record_customer_payment(cust.id, 25)
        fresh = Ledger(Repository(store)); await fresh.load()
        self.assertEqual(fresh.customers[0].balance, D(65))
        self.assertEqual(len([h for h in fresh.customers[0].history if h.type == "payment"]), 2)

    async def test_offline_then_sync_replays_deletes(self):
        store = MemoryStore()
        led = Ledger(Repository(store)); await led.load()
        it = await led.add_item("ص", "c", 1, 2, 5)
        store.fail = True
        await led.delete_item(it.id)       # بلا اتصال
        self.assertIn("inventory", led.repo.dirty)
        store.fail = False
        await led.repo.sync_dirty({"inventory": led.inventory})
        fresh = Ledger(Repository(store)); await fresh.load()
        self.assertEqual(fresh.inventory, [])


if __name__ == "__main__":
    unittest.main()
