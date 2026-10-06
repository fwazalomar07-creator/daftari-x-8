"""التقارير وكشوف الحساب — دوال صافية (بدون واجهة) منقولة من renderXxx في النسخة الأصلية."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Sequence

from .calc import _parse_dt, invoice_cost
from .models import Customer, HistoryEntry, InventoryItem, Invoice, Purchase, Supplier, Voucher
from .money import ZERO

LOW_STOCK_THRESHOLD = 3


def same_name(a: str, b: str) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def is_invoice_paid(inv: Invoice) -> bool:
    """الحالة اليدوية (زر «تحديد») تتقدّم على الحساب التلقائي من المتبقي — كما في الأصل."""
    if inv.payment_status_manual:
        return inv.payment_status_manual == "paid"
    return not (inv.remaining > 0)


# ---- المخزون ------------------------------------------------------------------------------
def low_stock_items(items: Iterable[InventoryItem], threshold: int = LOW_STOCK_THRESHOLD) -> list[InventoryItem]:
    return [i for i in items if i.stock <= threshold]


@dataclass
class LossItem:
    item: InventoryItem
    cost: Decimal
    bad_tiers: list[tuple[str, Decimal]]  # (اسم الفئة، الخسارة للقطعة)


def loss_items(items: Iterable[InventoryItem]) -> list[LossItem]:
    """أصناف رأس مالها أكبر من سعر بيعها بأي فئة (جملة/توزيع/مفرق)."""
    out: list[LossItem] = []
    for it in items:
        retail = it.price
        wholesale = it.price_wholesale or it.price
        distribution = it.price_distribution or it.price
        bad = []
        if it.cost > wholesale:
            bad.append(("جملة", it.cost - wholesale))
        if it.cost > distribution:
            bad.append(("توزيع", it.cost - distribution))
        if it.cost > retail:
            bad.append(("مفرق", it.cost - retail))
        if bad:
            out.append(LossItem(it, it.cost, bad))
    return out


# ---- كشف حساب عميل ----------------------------------------------------------------------------
@dataclass
class CustomerStatement:
    customer: Customer
    invoices: list[Invoice]
    total_sales: Decimal
    total_paid: Decimal
    total_remaining: Decimal
    transactions: list[HistoryEntry]  # دفعات + ديون يدوية فقط (كما في الأصل)


def customer_statement(c: Customer, invoices: Sequence[Invoice], *, oldest_first: bool = False,
                       limit: int | None = None) -> CustomerStatement:
    mine = sorted((i for i in invoices if same_name(i.customer, c.name)), key=lambda i: _parse_dt(i.date),
                  reverse=not oldest_first)
    if limit:  # «آخر N فاتورة»: الأحدث دائماً
        mine = sorted(mine, key=lambda i: _parse_dt(i.date))[-limit:]
        if not oldest_first:
            mine.reverse()
    tx = sorted((h for h in c.history if h.type in ("payment", "manual_debt")), key=lambda h: _parse_dt(h.date),
                reverse=not oldest_first)
    return CustomerStatement(
        c, mine,
        sum((i.total for i in mine), ZERO), sum((i.paid for i in mine), ZERO),
        sum((i.remaining for i in mine), ZERO), tx,
    )


@dataclass
class SupplierStatement:
    supplier: Supplier
    purchases: list[Purchase]
    total_purchases: Decimal
    total_paid: Decimal
    transactions: list[HistoryEntry]


def supplier_statement(s: Supplier, purchases: Sequence[Purchase], *, oldest_first: bool = False) -> SupplierStatement:
    mine = sorted((p for p in purchases if same_name(p.supplier, s.name)), key=lambda p: _parse_dt(p.date),
                  reverse=not oldest_first)
    tx = sorted((h for h in s.history if h.type in ("payment", "purchase_debt", "initial")),
                key=lambda h: _parse_dt(h.date), reverse=not oldest_first)
    return SupplierStatement(s, mine, sum((p.subtotal for p in mine), ZERO), sum((p.paid for p in mine), ZERO), tx)


def invoice_count_for(customers_name: str, invoices: Iterable[Invoice]) -> int:
    return sum(1 for i in invoices if same_name(i.customer, customers_name))


def purchase_count_for(supplier_name: str, purchases: Iterable[Purchase]) -> int:
    return sum(1 for p in purchases if same_name(p.supplier, supplier_name))


# ---- التقارير -----------------------------------------------------------------------------------
@dataclass
class TopSold:
    name: str
    qty: int


def top_selling(invoices: Iterable[Invoice], n: int = 8) -> list[TopSold]:
    """أكثر المنتجات مبيعاً حسب الكمية. الأصناف اليدوية (isCustom) مستثناة."""
    agg: dict[str, TopSold] = {}
    for inv in invoices:
        for ln in inv.items:
            if ln.is_custom:
                continue
            row = agg.setdefault(ln.id, TopSold(ln.name, 0))
            row.qty += ln.qty
    return sorted(agg.values(), key=lambda t: t.qty, reverse=True)[:n]


@dataclass
class Movement:
    date: str
    kind: str   # بيع | شراء
    qty: int    # سالب للبيع
    ref: str


def item_movement(item: InventoryItem, invoices: Iterable[Invoice], purchases: Iterable[Purchase]) -> list[Movement]:
    out: list[Movement] = []
    for inv in invoices:
        for ln in inv.items:
            if ln.id == item.id and not ln.is_custom:
                out.append(Movement(inv.date, "بيع", -ln.qty, inv.number))
    for p in purchases:
        for ln in p.items:
            if ln.id == item.id:
                out.append(Movement(p.date, "شراء", ln.qty, p.number))
    out.sort(key=lambda m: _parse_dt(m.date), reverse=True)
    return out


def invoices_on_day(invoices: Iterable[Invoice], day_key: str) -> list[Invoice]:
    return [i for i in invoices if _parse_dt(i.date).strftime("%Y-%m-%d") == day_key]


def invoice_profit_value(inv: Invoice) -> Decimal:
    """ربح الفاتورة كما في سجل الفواتير: الإجمالي − تكلفة البضاعة."""
    return inv.total - invoice_cost(inv)


def total_expenses_by_category(expenses) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for e in expenses:
        out[e.category] += e.amount
    return dict(out)
