"""تحليل الصور بنموذج «مساعد شركة العمر» (Gemini) بدل OpenCV وTesseract.

لماذا؟ opencv وpytesseract وnumpy لا تعمل على أندرويد/iOS، أما النموذج متعدد الوسائط فيقرأ الفاتورة
العربية المصوّرة (مائلة، مظلّلة، بخط يد أحياناً) مباشرة ويعيد أصنافها منظّمة. الدقة الحسابية لا نتركها
للنموذج وحده: نمرّر كل سطر على `reconcile` (الكمية × السعر = المجموع) ونقارن مجموع السطور بالإجمالي
المكتوب، فأي سطر مشكوك فيه يظهر «يحتاج مراجعة» ولا يدخل المخزون بصمت.

المتطلبات: Pillow فقط (لضغط الصورة) + مفتاح Gemini (أو أي مزوّد يدعم الرؤية) من الإعدادات.
"""
from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from ..ai.media import compress_for_vision, inline_image_part
from ..core.money import ZERO, clean_number_str, q2
from .engine import OcrUnavailable
from .parser import ScannedInvoice, ScannedRow, _close, reconcile
from .preprocess import ImageError, Quality

SCAN_SIDE = 1800          # دقة كافية لقراءة الأسطر الصغيرة في الفواتير الطويلة
PREVIEW_SIDE = 1100
MIN_GOOD_SIDE = 700

INVOICE_SYSTEM = (
    "أنت محاسب خبير في قراءة فواتير الموردين (عربية وإنجليزية) وقطع الغيار. "
    "تستخرج بنود الفاتورة من الصورة بدقة تامة وتُرجع JSON صالحاً فقط، بلا شرح ولا علامات ```."
)

INVOICE_PROMPT = """اقرأ فاتورة المورد في الصورة وأرجع JSON بهذا الشكل بالضبط:
{
  "supplier": "اسم المورد أو الشركة المصدرة أو فارغ",
  "invoice_number": "رقم الفاتورة أو فارغ",
  "date": "YYYY-MM-DD أو فارغ",
  "total": رقم الإجمالي النهائي المكتوب على الفاتورة أو null,
  "items": [
    {"name": "اسم الصنف كما هو مكتوب", "code": "كود/رقم القطعة كما هو مكتوب أو فارغ",
     "qty": الكمية, "unit_price": سعر الوحدة, "line_total": مجموع السطر أو null}
  ],
  "notes": "أي ملاحظة عن وضوح الصورة أو سطور لم تُقرأ، أو فارغ"
}
القواعد:
- سطر واحد لكل صنف حقيقي. لا تضع سطور المجموع/الخصم/الضريبة/المدفوع/الرصيد كأصناف.
- انسخ الأسماء والأكواد حرفياً كما تظهر (حروف وأرقام وشرطات) ولا تخترع أو تكمل ما لا تراه.
- حوّل الأرقام العربية الهندية إلى لاتينية. الأرقام بلا فواصل آلاف وبنقطة عشرية (1250.50).
- إن كان أي رقم غير مقروء ضع null بدل التخمين.
- ترتيب الأصناف كما في الفاتورة من الأعلى للأسفل.
- أرجع JSON فقط."""

BARCODE_SYSTEM = "أنت قارئ باركود دقيق. تُرجع JSON صالحاً فقط."
BARCODE_PROMPT = (
    'اقرأ الباركود (أو رقم الصنف المطبوع تحته/عليه) في الصورة. أرجع JSON: {"codes": ["الرقم الأول", ...]} '
    "بالأرقام/الحروف كما هي بلا مسافات، الأوضح أولاً. إن لم تجد أي باركود أو رقم واضح أرجع {\"codes\": []}."
)


# ------------------------------------------------------------------------------------ مساعدات
def extract_json(text: str) -> Any:
    """يستخرج أول كائن/مصفوفة JSON من ردّ النموذج حتى لو لُفّ بـ ```json أو سبقه كلام."""
    t = (text or "").strip()
    if not t:
        raise ValueError("ردّ فارغ")
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t.find(open_c), t.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except ValueError:
                continue
    raise ValueError("لا يوجد JSON صالح في الردّ")


def _num(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            return None
    else:
        s = clean_number_str(v)
        if not s:
            return None
        try:
            d = Decimal(s)
        except InvalidOperation:
            return None
    return d if d.is_finite() else None


def _str(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, "null") else ""


def invoice_from_json(data: Any) -> ScannedInvoice:
    """يحوّل JSON النموذج إلى ScannedInvoice مع تسوية حسابية لكل سطر وتحذيرات عن الإجمالي."""
    if isinstance(data, list):
        data = {"items": data}
    if not isinstance(data, dict):
        raise ValueError("شكل الردّ غير متوقع")
    inv = ScannedInvoice(
        invoice_number=_str(data.get("invoice_number")), date=_str(data.get("date")),
        supplier=_str(data.get("supplier")), stated_total=_num(data.get("total")), header_found=True,
    )
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        name, code = _str(it.get("name")), _str(it.get("code"))
        qty, cost, total = _num(it.get("qty")), _num(it.get("unit_price")), _num(it.get("line_total"))
        if not (name or code) and qty is None and cost is None:
            continue
        issues: list[str] = []
        confidence = 1.0
        if qty is not None and qty <= 0:
            qty, confidence = None, 0.5
            issues.append("الكمية غير صالحة")
        if cost is not None and cost < 0:
            cost, confidence = None, 0.5
            issues.append("السعر غير صالح")
        q, c, t, fixes = reconcile(qty, cost, total)
        if fixes:
            issues += fixes
            confidence = min(confidence, 0.6)
        if q is None or c is None:
            issues.append("كمية أو سعر غير مقروء — أكمله يدوياً")
            confidence = min(confidence, 0.4)
        if not (name or code):
            issues.append("اسم/كود الصنف غير مقروء")
            confidence = min(confidence, 0.4)
        inv.rows.append(ScannedRow(name=name, code=code, qty=q, cost=c, total=t, confidence=confidence, issues=issues))

    inv.computed_total = q2(sum((r.total if r.total is not None else (r.qty or ZERO) * (r.cost or ZERO)
                                 for r in inv.rows), ZERO))
    if not inv.rows:
        inv.warnings.append("لم يجد النموذج أصنافاً في الصورة — تأكد أنها فاتورة واضحة وكاملة وأعد المحاولة.")
    elif inv.stated_total is not None and not _close(inv.computed_total, inv.stated_total):
        inv.warnings.append(
            f"مجموع الأصناف ({inv.computed_total}) لا يطابق إجمالي الفاتورة ({inv.stated_total}) — "
            "قد يكون سطر ناقصاً أو رقم مقروء خطأ؛ راجع الأسطر البرتقالية."
        )
    note = _str(data.get("notes"))
    if note:
        inv.warnings.append("ملاحظة النموذج: " + note)
    return inv


def _quality_of(image: bytes) -> tuple[bytes, Quality]:
    """معاينة بدون أي مكتبة ثقيلة: Pillow فقط. تنبيهات بسيطة عن صغر الصورة."""
    from PIL import Image
    import io
    jpeg = compress_for_vision(image, PREVIEW_SIDE, 82)
    with Image.open(io.BytesIO(image)) as im:
        w, h = im.size
    q = Quality(document_found=False)
    if max(w, h) < MIN_GOOD_SIDE:
        q.hints.append("الصورة صغيرة الدقة — قد لا تُقرأ الأرقام الدقيقة؛ صوّرها من قرب أو بدقة أعلى")
    elif min(w, h) < 300:
        q.hints.append("الصورة ضيقة جداً — صوّر الفاتورة كاملة")
    return jpeg, q


# ------------------------------------------------------------------------------------ الماسح
class VisionInvoiceScanner:
    """نفس واجهة InvoiceScanner (preview / scan) لكن عبر نموذج الذكاء الاصطناعي."""

    def __init__(self, client_factory: Callable[[], Any]):
        self._factory = client_factory
        self._client: Any = None

    def client(self) -> Any:
        if self._client is None or not getattr(self._client, "ready", False):
            self._client = self._factory()
        if not getattr(self._client, "ready", False):
            raise OcrUnavailable(
                "قراءة الصور تحتاج مفتاح Gemini — أضفه من الإعدادات ← «مزوّدو الذكاء الاصطناعي» "
                "(مجاني من aistudio.google.com/apikey)."
            )
        return self._client

    @property
    def engine_name(self) -> str:
        cfg = getattr(self._client, "cfg", None)
        return f"Gemini · {getattr(cfg, 'model', '') or 'AI'}"

    async def _ask(self, image: bytes, prompt: str, system: str, side: int) -> Any:
        client = self.client()
        try:
            jpeg = await asyncio.to_thread(compress_for_vision, image, side, 88)
        except Exception as e:  # noqa: BLE001 — صورة تالفة/غير مدعومة (data.images.ImageError أو غيرها)
            raise ImageError(str(e) if type(e).__name__ == "ImageError" else
                             "تعذّر فتح الصورة — اختر ملف JPG أو PNG أو WEBP صالحاً") from e
        contents = [{"role": "user", "parts": [inline_image_part(jpeg, "image/jpeg"), {"text": prompt}]}]
        try:
            reply = await client.generate(contents, system, None, google_search=False)
        except TypeError:       # مزوّد لا يعرف الوسيط google_search
            reply = await client.generate(contents, system, None)
        text = (getattr(reply, "text", "") or "").strip()
        if not text:
            raise OcrUnavailable("لم يُرجع النموذج أي نص — أعد المحاولة بصورة أوضح.")
        return extract_json(text)

    async def preview(self, image: bytes):
        try:
            return await asyncio.to_thread(_quality_of, image)
        except ImageError:
            raise
        except Exception as e:  # noqa: BLE001 — صورة تالفة/غير مدعومة
            raise ImageError("تعذّر فتح الصورة — اختر ملف JPG أو PNG أو WEBP صالحاً") from e

    async def scan(self, image: bytes):
        from .scanner import ScanResult
        if not image:
            raise ImageError("الصورة فارغة")
        try:
            data = await self._ask(image, INVOICE_PROMPT, INVOICE_SYSTEM, SCAN_SIDE)
        except ValueError as e:
            if isinstance(e, ImageError):
                raise
            raise OcrUnavailable("ردّ النموذج غير مفهوم، أعد المحاولة. (" + str(e) + ")") from e
        try:
            inv = invoice_from_json(data)
        except ValueError as e:
            raise OcrUnavailable("ردّ النموذج غير مفهوم، أعد المحاولة. (" + str(e) + ")") from e
        png, quality = await self.preview(image)
        return ScanResult(inv, quality, png, self.engine_name, "vision")

    async def read_barcodes(self, image: bytes) -> list[str]:
        try:
            data = await self._ask(image, BARCODE_PROMPT, BARCODE_SYSTEM, 1400)
        except ImageError:
            raise
        except ValueError:
            return []
        codes = data.get("codes") if isinstance(data, dict) else data
        out: list[str] = []
        for c in codes or []:
            c = re.sub(r"\s+", "", _str(c))
            if c and c not in out:
                out.append(c)
        return out


def default_client_factory(settings_getter: Callable[[], dict] | None = None) -> Callable[[], Any]:
    """مصنع العميل: يقرأ مزوّدي الذكاء (Gemini وغيره) من إعدادات البرنامج + متغيرات البيئة."""
    def make():
        from ..ai.llm import build_client, load_specs
        return build_client(load_specs(settings_getter() if settings_getter else None))
    return make
