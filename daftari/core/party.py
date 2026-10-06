"""ملخص شامل لكل عميل أو مورد: فواتيره، مبيعاته، سنداته، وأرباحه — كله يُحسب من البيانات ولا يُخزَّن.

العميل: الربح = Σ (إجمالي الفاتورة − تكلفة بضاعتها) لفواتيره بالاسم (نفس منطق سجل الفواتير).
المورد: لا يوجد رابط مباشر بين فاتورة البيع والمورد، لذا الربح «تقديري» = ربح مبيعات الأصناف التي اشتريناها منه.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from .calc import invoice_cost
from .models import Customer, Invoice, Purchase, Supplier, Voucher
from .money import ZERO
from .reports import same_name


def _dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.min


def _top(counter: dict[str, int], n: int = 5) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: -kv[1])[:n]


@dataclass
class CustomerSummary:
    customer: Customer
    invoices: list[Invoice]                 # الأحدث أولاً
    receipts: list[Voucher]                 # سندات القبض (الأحدث أولاً)
    manual_debts: list                      # سجلات «دين يدوي» من كشفه
    sales_total: Decimal = ZERO             # إجمالي المبيعات له
    paid_at_sale: Decimal = ZERO            # المدفوع عند البيع
    unpaid_on_invoices: Decimal = ZERO      # المتبقي على فواتيره
    receipts_total: Decimal = ZERO          # مجموع سندات القبض
    cost_total: Decimal = ZERO
    profit: Decimal = ZERO                  # الربح المحقق منه
    items_qty: int = 0
    top_items: list[tuple[str, int]] = field(default_factory=list)
    last_date: str = ""

    @property
    def count(self) -> int:
        return len(self.invoices)

    @property
    def avg_invoice(self) -> Decimal:
        return self.sales_total / self.count if self.count else ZERO

    @property
    def margin_pct(self) -> Decimal:
        return (self.profit / self.sales_total * 100) if self.sales_total else ZERO

    @property
    def collected_pct(self) -> Decimal:
        """نسبة ما قبضناه منه من إجمالي مبيعاته: (المدفوع عند البيع + سندات القبض) ÷ المبيعات."""
        if not self.sales_total:
            return ZERO
        got = self.paid_at_sale + self.receipts_total
        return max(ZERO, min(Decimal(100), got / self.sales_total * 100))


def customer_summary(c: Customer, invoices: Iterable[Invoice], vouchers: Iterable[Voucher]) -> CustomerSummary:
    mine = sorted((i for i in invoices if same_name(i.customer, c.name)), key=lambda i: _dt(i.date), reverse=True)
    rec = sorted((v for v in vouchers if v.type == "receipt" and same_name(v.person, c.name)),
                 key=lambda v: _dt(v.date), reverse=True)
    qty: dict[str, int] = defaultdict(int)
    for inv in mine:
        for l in inv.items:
            qty[l.name] += l.qty
    s = CustomerSummary(
        c, mine, rec, [h for h in c.history if h.type == "manual_debt"],
        sales_total=sum((i.total for i in mine), ZERO), paid_at_sale=sum((i.paid for i in mine), ZERO),
        unpaid_on_invoices=sum((i.remaining for i in mine), ZERO), receipts_total=sum((v.amount for v in rec), ZERO),
        cost_total=sum((invoice_cost(i) for i in mine), ZERO), items_qty=sum(qty.values()), top_items=_top(qty),
        last_date=mine[0].date if mine else "")
    s.profit = s.sales_total - s.cost_total
    return s


@dataclass
class SupplierSummary:
    supplier: Supplier
    purchases: list[Purchase]
    payments: list[Voucher]                 # سندات الدفع له
    purchases_total: Decimal = ZERO
    paid_at_purchase: Decimal = ZERO
    unpaid_on_purchases: Decimal = ZERO
    payments_total: Decimal = ZERO
    items_qty: int = 0
    top_items: list[tuple[str, int]] = field(default_factory=list)
    est_profit: Decimal = ZERO              # تقديري: ربح مبيعات أصنافه
    est_sales: Decimal = ZERO
    last_date: str = ""

    @property
    def count(self) -> int:
        return len(self.purchases)

    @property
    def avg_purchase(self) -> Decimal:
        return self.purchases_total / self.count if self.count else ZERO

    @property
    def paid_pct(self) -> Decimal:
        if not self.purchases_total:
            return ZERO
        return max(ZERO, min(Decimal(100), (self.paid_at_purchase + self.payments_total) / self.purchases_total * 100))


def supplier_summary(s: Supplier, purchases: Iterable[Purchase], vouchers: Iterable[Voucher],
                     invoices: Iterable[Invoice]) -> SupplierSummary:
    mine = sorted((p for p in purchases if same_name(p.supplier, s.name)), key=lambda p: _dt(p.date), reverse=True)
    pay = sorted((v for v in vouchers if v.type == "payment" and same_name(v.person, s.name)),
                 key=lambda v: _dt(v.date), reverse=True)
    qty: dict[str, int] = defaultdict(int)
    ids: set[str] = set()
    for p in mine:
        for l in p.items:
            qty[l.name] += l.qty
            if l.id:
                ids.add(l.id)
    sales = profit = ZERO
    for inv in invoices:
        for l in inv.items:
            if l.id in ids and not l.is_custom:
                sales += l.price * l.qty
                profit += (l.price - l.cost) * l.qty
    return SupplierSummary(
        s, mine, pay, purchases_total=sum((p.subtotal for p in mine), ZERO),
        paid_at_purchase=sum((p.paid for p in mine), ZERO), unpaid_on_purchases=sum((p.remaining for p in mine), ZERO),
        payments_total=sum((v.amount for v in pay), ZERO), items_qty=sum(qty.values()), top_items=_top(qty),
        est_profit=profit, est_sales=sales, last_date=mine[0].date if mine else "")
