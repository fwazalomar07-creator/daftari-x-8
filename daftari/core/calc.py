"""دوال الحساب الصافية (بدون قاعدة بيانات ولا واجهة) — سهلة الاختبار.

مطابقة لمنطق النسخة الأصلية مع فروق مقصودة موثّقة في التعليقات.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from .models import (
    Customer, Expense, HistoryEntry, Invoice, InvoiceLine, InventoryItem,
    PurchaseLine, Supplier,
)
from .money import ZERO, clamp, q2, to_decimal

# كيف يؤثر كل نوع حركة على الرصيد (مطابق لـ CUSTOMER_/SUPPLIER_HISTORY_SIGN)
CUSTOMER_SIGN = {"initial": 1, "invoice_debt": 1, "manual_debt": 1, "payment": -1}
SUPPLIER_SIGN = {"initial": 1, "purchase_debt": 1, "payment": -1}


@dataclass(frozen=True)
class Totals:
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    paid: Decimal
    remaining: Decimal


def _settle(subtotal: Decimal, discount: Decimal, paid: Decimal | None) -> Totals:
    subtotal = q2(subtotal)
    discount = clamp(q2(discount), ZERO, subtotal)
    total = subtotal - discount
    # paid=None يعني "مدفوعة بالكامل" (نفس سلوك paidAmount === null)
    paid_v = total if paid is None else clamp(q2(paid), ZERO, total)
    return Totals(subtotal, discount, total, paid_v, total - paid_v)


def invoice_totals(lines: Iterable[InvoiceLine], discount: Decimal = ZERO, paid: Decimal | None = None) -> Totals:
    return _settle(sum((l.price * l.qty for l in lines), ZERO), discount, paid)


def purchase_totals(lines: Iterable[PurchaseLine], paid: Decimal | None = None) -> Totals:
    return _settle(sum((l.cost * l.qty for l in lines), ZERO), ZERO, paid)


def invoice_revenue(inv: Invoice) -> Decimal:
    """الإيراد = مجموع البنود − الخصم (كما في تبويب الأرباح)."""
    return sum((l.price * l.qty for l in inv.items), ZERO) - inv.discount


def invoice_cost(inv: Invoice) -> Decimal:
    return sum((l.cost * l.qty for l in inv.items), ZERO)


def invoice_profit(inv: Invoice) -> Decimal:
    return invoice_revenue(inv) - invoice_cost(inv)


def total_capital(items: Iterable[InventoryItem]) -> Decimal:
    """رأس المال المجمَّد في المخزون = Σ تكلفة × كمية."""
    return sum((i.cost * i.stock for i in items), ZERO)


# ---- كشف الحساب -----------------------------------------------------------------
def recompute_balance(history: Iterable[HistoryEntry], sign_map: dict[str, int]) -> Decimal:
    """يعيد بناء الرصيد من سجل الحركات.

    manual_adjustment مخزَّن كفرق موقَّع جاهز فيُجمع كما هو (مثل mergeAccountRecord).
    الأنواع المجهولة لا تؤثر (لا نخمّن).
    """
    hist = list(history)
    adj = sum((h.amount for h in hist if h.type == "manual_adjustment"), ZERO)
    body = sum((sign_map[h.type] * h.amount for h in hist if h.type in sign_map), ZERO)
    return max(ZERO, q2(adj + body))


def merge_history(cloud: Iterable[HistoryEntry], local: Iterable[HistoryEntry], deleted_signatures: Iterable[str]) -> list[HistoryEntry]:
    by_sig: dict[str, HistoryEntry] = {}
    for h in cloud:
        by_sig[h.signature()] = h
    for h in local:
        by_sig[h.signature()] = h
    for sig in deleted_signatures:
        by_sig.pop(sig, None)
    return sorted(by_sig.values(), key=lambda h: _parse_dt(h.date))


def merge_account(cloud, local, sign_map, deleted_signatures):
    """دمج نسختين من نفس حساب عميل/مورد (جهازان عدّلاه معاً) دون ضياع أي دفعة."""
    if cloud is None:
        return local
    if local is None:
        return cloud
    history = merge_history(cloud.history, local.history, deleted_signatures)
    merged = type(local).from_dict({**cloud.to_dict(), **local.to_dict()})
    merged.history = history
    merged.balance = recompute_balance(history, sign_map)
    return merged


# ---- الأرباح ----------------------------------------------------------------------
@dataclass
class Bucket:
    label: str
    revenue: Decimal = ZERO
    cost: Decimal = ZERO
    profit: Decimal = ZERO
    expenses: Decimal = ZERO
    count: int = 0

    @property
    def net_profit(self) -> Decimal:
        return self.profit - self.expenses


@dataclass
class ProfitReport:
    months: dict[str, Bucket]
    days: dict[str, Bucket]
    total_revenue: Decimal
    total_cost: Decimal
    total_profit: Decimal
    total_expenses: Decimal
    today_revenue: Decimal
    today_profit: Decimal

    @property
    def total_net_profit(self) -> Decimal:
        return self.total_profit - self.total_expenses


def _parse_dt(s: str) -> datetime:
    if not s:
        return datetime.min
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min
    # كـ JS: التاريخ المخزَّن UTC لكن اليوم/الشهر يُحسبان بتوقيت الجهاز المحلي
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def profit_report(invoices: Iterable[Invoice], expenses: Iterable[Expense], today: datetime | None = None) -> ProfitReport:
    months: dict[str, Bucket] = defaultdict(lambda: Bucket(""))
    days: dict[str, Bucket] = defaultdict(lambda: Bucket(""))
    tr = tc = tp = te = ZERO
    today_key = (today or datetime.now()).strftime("%Y-%m-%d")
    t_rev = t_prof = ZERO

    for inv in invoices:
        d = _parse_dt(inv.date)
        mk, dk = d.strftime("%Y-%m"), d.strftime("%Y-%m-%d")
        rev, cost = invoice_revenue(inv), invoice_cost(inv)
        prof = rev - cost
        for b, key in ((months[mk], mk), (days[dk], dk)):
            b.label = key
            b.revenue += rev
            b.cost += cost
            b.profit += prof
        days[dk].count += 1
        tr, tc, tp = tr + rev, tc + cost, tp + prof
        if dk == today_key:
            t_rev += rev
            t_prof += prof

    for ex in expenses:
        mk = _parse_dt(ex.date).strftime("%Y-%m")
        months[mk].label = mk
        months[mk].expenses += ex.amount
        te += ex.amount

    return ProfitReport(dict(months), dict(days), tr, tc, tp, te, t_rev, t_prof)


# ---- مساعدات ترقيم ------------------------------------------------------------------
def next_number(prefix: str, existing: Iterable[str], start: int = 1001) -> str:
    """الرقم التالي = أعلى رقم موجود + 1.

    فرق مقصود عن الأصل: الأصل يستخدم (عدد الفواتير + 1001) فيُنتج رقماً مكرراً
    بعد حذف فاتورة. هنا لا يتكرر الرقم أبداً.
    """
    best = start - 1
    for n in existing:
        if n and n.upper().startswith(prefix.upper()):
            tail = n[len(prefix):]
            if tail.isdigit():
                best = max(best, int(tail))
    return f"{prefix}{best + 1}"
