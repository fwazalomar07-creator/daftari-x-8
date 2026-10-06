"""استرجاع الفاتورة غير المكتملة (مثل saveInvoiceDraftLocally / offerDraftRecovery).

المسودة محلية على الجهاز فقط ولا تُرفع للسحابة، وتُحفظ ذرّياً عند كل تغيير حتى لا تضيع
إن أغلق النظام التطبيق من الذاكرة.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from ..core.models import InvoiceLine, now_iso
from ..core.money import to_decimal, to_json_number
from ..ledger import InvoiceDraft, PurchaseDraft

INVOICE_FILE, PURCHASE_FILE = "draft_invoice.json", "draft_purchase.json"


def _num(v):  # Decimal -> JSON
    return None if v is None else to_json_number(v)


def invoice_has_content(d: InvoiceDraft) -> bool:
    return any(q > 0 for q in d.cart.values()) or bool(d.custom_lines)


def purchase_has_content(d: PurchaseDraft) -> bool:
    return any(q > 0 for q in d.cart.values()) or bool(getattr(d, "orphans", None))


class DraftStore:
    def __init__(self, folder: Path | None):
        self.folder = Path(folder) if folder else None
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=True)

    def _write(self, name: str, payload: dict | None) -> None:
        if not self.folder:
            return
        p = self.folder / name
        if payload is None:
            p.unlink(missing_ok=True)
            return
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def _read(self, name: str) -> dict | None:
        if not self.folder:
            return None
        try:
            return json.loads((self.folder / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ---- فاتورة بيع ----
    def save_invoice(self, d: InvoiceDraft) -> None:
        if not invoice_has_content(d):
            return self._write(INVOICE_FILE, None)
        self._write(INVOICE_FILE, {
            "cart": d.cart, "price_overrides": {k: _num(v) for k, v in d.price_overrides.items()},
            "custom_lines": [l.to_dict() for l in d.custom_lines], "customer": d.customer,
            "discount": _num(d.discount), "notes": d.notes, "paid": _num(d.paid), "custom_number": d.custom_number,
            "editing_id": d.editing_id, "editing_number": d.editing_number, "editing_date": d.editing_date,
            "price_tier": d.price_tier, "expiry": d.expiry, "saved_at": now_iso(),
        })

    def load_invoice(self) -> InvoiceDraft | None:
        r = self._read(INVOICE_FILE)
        if not r:
            return None
        d = InvoiceDraft(
            cart={k: int(v) for k, v in r.get("cart", {}).items()},
            price_overrides={k: to_decimal(v) for k, v in r.get("price_overrides", {}).items()},
            custom_lines=[InvoiceLine.from_dict(x) for x in r.get("custom_lines", [])],
            customer=r.get("customer", ""), discount=to_decimal(r.get("discount")), notes=r.get("notes", ""),
            paid=None if r.get("paid") is None else to_decimal(r["paid"]), custom_number=r.get("custom_number", ""),
            editing_id=r.get("editing_id"), editing_number=r.get("editing_number"), editing_date=r.get("editing_date"),
            price_tier=dict(r.get("price_tier") or {}), expiry=r.get("expiry"),
        )
        return d if invoice_has_content(d) else None

    def clear_invoice(self) -> None:
        self._write(INVOICE_FILE, None)

    # ---- فاتورة شراء ----
    def save_purchase(self, d: PurchaseDraft) -> None:
        if not purchase_has_content(d):
            return self._write(PURCHASE_FILE, None)
        self._write(PURCHASE_FILE, {
            "cart": d.cart, "costs": {k: _num(v) for k, v in d.costs.items()}, "supplier": d.supplier,
            "supplier_invoice_number": d.supplier_invoice_number, "paid": _num(d.paid), "notes": d.notes,
            "editing_id": d.editing_id, "editing_number": d.editing_number, "editing_date": d.editing_date,
            "orphans": [o.to_dict() for o in d.orphans],
            "saved_at": now_iso(),
        })

    def load_purchase(self) -> PurchaseDraft | None:
        r = self._read(PURCHASE_FILE)
        if not r:
            return None
        d = PurchaseDraft(
            cart={k: int(v) for k, v in r.get("cart", {}).items()},
            costs={k: to_decimal(v) for k, v in r.get("costs", {}).items()},
            supplier=r.get("supplier", ""), supplier_invoice_number=r.get("supplier_invoice_number", ""),
            paid=None if r.get("paid") is None else to_decimal(r["paid"]), notes=r.get("notes", ""),
            editing_id=r.get("editing_id"), editing_number=r.get("editing_number"), editing_date=r.get("editing_date"),
        )
        try:
            from ..core.models import PurchaseLine
            d.orphans = [PurchaseLine.from_dict(x) for x in r.get("orphans", []) if isinstance(x, dict)]
        except Exception:  # noqa: BLE001
            d.orphans = []
        return d if purchase_has_content(d) else None

    def clear_purchase(self) -> None:
        self._write(PURCHASE_FILE, None)
