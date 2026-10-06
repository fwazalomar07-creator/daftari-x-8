"""البحث في المخزون (عربي/إنجليزي) + تحليل جداول الاستيراد (Excel/CSV/OCR)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from .models import InventoryItem
from .money import parse_num, to_decimal
from decimal import Decimal

_STRIP = re.compile(r"[^a-z0-9\u0600-\u06FF]")
_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u0640]")  # تشكيل + تطويل


def normalize_str(s: Any) -> str:
    """أحرف صغيرة، بدون رموز/تشكيل، وتوحيد الألف والياء والتاء المربوطة."""
    t = _DIACRITICS.sub("", str(s or "").lower())
    t = t.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
    return _STRIP.sub("", t)


def loose_overlap(haystack: str, needle: str) -> float:
    if not needle:
        return 1.0
    pool = list(haystack)
    matched = 0
    for ch in needle:
        if ch in pool:
            pool.remove(ch)
            matched += 1
    return matched / len(needle)


def direct_score(h: str, n: str) -> int:
    if not n:
        return 0
    if not h:
        return -1
    if h == n:
        return 1000
    if h.startswith(n):
        return 800 - min(200, len(h) - len(n))
    idx = h.find(n)
    if idx != -1:
        return 500 - idx * 2 - min(150, len(h) - len(n))
    return -1


def item_score(item: InventoryItem, query: str, allow_loose: bool = True) -> float:
    name_h, code_h = normalize_str(item.name), normalize_str(item.code)
    words = [w for w in (normalize_str(w) for w in query.split()) if w]
    if not words:
        return 0
    if len(words) == 1:
        n = words[0]
        best = max(direct_score(name_h, n), direct_score(code_h, n))
        if best > -1:
            return best
        if allow_loose and len(n) >= 4:
            ratio = max(loose_overlap(name_h, n), loose_overlap(code_h, n))
            if ratio >= 0.85:
                return round(ratio * 50)
        return -1
    total = 0
    for w in words:
        s = max(direct_score(name_h, w), direct_score(code_h, w))
        if s == -1:
            return -1
        total += s
    return total / len(words)


def search_inventory(items: Sequence[InventoryItem], query: str) -> list[InventoryItem]:
    """أفضل تطابق أولاً. الاحتياط المكلف (أخطاء الإملاء) لا يعمل إلا إن قلّت النتائج المباشرة."""
    if not query.strip():
        return list(items)
    scored, seen = [], set()
    for it in items:
        s = item_score(it, query, False)
        if s > -1:
            scored.append((s, it))
            seen.add(it.id)
    if len(scored) < 6:
        for it in items:
            if it.id in seen:
                continue
            s = item_score(it, query, True)
            if s > -1:
                scored.append((s, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in scored]


# ---- استيراد جداول ------------------------------------------------------------------
_KEYWORDS = {
    "name": ["اسم", "صنف", "المنتج", "الوصف", "بيان", "item", "name", "product", "description"],
    "code": ["كود", "رمز", "باركود", "code", "sku", "barcode"],
    "qty": ["كمية", "عدد", "qty", "quantity", "count"],
    "cost": ["سعر", "تكلفة", "راس المال", "رأس المال", "price", "cost"],
}


def detect_table_columns(rows: Sequence[Sequence[Any]]):
    for r in range(min(3, len(rows))):
        mapping: dict[str, int] = {}
        for idx, cell in enumerate(rows[r] or []):
            norm = normalize_str(cell)
            if not norm:
                continue
            for field_name, kws in _KEYWORDS.items():
                if field_name in mapping:
                    continue
                if any(normalize_str(k) in norm for k in kws):
                    mapping[field_name] = idx
                    break
        if len(mapping) >= 2:
            return mapping, r
    return None


@dataclass
class RawRow:
    name: str
    code: str
    qty: str
    cost: str
    price: str = ""
    price_wholesale: str = ""
    price_distribution: str = ""


def parse_table_rows(rows: Sequence[Sequence[Any]]) -> list[RawRow]:
    rows = [r for r in (rows or []) if r and any(str(v).strip() for v in r if v is not None)]
    if not rows:
        return []
    det = detect_table_columns(rows)
    m = det[0] if det else {}
    c_name, c_code = m.get("name", 0), m.get("code", 1)
    c_qty, c_cost = m.get("qty", 2), m.get("cost", 3)
    start = det[1] + 1 if det else 0

    def cell(row, i):
        return "" if i >= len(row) or row[i] is None else str(row[i]).strip()

    out: list[RawRow] = []
    for r in range(start, len(rows)):
        row = rows[r]
        name, code, qty, cost = cell(row, c_name), cell(row, c_code), cell(row, c_qty), cell(row, c_cost)
        if not (name or code or qty or cost):
            continue
        if not det and r == 0 and parse_num(qty) is None and parse_num(cost) is None and name:
            continue  # أول سطر عنوان بلا ترويسة معروفة
        out.append(RawRow(name, code, qty, cost))
    return out


@dataclass
class PendingItem:
    name: str
    code: str
    qty: int
    cost: Decimal
    price: Decimal | None = None
    price_wholesale: Decimal | None = None
    price_distribution: Decimal | None = None


@dataclass
class ImportResult:
    matched: list[tuple[InventoryItem, int, Decimal]]  # (الصنف الموجود، الكمية، التكلفة)
    pending: list[PendingItem]  # أصناف جديدة بانتظار التسعير
    skipped: int = 0
    fixed_up: int = 0
    needs_review: int = 0


def match_import_rows(rows: Sequence[RawRow], inventory: Sequence[InventoryItem]) -> ImportResult:
    """نفس processPurchaseImportData: المطابقة بالكود أولاً (الأدق) ثم بالاسم إن لم يوجد كود."""
    res = ImportResult([], [])
    for raw in rows:
        name, code = raw.name.strip(), raw.code.strip()
        q_raw, c_raw = parse_num(raw.qty), parse_num(raw.cost)
        qty = int(q_raw) if q_raw is not None else 0
        q_missing = qty <= 0
        c_missing = c_raw is None or c_raw < 0
        if not name and not code and q_missing and c_missing:
            res.skipped += 1
            continue
        if q_missing:
            qty = 1
            res.fixed_up += 1
        cost = Decimal(0) if c_missing else c_raw
        if c_missing:
            res.fixed_up += 1
        nc, nn = normalize_str(code), normalize_str(name)
        existing = None
        if nc:
            existing = next((i for i in inventory if i.code and normalize_str(i.code) == nc), None)
        elif nn:
            existing = next((i for i in inventory if normalize_str(i.name) == nn), None)
        if existing:
            res.matched.append((existing, qty, cost))
        else:
            def opt(v: str):
                n = parse_num(v)
                return n if n is not None and n >= 0 else None
            res.pending.append(PendingItem(name, code, qty, cost, opt(raw.price), opt(raw.price_wholesale),
                                           opt(raw.price_distribution)))
            if not name or not code:
                res.needs_review += 1
    return res


def fuzzy_match(haystack: str, needle: str) -> bool:
    """مطابقة تساهلية للنصوص القصيرة (رقم فاتورة، اسم زبون/مورد) — نفس fuzzyMatch الأصلية.

    عدة كلمات: كل كلمة يجب أن تظهر في النص بأي ترتيب. كلمة واحدة: تطابق مباشر،
    وإلا تطابق الحروف المبعثرة (للأخطاء الإملائية) بشرط طول ≥ 4 وتداخل عالٍ.
    """
    words = [x for x in str(needle or "").split() if x]
    if not words:
        return True
    h = normalize_str(haystack)
    if len(words) > 1:
        return all(normalize_str(x) in h for x in words)
    n = normalize_str(words[0])
    if not n or n in h:
        return True
    return len(n) >= 4 and loose_overlap(h, n) >= 0.85
