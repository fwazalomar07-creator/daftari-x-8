"""دقة مالية: كل المبالغ Decimal بدل float.

في النسخة الأصلية (JavaScript) كل الحسابات بـ float، فمثلاً 0.1 + 0.2 = 0.30000000000000004
وتتراكم الأخطاء في الأرصدة. هنا نستخدم Decimal ونقرّب إلى منزلتين بطريقة
ROUND_HALF_UP (التقريب المحاسبي المعتاد).
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

CENT = Decimal("0.01")
ZERO = Decimal("0")

_ARABIC_INDIC = "٠١٢٣٤٥٦٧٨٩"
_EASTERN_ARABIC = "۰۱۲۳۴۵۶۷۸۹"
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate(_ARABIC_INDIC)}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate(_EASTERN_ARABIC)})


def normalize_digits(s: Any) -> str:
    """يحوّل الأرقام العربية/الفارسية إلى لاتينية، والفاصلة العربية إلى نقطة."""
    if s is None:
        return ""
    return str(s).translate(_DIGIT_MAP).replace("،", ".").replace("٫", ".")


def clean_number_str(s: Any) -> str:
    s = normalize_digits(s).strip()
    if not s:
        return ""
    # نفس سلوك النسخة الأصلية: الفاصلة الإنجليزية = فاصل آلاف، وأي رمز عملة يُحذف
    s = s.replace(",", "")
    s = re.sub(r"[^\d.\-]", "", s)
    # "1234.5." (نقطة زائدة من رمز عملة مثل ل.س) -> خذ أول رقم صالح فقط، كـ parseFloat في JS
    m = re.search(r"-?\d+(?:\.\d+)?|-?\.\d+", s)
    return m.group(0) if m else ""


def to_decimal(value: Any, default: Decimal | None = None) -> Decimal:
    """تحويل آمن لأي قيمة (رقم/نص عربي/None) إلى Decimal.

    float يمر عبر str() حتى لا نرث أخطاء التمثيل الثنائي (0.1 -> 0.1 وليس 0.1000000000000000055).
    """
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return default if default is not None else ZERO
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, (int,)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    cleaned = clean_number_str(value)
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return default if default is not None else ZERO


def parse_num(value: Any) -> Decimal | None:
    """مثل parseNum في JS لكن يرجع None بدل NaN عند الفشل."""
    cleaned = clean_number_str(value)
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def q2(value: Any) -> Decimal:
    """تقريب إلى منزلتين (ROUND_HALF_UP)."""
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


CURRENCY = "$"      # العملة المعروضة: دولار أمريكي (لا تؤثر على الحسابات، فقط على الشكل)


def fmt_num(value: Any) -> str:
    """رقم منسّق بلا رمز عملة: 1,234.5 — للحقول القابلة للتعديل والتصدير."""
    d = q2(value)
    text = f"{d:,.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def fmt_money(value: Any, symbol: bool = True) -> str:
    """مبلغ للعرض بالدولار: $1,234.5 — والسالب -$12 (الرمز قبل الرقم بلا فراغ حتى لا ينقلب في العربية)."""
    text = fmt_num(value)
    if not symbol:
        return text
    return f"-{CURRENCY}{text[1:]}" if text.startswith("-") else f"{CURRENCY}{text}"


def to_json_number(value: Decimal) -> int | float:
    """للتخزين في JSON (Supabase jsonb): رقم عادي، متوافق مع بيانات النسخة القديمة."""
    d = q2(value)
    return int(d) if d == d.to_integral_value() else float(d)
