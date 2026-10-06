"""النماذج (Models) — متوافقة 100% مع شكل JSON المخزَّن حالياً في جدول app_data.

- المبالغ Decimal داخلياً، وتُخزَّن كأرقام JSON عادية (لا تتغير بياناتك القديمة).
- أي حقل غير معروف في السجل يُحفظ في `extra` ويُعاد كتابته كما هو، حتى لا نخسر
  بيانات أضافتها نسخة الويب القديمة أو نسخة لاحقة.
"""
from __future__ import annotations

import time
import secrets
from dataclasses import dataclass, field, fields
from decimal import Decimal
from typing import Any, ClassVar

from .money import ZERO, to_decimal, to_json_number


def uid() -> str:
    """معرّف فريد بنفس روح النسخة الأصلية (وقت + عشوائي)."""
    return _b36(int(time.time() * 1000)) + secrets.token_hex(3)[:5]


def _b36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = chars[r] + out
    return out or "0"


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---- مساعدات تعريف الحقول ---------------------------------------------------
def _f(kind: str, default: Any = None, key: str | None = None, item: type | None = None):
    meta = {"kind": kind, "key": key, "item": item}
    if kind == "list":
        return field(default_factory=list, metadata=meta)
    if kind == "money":
        return field(default=ZERO, metadata=meta)
    if kind == "int":
        return field(default=0, metadata=meta)
    if kind == "str":
        return field(default="", metadata=meta)
    if kind == "id":
        return field(default_factory=uid, metadata=meta)
    if kind == "bool":
        return field(default=False, metadata=meta)
    # opt_str / opt_money
    return field(default=None, metadata=meta)


@dataclass(kw_only=True)
class Record:
    extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]):
        kwargs: dict[str, Any] = {}
        known_keys: set[str] = set()
        for fl in fields(cls):
            if fl.name == "extra":
                continue
            key = fl.metadata.get("key") or fl.name
            known_keys.add(key)
            if key not in d or d[key] is None:
                continue
            raw, kind = d[key], fl.metadata["kind"]
            if kind in ("money", "opt_money"):
                kwargs[fl.name] = to_decimal(raw)
            elif kind == "int":
                kwargs[fl.name] = int(to_decimal(raw))
            elif kind in ("str", "id", "opt_str"):
                kwargs[fl.name] = str(raw)
            elif kind == "bool":
                kwargs[fl.name] = bool(raw)
            elif kind == "list":
                item = fl.metadata.get("item")
                kwargs[fl.name] = [item.from_dict(x) if item else x for x in raw]
        kwargs["extra"] = {k: v for k, v in d.items() if k not in known_keys}
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.extra)
        for fl in fields(self):
            if fl.name == "extra":
                continue
            key = fl.metadata.get("key") or fl.name
            val, kind = getattr(self, fl.name), fl.metadata["kind"]
            if kind in ("opt_str", "opt_money") and val is None:
                continue
            if kind in ("money", "opt_money"):
                out[key] = to_json_number(val)
            elif kind == "list":
                out[key] = [v.to_dict() if isinstance(v, Record) else v for v in val]
            else:
                out[key] = val
        return out


# ---- المخزون ----------------------------------------------------------------
@dataclass(kw_only=True)
class InventoryItem(Record):
    TABLE: ClassVar[str] = "inventory"
    id: str = _f("id")
    name: str = _f("str")
    code: str = _f("str")
    cost: Decimal = _f("money")
    price: Decimal = _f("money")  # مفرق
    price_wholesale: Decimal = _f("money", key="priceWholesale")
    price_distribution: Decimal = _f("money", key="priceDistribution")
    stock: int = _f("int")
    img: str | None = _f("opt_str")  # بصمة صورة الصنف (الصورة نفسها محفوظة منفصلة — انظر data/images.py)

    def price_for_tier(self, tier: str) -> Decimal:
        return {"wholesale": self.price_wholesale, "distribution": self.price_distribution}.get(tier, self.price)


# ---- فواتير البيع -------------------------------------------------------------
@dataclass(kw_only=True)
class InvoiceLine(Record):
    id: str = _f("id")
    name: str = _f("str")
    code: str = _f("str")
    price: Decimal = _f("money")
    cost: Decimal = _f("money")
    qty: int = _f("int")
    is_custom: bool = _f("bool", key="isCustom")

    @property
    def total(self) -> Decimal:
        return self.price * self.qty


@dataclass(kw_only=True)
class Invoice(Record):
    TABLE: ClassVar[str] = "invoices"
    id: str = _f("id")
    number: str = _f("str")
    date: str = _f("str")
    edited_at: str | None = _f("opt_str", key="editedAt")
    customer: str = _f("str")
    items: list[InvoiceLine] = _f("list", item=InvoiceLine)
    subtotal: Decimal = _f("money")
    discount: Decimal = _f("money")
    notes: str = _f("str")
    total: Decimal = _f("money")
    paid: Decimal = _f("money")
    remaining: Decimal = _f("money")
    customer_prior_debt: Decimal = _f("money", key="customerPriorDebt")
    customer_total_debt_after: Decimal = _f("money", key="customerTotalDebtAfter")
    payment_status_manual: str | None = _f("opt_str", key="paymentStatusManual")  # paid | unpaid

    @classmethod
    def from_dict(cls, d):
        # فواتير قديمة بلا حقل paid كانت تُعدّ مدفوعة بالكامل (inv.paid !== undefined ? inv.paid : inv.total)
        if "paid" not in d and "total" in d:
            d = {**d, "paid": d["total"], "remaining": d.get("remaining", 0)}
        return super().from_dict(d)


# ---- فواتير الشراء ------------------------------------------------------------
@dataclass(kw_only=True)
class PurchaseLine(Record):
    id: str = _f("id")
    name: str = _f("str")
    code: str = _f("str")
    qty: int = _f("int")
    cost: Decimal = _f("money")


@dataclass(kw_only=True)
class Purchase(Record):
    TABLE: ClassVar[str] = "purchases"
    id: str = _f("id")
    number: str = _f("str")
    supplier: str = _f("str")
    supplier_invoice_number: str = _f("str", key="supplierInvoiceNumber")
    date: str = _f("str")
    edited_at: str | None = _f("opt_str", key="editedAt")
    items: list[PurchaseLine] = _f("list", item=PurchaseLine)
    subtotal: Decimal = _f("money")
    paid: Decimal = _f("money")
    remaining: Decimal = _f("money")
    notes: str = _f("str")

    @classmethod
    def from_dict(cls, d):
        if "paid" not in d and "subtotal" in d:
            d = {**d, "paid": d["subtotal"], "remaining": d.get("remaining", 0)}
        return super().from_dict(d)


# ---- حسابات العملاء والموردين -----------------------------------------------------
@dataclass(kw_only=True)
class HistoryEntry(Record):
    date: str = _f("str")
    type: str = _f("str")  # initial | invoice_debt | manual_debt | purchase_debt | payment | manual_adjustment
    amount: Decimal = _f("money")
    invoice_number: str | None = _f("opt_str", key="invoiceNumber")
    purchase_number: str | None = _f("opt_str", key="purchaseNumber")
    voucher_number: str | None = _f("opt_str", key="voucherNumber")
    note: str | None = _f("opt_str")

    def signature(self) -> str:
        """نفس historyEntrySignature في JS — تُستخدم لدمج/حذف سطر واحد من كشف الحساب."""
        ref = self.invoice_number or self.purchase_number or self.voucher_number or self.note or ""
        return "|".join([self.date, self.type, to_json_num_str(self.amount), ref])


def to_json_num_str(d: Decimal) -> str:
    # JS يطبع 5 وليس 5.0، و 5.5 كما هي — نطابقها حتى تتطابق التواقيع مع بيانات الويب القديمة
    n = to_json_number(d)
    return str(n)


@dataclass(kw_only=True)
class Customer(Record):
    TABLE: ClassVar[str] = "customers"
    id: str = _f("id")
    name: str = _f("str")
    phone: str = _f("str")
    balance: Decimal = _f("money")
    history: list[HistoryEntry] = _f("list", item=HistoryEntry)


@dataclass(kw_only=True)
class Supplier(Record):
    TABLE: ClassVar[str] = "suppliers"
    id: str = _f("id")
    name: str = _f("str")
    type: str = _f("str")  # دائماً we_owe
    balance: Decimal = _f("money")
    history: list[HistoryEntry] = _f("list", item=HistoryEntry)


# ---- مصاريف وسندات -----------------------------------------------------------------
EXPENSE_CATEGORIES = {
    "rent": "إيجار المحل",
    "water": "مياه",
    "internet": "إنترنت",
    "fuel": "وقود",
    "other": "أخرى",
}


@dataclass(kw_only=True)
class Expense(Record):
    TABLE: ClassVar[str] = "expenses"
    id: str = _f("id")
    category: str = _f("str")
    amount: Decimal = _f("money")
    note: str = _f("str")
    period: str = _f("str")
    date: str = _f("str")


@dataclass(kw_only=True)
class Voucher(Record):
    TABLE: ClassVar[str] = "vouchers"
    id: str = _f("id")
    number: str = _f("str")
    type: str = _f("str")  # receipt | payment
    person: str = _f("str")
    amount: Decimal = _f("money")
    note: str = _f("str")
    ref_number: str | None = _f("opt_str", key="refNumber")
    previous_balance: Decimal | None = _f("opt_money", key="previousBalance")
    remaining_balance: Decimal | None = _f("opt_money", key="remainingBalance")
    date: str = _f("str")
    applied_to_id: str | None = _f("opt_str", key="appliedToId")


TABLES: dict[str, type[Record]] = {
    c.TABLE: c for c in (InventoryItem, Invoice, Purchase, Customer, Supplier, Expense, Voucher)
}
