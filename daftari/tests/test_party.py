"""ملخص العميل/المورد: الفواتير، المبيعات، السندات، والأرباح بدقة."""
import unittest
from decimal import Decimal as D

from daftari.core.models import Customer, Invoice, InvoiceLine, Purchase, PurchaseLine, Supplier, Voucher
from daftari.core.party import customer_summary, supplier_summary


def inv(num, cust, lines, total, paid, date="2026-10-01T10:00:00"):
    return Invoice(number=num, customer=cust, date=date, items=lines, total=D(total), paid=D(paid),
                   remaining=D(total) - D(paid))


class PartyTests(unittest.TestCase):
    def test_customer_totals_profit_and_receipts(self):
        c = Customer(name="أبو أحمد", balance=D(30))
        i1 = inv("1", "أبو أحمد", [InvoiceLine(id="a", name="زيت", price=D(10), cost=D(6), qty=5)], 50, 20)
        i2 = inv("2", " أبو أحمد ", [InvoiceLine(id="b", name="فلتر", price=D(8), cost=D(5), qty=2)], 16, 16,
                 "2026-10-02T10:00:00")
        other = inv("3", "غيره", [InvoiceLine(id="a", name="زيت", price=D(10), cost=D(6), qty=9)], 90, 90)
        v1 = Voucher(number="R1", type="receipt", person="أبو أحمد", amount=D(10), date="2026-10-03T09:00:00")
        v2 = Voucher(number="P1", type="payment", person="أبو أحمد", amount=D(99))      # سند دفع لا يُحتسب
        s = customer_summary(c, [i1, i2, other], [v1, v2])
        self.assertEqual(s.count, 2)
        self.assertEqual(s.invoices[0].number, "2")                     # الأحدث أولاً
        self.assertEqual(s.sales_total, D(66))
        self.assertEqual(s.profit, D(66) - D(30) - D(10))              # 66 − (5×6 + 2×5)
        self.assertEqual(s.receipts_total, D(10))
        self.assertEqual(s.paid_at_sale, D(36))
        self.assertEqual(s.top_items[0], ("زيت", 5))
        self.assertAlmostEqual(float(s.collected_pct), (36 + 10) / 66 * 100, places=3)

    def test_customer_empty(self):
        s = customer_summary(Customer(name="جديد"), [], [])
        self.assertEqual((s.count, s.sales_total, s.profit, s.collected_pct, s.margin_pct), (0, 0, 0, 0, 0))

    def test_supplier_summary_and_estimated_profit(self):
        sup = Supplier(name="مورد 1", type="we_owe", balance=D(40))
        p1 = Purchase(number="P-1", supplier="مورد 1", date="2026-09-30T10:00:00", subtotal=D(100), paid=D(60),
                      remaining=D(40), items=[PurchaseLine(id="a", name="زيت", qty=10, cost=D(6))])
        p2 = Purchase(number="P-2", supplier="مورد آخر", subtotal=D(500), items=[PurchaseLine(id="z", name="x", qty=1)])
        pay = Voucher(number="P1", type="payment", person="مورد 1", amount=D(25))
        sold = inv("1", "x", [InvoiceLine(id="a", name="زيت", price=D(10), cost=D(6), qty=5),
                              InvoiceLine(id="q", name="غير", price=D(7), cost=D(1), qty=3)], 71, 71)
        s = supplier_summary(sup, [p1, p2], [pay], [sold])
        self.assertEqual(s.count, 1)
        self.assertEqual(s.purchases_total, D(100))
        self.assertEqual(s.payments_total, D(25))
        self.assertEqual(s.est_profit, D(20))                 # (10−6)×5 فقط لصنف اشتريناه منه
        self.assertEqual(s.est_sales, D(50))


if __name__ == "__main__":
    unittest.main()
