"""تحويل كلمات OCR (مع إحداثياتها) إلى جدول أصناف فاتورة — مع فحص حسابي.

سر الدقة هنا ليس في OCR وحده بل في **التحقق**: لكل سطر نتحقق أن الكمية × السعر = المجموع.
إن اختلف نحاول التصحيح (غالباً خطأ في رقم واحد)، وإلا نعلّم السطر "يحتاج مراجعة" بدل
أن ندخل رقماً خاطئاً بصمت إلى المخزون. وفي نهاية الفاتورة نقارن مجموع السطور بالإجمالي المكتوب.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from ..core.money import ZERO, normalize_digits, q2
from ..core.search import RawRow, normalize_str
from .engine import Word

REVIEW_THRESHOLD = 0.75

_HEADER_KW = {
    "code": ["كود", "الكود", "رمز", "الرمز", "باركود", "الباركود", "code", "sku", "barcode", "ref"],
    "qty": ["كمية", "الكمية", "العدد", "عدد", "qty", "quantity", "qnty"],
    "total": ["مجموع", "المجموع", "الاجمالي", "اجمالي", "المبلغ", "amount", "total", "value"],
    "cost": ["سعر", "السعر", "الوحدة", "الافرادي", "افرادي", "price", "unit", "rate", "cost"],
    "name": ["اسم", "الصنف", "صنف", "البيان", "الوصف", "المادة", "المنتج", "item", "description", "product", "name", "details"],
}
_FOOTER_KW = ["المجموع الكلي", "الاجمالي الكلي", "اجمالي الفاتوره", "صافي", "الخصم", "خصم", "ضريبه", "subtotal",
              "grand", "discount", "tax", "vat", "balance", "المطلوب", "الاجمالي", "total"]
_TOTAL_LINE_KW = ["المجموعالكلي", "الاجماليالكلي", "اجماليالفاتوره", "الاجمالي", "المجموع", "grandtotal", "total", "صافي"]

_CONFUSIONS_MAP = {"O": "0", "o": "0", "D": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8", "Z": "2"}
_NUM_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")
_CODE_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9][A-Za-z0-9\-_/\.]{2,}$|^[A-Za-z]{1,4}[-_/]\d+$")
_ARABIC = re.compile(r"[\u0600-\u06FF]")


@dataclass
class ScannedRow:
    name: str = ""
    code: str = ""
    qty: Decimal | None = None
    cost: Decimal | None = None
    total: Decimal | None = None
    confidence: float = 1.0
    issues: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return self.confidence < REVIEW_THRESHOLD or bool(self.issues)

    def to_raw(self) -> RawRow:
        """يغذّي مسار الاستيراد الموجود (Ledger.apply_import) — نفس مراجعة الأصناف الجديدة."""
        f = lambda v: "" if v is None else str(v)  # noqa: E731
        return RawRow(self.name, self.code, f(self.qty), f(self.cost))


@dataclass
class ScannedInvoice:
    rows: list[ScannedRow] = field(default_factory=list)
    invoice_number: str = ""
    date: str = ""
    supplier: str = ""
    stated_total: Decimal | None = None
    computed_total: Decimal = ZERO
    warnings: list[str] = field(default_factory=list)
    raw_text: str = ""
    header_found: bool = False

    @property
    def mean_confidence(self) -> float:
        return sum(r.confidence for r in self.rows) / len(self.rows) if self.rows else 0.0


# ---- أرقام ------------------------------------------------------------------------------
def _fix_confusions(t: str) -> str:
    """O→0 وl→1 …: لا يُحوَّل الحرف إلا إذا سبقه رقم أو فاصلة عشرية (1O.5 → 10.5)،
    فالأكواد التي تبدأ بحرف (B100, OIL530) أو تحوي حروفاً أخرى (5W30) تبقى كما هي."""
    if not any(c.isdigit() for c in t):
        return t
    out, prev_numeric = [], False
    for ch in t:
        if ch in _CONFUSIONS_MAP and prev_numeric:
            out.append(_CONFUSIONS_MAP[ch])
            continue
        out.append(ch)
        prev_numeric = ch.isdigit() or (prev_numeric and ch in ".,")
    return "".join(out)


def numeric_value(token: str) -> Decimal | None:
    t = normalize_digits(token).strip().strip(":;")
    t = re.sub(r"(?i)(ل\.?س|ر\.?س|ج\.?م|د\.?ا|\$|usd|sar|egp|syp|sp|€|£)$", "", t).strip()
    if not t:
        return None
    t = _fix_confusions(t)
    t = t.replace("٬", ",")
    if _NUM_RE.match(t):
        try:
            return Decimal(t.replace(",", ""))
        except Exception:  # noqa: BLE001
            return None
    return None


def _is_code(token: str) -> bool:
    return bool(_CODE_RE.match(token)) and numeric_value(token) is None


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(Decimal("0.02"), abs(b) * Decimal("0.005"))


def _near_int(x: Decimal) -> int | None:
    r = x.quantize(Decimal(1))
    return int(r) if r >= 1 and abs(x - r) <= Decimal("0.02") else None


# ---- تجميع الكلمات في أسطر ------------------------------------------------------------------
def group_rows(words: list[Word]) -> list[list[Word]]:
    if not words:
        return []
    tol = 0.6 * statistics.median(w.h for w in words)
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: w.cy):
        if rows and abs(w.cy - statistics.fmean(x.cy for x in rows[-1])) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    return rows


def _row_text(row: list[Word]) -> str:
    rtl = sum(bool(_ARABIC.search(w.text)) for w in row) > len(row) / 2
    return " ".join(w.text for w in sorted(row, key=lambda w: w.cx, reverse=rtl))


# ---- الترويسة ------------------------------------------------------------------------------
def detect_header(rows: list[list[Word]]) -> tuple[int, dict[str, float]] | None:
    """أول سطر (من أول 12 سطراً) يحوي ≥ 2 أعمدة معروفة. يرجع (فهرسه، {عمود: موضع x})."""
    for idx, row in enumerate(rows[:12]):
        cols: dict[str, float] = {}
        for w in row:
            n = normalize_str(w.text)
            if not n:
                continue
            for field_name in ("code", "qty", "total", "cost", "name"):  # الترتيب مهم: total قبل cost
                if field_name in cols:
                    continue
                if any(normalize_str(k) == n or (len(normalize_str(k)) >= 4 and normalize_str(k) in n)
                       for k in _HEADER_KW[field_name]):
                    cols[field_name] = w.cx
                    break
        if len(cols) >= 3 or (len(cols) == 2 and "qty" in cols):
            return idx, cols
    return None


# ---- تسوية السطر (كمية × سعر = مجموع) ---------------------------------------------------------
def reconcile(qty: Decimal | None, cost: Decimal | None, total: Decimal | None):
    issues: list[str] = []
    if qty is not None and cost is not None and total is not None:
        if _close(qty * cost, total):
            return qty, cost, total, issues
        if cost > 0 and (n := _near_int(total / cost)) is not None:
            issues.append(f"صُحّحت الكمية {qty}→{n} لتطابق المجموع")
            return Decimal(n), cost, total, issues
        if qty > 0:
            new_cost = q2(total / qty)
            issues.append(f"صُحّح السعر {cost}→{new_cost} لتطابق المجموع — راجعه")
            return qty, new_cost, total, issues
        issues.append("المجموع لا يطابق الكمية × السعر")
    elif qty is not None and cost is not None:
        return qty, cost, q2(qty * cost), issues
    elif qty is not None and total is not None and qty > 0:
        return qty, q2(total / qty), total, ["السعر مستنتج من المجموع"]
    elif cost is not None and total is not None and cost > 0 and (n := _near_int(total / cost)) is not None:
        return Decimal(n), cost, total, ["الكمية مستنتجة من المجموع"]
    return qty, cost, total, issues


def _infer_without_header(nums: list[Decimal]):
    """بلا ترويسة: ابحث عن ثلاثية (كمية، سعر، مجموع) تحقق q*u=t من بين الأرقام."""
    n = len(nums)
    for i in range(n):
        for j in range(n):
            for k in range(n):
                if len({i, j, k}) < 3:
                    continue
                q, u, t = nums[i], nums[j], nums[k]
                if q >= 1 and q == q.to_integral_value() and u > 0 and _close(q * u, t):
                    return q, u, t, 0.0
    if n == 2:
        a, b = nums
        q, u = (a, b) if (a == a.to_integral_value() and a <= b) else (b, a)
        return q, u, None, 0.15
    if n == 1:
        return Decimal(1), nums[0], None, 0.3
    return None, None, None, 0.3


# ---- سطر بيانات ----------------------------------------------------------------------------
def _assign_columns(nums: list[tuple[Word, Decimal]], cols: dict[str, float], max_dist: float):
    avail = [c for c in ("qty", "cost", "total") if c in cols]
    pairs = sorted(((abs(w.cx - cols[c]), i, c) for i, (w, _) in enumerate(nums) for c in avail))
    used_tok, used_col, out = set(), set(), {}
    for dist, i, c in pairs:
        if i in used_tok or c in used_col or dist > max_dist:
            continue
        used_tok.add(i)
        used_col.add(c)
        out[c] = nums[i][1]
    return out, used_tok


def parse_row(row: list[Word], cols: dict[str, float] | None, page_w: float) -> ScannedRow | None:
    nums: list[tuple[Word, Decimal]] = []
    texts: list[Word] = []
    code_cands: list[Word] = []
    for w in sorted(row, key=lambda w: w.cx):
        v = numeric_value(w.text)
        if v is not None:
            nums.append((w, v))
        elif _is_code(w.text):
            code_cands.append(w)
        else:
            texts.append(w)
    # أكثر من مرشح للكود (مثل "5W30" داخل الاسم و"OIL530" في عمود الكود): الأقرب لعمود الكود يفوز،
    # وبلا ترويسة نأخذ الأخير (الكود عادةً في عمود مستقل بعد الاسم)، والباقي يعود جزءاً من الاسم.
    code_w: Word | None = None
    if code_cands:
        if cols and "code" in cols:
            code_w = min(code_cands, key=lambda w: abs(w.cx - cols["code"]))
        else:
            code_w = code_cands[-1]
        texts += [w for w in code_cands if w is not code_w]

    # رقم تسلسلي (1،2،3…) في أقصى العمود: لا يقع قرب أي عمود رقمي معروف
    qty = cost = total = None
    conf_penalty = 0.0
    if cols and any(c in cols for c in ("qty", "cost", "total")):
        assigned, used = _assign_columns(nums, cols, max_dist=0.18 * page_w)
        qty, cost, total = assigned.get("qty"), assigned.get("cost"), assigned.get("total")
        # رقم صحيح وحيد قرب عمود "code" (كود رقمي بحت)
        if "code" in cols and code_w is None:
            left = [(abs(w.cx - cols["code"]), i) for i, (w, _) in enumerate(nums) if i not in used]
            if left:
                d, i = min(left)
                if d <= 0.08 * page_w:
                    code_w = nums[i][0]
    else:
        vals = [v for _, v in nums]
        if len(vals) >= 2 or (vals and texts):
            qty, cost, total, conf_penalty = _infer_without_header(vals[-3:] if len(vals) > 3 else vals)
        else:
            return None

    if not texts and code_w is None and qty is None and cost is None:
        return None

    rtl = any(_ARABIC.search(w.text) for w in texts)
    name = " ".join(w.text for w in sorted(texts, key=lambda w: w.cx, reverse=rtl)).strip(" -:|.")
    code = code_w.text if code_w else ""

    issues: list[str] = []
    qty, cost, total, rec_issues = reconcile(qty, cost, total)
    issues += rec_issues
    if qty is None or qty <= 0:
        issues.append("الكمية غير مقروءة")
    if cost is None:
        issues.append("السعر غير مقروء")
    if not name:
        issues.append("الاسم غير مقروء")
    if qty is not None and qty != qty.to_integral_value():
        issues.append("الكمية غير صحيحة (كسر)")

    mean_conf = statistics.fmean(w.conf for w in row) / 100
    conf = mean_conf - conf_penalty
    if any("لا يطابق" in s or "راجعه" in s for s in issues):
        conf -= 0.25
    conf -= 0.2 * sum(("غير مقروء" in s) for s in issues)
    return ScannedRow(name, code, qty, cost, total, max(0.0, min(1.0, conf)), issues)


# ---- الفاتورة كاملة --------------------------------------------------------------------------
_INV_NO = re.compile(r"(?:رقم\s*الفاتور[ةه]|فاتور[ةه]\s*رقم|invoice\s*(?:no\.?|number|#)?|inv\s*#?|no\.?)\s*[:#\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,20})", re.I)
_DATE = re.compile(r"\b(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\b")
_SUPPLIER = re.compile(r"(?:المورد|السادة|مؤسسة|شركة|supplier|from|vendor)\s*[:\-]?\s*(.+)", re.I)


def parse_invoice(words: list[Word], page_width: float | None = None) -> ScannedInvoice:
    inv = ScannedInvoice()
    if not words:
        inv.warnings.append("لم يُعثر على أي نص في الصورة")
        return inv
    page_w = page_width or max(w.x + w.w for w in words)
    rows = group_rows(words)
    texts = [normalize_digits(_row_text(r)) for r in rows]
    inv.raw_text = "\n".join(texts)

    for t in texts[:15]:
        if not inv.invoice_number and (m := _INV_NO.search(t)):
            inv.invoice_number = m.group(1)
        if not inv.date and (m := _DATE.search(t)):
            inv.date = m.group(1)
        if not inv.supplier and (m := _SUPPLIER.search(t)):
            inv.supplier = m.group(1).strip()[:60]

    hdr = detect_header(rows)
    inv.header_found = hdr is not None
    start = hdr[0] + 1 if hdr else 0
    cols = hdr[1] if hdr else None
    if not hdr:
        inv.warnings.append("لم تُكتشف ترويسة الجدول — الأعمدة مستنتجة حسابياً، راجع كل سطر")

    for idx in range(start, len(rows)):
        norm = normalize_str(texts[idx])
        if any(normalize_str(k) in norm for k in _FOOTER_KW) and len(rows[idx]) <= 6:
            nums = [v for w in rows[idx] if (v := numeric_value(w.text)) is not None]
            if nums and any(normalize_str(k) in norm for k in _TOTAL_LINE_KW):
                inv.stated_total = max(nums)
            if hdr:  # بعد الترويسة أي سطر تذييل ينهي الجدول
                break
            continue
        parsed = parse_row(rows[idx], cols, page_w)
        if parsed and (parsed.name or parsed.code):
            inv.rows.append(parsed)

    inv.computed_total = sum((r.total if r.total is not None else (r.qty or 0) * (r.cost or 0) for r in inv.rows), ZERO)
    if inv.stated_total is not None and inv.rows and not _close(inv.computed_total, inv.stated_total):
        inv.warnings.append(
            f"مجموع السطور ({inv.computed_total}) لا يطابق إجمالي الفاتورة المكتوب ({inv.stated_total}) "
            "— غالباً سطر ناقص أو رقم مقروء خطأ")
    if not inv.rows:
        inv.warnings.append("لم تُستخرج أصناف — جرّب صورة أوضح أو أدخل الأصناف يدوياً")
    return inv
