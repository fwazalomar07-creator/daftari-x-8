"""رسم المستندات المطبوعة (فاتورة، شراء، سند، كشف حساب) كصور وملف PDF.

لماذا Pillow وليس reportlab/HTML؟ لأنه يعمل دون إنترنت وبدون محرك طباعة، ويشكّل الحروف العربية
ويرتّب الاتجاه (RTL) عبر libraqm. إن لم يتوفر raqm على الجهاز (بعض بنايات الأندرويد) يُستخدم
arabic_reshaper + python-bidi تلقائياً إن كانا مثبَّتين — أضفهما في requirements لضمان الحالتين.

الخط: ضع خطاً عربياً في assets/fonts (يُنصح Noto Naskh Arabic أو Amiri أو Cairo)، أو حدّد DAFTARI_FONT.
"""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps, features

from ..core.calc import _parse_dt
from ..core.models import Customer, Invoice, Purchase, Supplier, Voucher
from ..core.money import fmt_money, normalize_digits
from ..core.reports import CustomerStatement, SupplierStatement, is_invoice_paid

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# ألوان الهوية (من CSS الأصلي)
TEAL, TEAL_DARK, TINT = (31, 61, 99), (20, 42, 71), (232, 237, 243)
INK, SOFT, LINE = (27, 46, 42), (91, 108, 103), (228, 224, 214)
RED, GOLD, GREEN = (176, 64, 47), (184, 134, 46), (20, 90, 70)
WHITE, BAND = (255, 255, 255), (31, 54, 80)
CREAM = (246, 238, 218)      # ترويسة الجدول بنفس شكل فاتورة شركة العمر (كريمي + خطوط ذهبية)

# حجم صورة الصنف في الفاتورة/فاتورة الشراء المطبوعة (بكسل عند 150dpi؛ 150px ≈ 2.5 سم) — كانت 84
PRINT_IMG_SIZE = 110

_MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
_AR = re.compile(r"[\u0600-\u06FF]")
HAS_RAQM = features.check("raqm")


def arabic_date(iso: str | datetime | None = None) -> str:
    d = iso if isinstance(iso, datetime) else (_parse_dt(iso) if iso else datetime.now())
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def latin_digits(s) -> str:
    return normalize_digits(str(s)).replace("،", "،")  # الأرقام العربية -> لاتينية (مثل escapeHtml الأصلية)


# ---- الخطوط -------------------------------------------------------------------------------------
# الترتيب: الخطوط التي تضعها أنت في assets/fonts أولاً، ثم خطوط النظام العربية الجاهزة (ويندوز: Segoe UI / Tahoma / Arial)
_REGULAR = ["Cairo-Regular.ttf", "NotoNaskhArabic-Regular.ttf", "NotoSansArabic-Regular.ttf", "Amiri-Regular.ttf",
            "segoeui.ttf", "tahoma.ttf", "arial.ttf", "times.ttf", "DroidNaskh-Regular.ttf",
            "GeezaPro.ttc", "Arial Unicode.ttf", "DejaVuSans.ttf"]
_BOLD = ["Cairo-Bold.ttf", "NotoNaskhArabic-Bold.ttf", "NotoSansArabic-Bold.ttf", "Amiri-Bold.ttf",
         "segoeuib.ttf", "tahomabd.ttf", "arialbd.ttf", "timesbd.ttf", "DejaVuSans-Bold.ttf"]
_FONT_PATH_CACHE: dict[bool, str | None] = {}


# أحرف عيّنة: يجب أن يرسمها الخط كلها وإلا فهو لا يصلح للفواتير. خطوط أندرويد العربية (مثل NotoNaskhArabic في /system/fonts)
# تحوي العربية والأرقام فقط، فتظهر الحروف اللاتينية والشرطة و$ مربعات □ في الأكواد والأسعار (مثل 26300-42040 و$35 وINV-1107).
_COVER_SAMPLE = "AaWwZz09-$.,:/()%+×" + "ابتثجحخدذرزسشصضطظعغفقكلمنهوي"


@lru_cache(maxsize=128)
def _font_covers(path: str) -> bool:
    """هل يرسم الخط العربية واللاتينية والأرقام وعلامات الترقيم؟ الحرف المفقود يُرسم برمز .notdef (مربع أو فراغ) فنقارنه به."""
    try:
        f = ImageFont.truetype(path, 36, layout_engine=ImageFont.Layout.BASIC)

        def ink(ch: str) -> bytes:
            im = Image.new("L", (64, 64), 0)
            ImageDraw.Draw(im).text((8, 4), ch, font=f, fill=255)
            return im.tobytes()
        missing = ink("\uFFFF")
        return all(ink(ch) != missing for ch in _COVER_SAMPLE)
    except Exception:  # noqa: BLE001
        return False


def _find_font(names: list[str]) -> str | None:
    env = os.getenv("DAFTARI_FONT")
    if env and Path(env).exists() and names is _REGULAR:
        return env
    roots = [ASSETS / "fonts", Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
             Path.home() / ".fonts", Path("/system/fonts"), Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts", Path("/Library/Fonts"),
             Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental")]
    first: str | None = None
    for n in names:
        for r in roots:
            if r.exists():
                for hit in r.rglob(n):
                    first = first or str(hit)
                    if _font_covers(str(hit)):          # نتخطّى الخط العربي-فقط ونكمل للخط الذي يغطي كل شيء (DejaVu المرفق)
                        return str(hit)
    return first                                         # لا يوجد خط كامل: نرجع أول خط وُجد كما كان سابقاً


def _faux_bold() -> bool:
    """Cairo-Regular موجود بلا Cairo-Bold: نرسم الخط العريض بتسميك الحواف (stroke) من نفس الخط ليبقى الشكل موحّداً."""
    fonts = ASSETS / "fonts"
    return (fonts / "Cairo-Regular.ttf").exists() and not (fonts / "Cairo-Bold.ttf").exists()


def _font_path(bold: bool) -> str | None:
    """يبحث عن الخط مرة واحدة فقط ثم يتذكره (البحث الدوري في كل المجلدات كان بطيئاً)."""
    if bold not in _FONT_PATH_CACHE:
        if bold and _faux_bold():
            _FONT_PATH_CACHE[bold] = _find_font(_REGULAR)
        else:
            _FONT_PATH_CACHE[bold] = _find_font(_BOLD if bold else _REGULAR) or _find_font(_REGULAR)
    return _FONT_PATH_CACHE[bold]


@lru_cache(maxsize=64)
def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    path = _font_path(bold)
    if not path:
        raise RuntimeError("لا يوجد خط عربي — ضع ملف خط (.ttf) في daftari/assets/fonts أو عيّن DAFTARI_FONT")
    return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM if HAS_RAQM else ImageFont.Layout.BASIC)


@lru_cache(maxsize=8)
def _missing_forms(path: str) -> dict[str, str]:
    """أشكال الحروف المركّبة (FE70–FEFF) التي لا يحويها الخط → الحرف الأساسي المقابل.

    بدون raqm يشكّل arabic_reshaper الحروف إلى «أشكال عرض» (isolated/initial/medial/final)، لكن خطوطاً مثل Cairo
    لا تحوي بعضها (خصوصاً الشكل المنفصل لـ ا و ل و ب …) فتظهر □ في الفاتورة. الشكل المنفصل يطابق الحرف الأساسي رسماً،
    فنستبدله به. نكتشف المفقود مرة واحدة بمقارنة الرسم برمز .notdef (بلا fontTools لأنه غير متاح على الأندرويد).
    """
    import unicodedata
    out: dict[str, str] = {}
    try:
        f = ImageFont.truetype(path, 36, layout_engine=ImageFont.Layout.BASIC)

        def ink(ch: str) -> bytes:
            im = Image.new("L", (64, 64), 0)
            ImageDraw.Draw(im).text((8, 4), ch, font=f, fill=255)
            return im.tobytes()
        notdef = ink("\uFFFF")
        for cp in list(range(0xFB50, 0xFDFF)) + list(range(0xFE70, 0xFEFD)):
            ch = chr(cp)
            dec = unicodedata.decomposition(ch)
            if not dec or ink(ch) != notdef:
                continue
            parts = dec.split()
            if len(parts) == 2 and parts[0] in ("<isolated>", "<final>", "<initial>", "<medial>"):
                base = chr(int(parts[1], 16))
                if ink(base) != notdef:
                    out[ch] = base
            elif parts[0] == "<isolated>" and len(parts) > 2:      # ligatures مفقودة (لا/لأ…): نفكّكها إلى حروفها
                out[ch] = "".join(chr(int(p, 16)) for p in parts[1:])
    except Exception:  # noqa: BLE001
        pass
    return out


def _fill_missing(s: str) -> str:
    try:
        path = _font_path(False)
        table = _missing_forms(path) if path else {}
    except Exception:  # noqa: BLE001
        return s
    if not table:
        return s
    return "".join(table.get(ch, ch) for ch in s)


def _shape(s: str) -> str:
    """بدون raqm: تشكيل الحروف + قلب الاتجاه يدوياً (مكتبتا arabic_reshaper/bidi إن وُجدتا، وإلا المشكِّل المدمج)."""
    if HAS_RAQM or not _AR.search(s):
        return s
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return _fill_missing(get_display(arabic_reshaper.reshape(s)))
    except Exception:  # noqa: BLE001 — غير مثبتتين: نستخدم المدمج (لا يحتاج أي تثبيت)
        from .arabic import visual
        return _fill_missing(visual(s))


def prewarm() -> None:
    """يُشغَّل بخيط خلفي عند فتح التطبيق: يبحث عن الخط ويفحصه ويحمّل أحجامه، فلا تتأخر أول فاتورة تُحفظ."""
    try:
        for bold in (False, True):
            _font_path(bold)
            for size in (20, 22, 24, 26, 28, 30, 32, 34):
                _font(size, bold)
        p = _font_path(False)
        if p and not HAS_RAQM:
            _missing_forms(p)
        f = _font(26, False)
        f.getlength(_shape("شركة العمر للزيوت والفلاتر 123"))     # يسخّن محرك النصوص والتشكيل
    except Exception as e:  # noqa: BLE001
        print("prewarm:", e)


# ---- لوحة الرسم -------------------------------------------------------------------------------------
class Doc:
    """صفحات A4 بدقة 150dpi. الإحداثيات بالبكسل، والاتجاه من اليمين لليسار."""

    def __init__(self, landscape: bool = False):
        self.w, self.h = (1754, 1240) if landscape else (1240, 1754)
        self.margin = 70
        self.pages: list[Image.Image] = []
        self.excel = None          # دالة تُرجع bytes لملف xlsx (للفواتير فقط)
        self.y = 0
        self._d: ImageDraw.ImageDraw | None = None
        self.new_page()

    # -- صفحات
    def new_page(self) -> None:
        im = Image.new("RGB", (self.w, self.h), WHITE)
        self.pages.append(im)
        self._d = ImageDraw.Draw(im)
        self.y = self.margin

    @property
    def right(self) -> int:
        return self.w - self.margin

    @property
    def left(self) -> int:
        return self.margin

    @property
    def inner_w(self) -> int:
        return self.w - 2 * self.margin

    def ensure(self, height: int) -> bool:
        """يفتح صفحة جديدة إن لم يتسع المحتوى. يرجع True إن فتح صفحة."""
        if self.y + height > self.h - self.margin:
            self.new_page()
            return True
        return False

    # -- نص
    def measure(self, s: str, size: int = 26, bold: bool = False) -> int:
        s = _shape(latin_digits(s))
        kw = ({"direction": "rtl"} if _AR.search(s) else {"direction": "ltr"}) if HAS_RAQM else {}
        return int(self._d.textlength(s, font=_font(size, bold), **kw))

    def text(self, x: int, y: int, s: str, size: int = 26, bold: bool = False, color=INK, align: str = "r") -> int:
        s = _shape(latin_digits(s))
        anchor = {"r": "ra", "l": "la", "m": "ma"}[align]
        # الاتجاه صريح دائماً: نص عربي = RTL، وغيره (أرقام/إشارات) = LTR حتى لا يصير -57 → 57-
        kw = ({"direction": "rtl", "language": "ar"} if _AR.search(s) else {"direction": "ltr"}) if HAS_RAQM else {}
        if bold and _faux_bold():
            kw = {**kw, "stroke_width": max(1, size // 30), "stroke_fill": color}
        self._d.text((x, y), s, font=_font(size, bold), fill=color, anchor=anchor, **kw)
        return int(size * 1.45)

    def wrap(self, s: str, size: int, bold: bool, max_w: int) -> list[str]:
        words, lines, cur = str(s).split(), [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if self.measure(trial, size, bold) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines or [""]

    def hline(self, y: int | None = None, color=LINE, width: int = 2, x1: int | None = None, x2: int | None = None) -> None:
        y = self.y if y is None else y
        self._d.line((x1 or self.left, y, x2 or self.right, y), fill=color, width=width)

    def rect(self, box, fill=None, outline=None, width: int = 2, radius: int = 0) -> None:
        if radius:
            self._d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
        else:
            self._d.rectangle(box, fill=fill, outline=outline, width=width)

    def paste(self, img: Image.Image, x: int, y: int) -> None:
        self.pages[-1].paste(img, (x, y))

    # -- جدول
    def table(self, cols: Sequence[tuple[str, float, str]], rows: Sequence[Sequence[str]], *, size: int = 25,
              header_size: int = 25, empty_text: str = "لا توجد بيانات", row_colors: Sequence[Sequence] | None = None,
              row_images: Sequence[bytes | None] | None = None, img_size: int = 84, header_fill=None,
              grid: bool = False, bold_first_col: bool = False, image_col: bool = False,
              header_color=None) -> None:
        """cols: [(العنوان، نسبة العرض، المحاذاة r|l|m)] مرتبة من اليمين لليسار. تكرار الترويسة عند كل صفحة."""
        total = sum(c[1] for c in cols)
        widths = [int(self.inner_w * c[1] / total) for c in cols]
        pad = 12

        def col_x_right(i: int) -> int:
            return self.right - sum(widths[:i])

        GRID = (150, 150, 150)

        def cell_boxes(y1: int, y2: int, fill=None) -> None:
            """شبكة كاملة كما في الشاشة: إطار لكل خلية (مشاركة الحدود بين الخلايا)."""
            for i in range(len(cols)):
                xr = col_x_right(i)
                self.rect((xr - widths[i], y1, xr, y2), fill=fill, outline=GRID, width=2)

        def header() -> None:
            h = int(header_size * 1.45) + 2 * pad
            if grid:
                cell_boxes(self.y, self.y + h, fill=(243, 243, 243))
            else:
                self.rect((self.left, self.y, self.right, self.y + h), fill=header_fill or TEAL)
            for i, (title, _, align) in enumerate(cols):
                xr = col_x_right(i)
                self._cell(title, xr - widths[i], xr, self.y + pad, header_size, True,
                           header_color or (INK if grid else WHITE), align, pad)
            self.y += h

        header()
        if not rows:
            self.y += 20
            self.text(self.w // 2, self.y, empty_text, 26, color=SOFT, align="m")
            self.y += 50
            return
        line_h = int(size * 1.45)
        # image_col: عمود مستقل أخير (أقصى اليسار) يُرسم دائماً بإطار ذهبي مربّع، وبأيقونة بديلة إن لم تتوفر صورة
        has_imgs = image_col or (bool(row_images) and any(row_images))
        shift = 0 if image_col else ((img_size + pad) if has_imgs else 0)     # عرض مساحة الصورة داخل عمود الصنف
        for ri, row in enumerate(rows):
            wrapped = [self.wrap(str(cell), size, False, widths[i] - 2 * pad - (shift if i == 0 else 0))
                       if cols[i][2] == "r" else [str(cell)]
                       for i, cell in enumerate(row)]
            n = max(len(w) for w in wrapped)
            rh = max(n * line_h + 2 * pad, (img_size + 2 * pad) if has_imgs else 0)
            if self.ensure(rh + 10):
                header()
            if grid:
                cell_boxes(self.y, self.y + rh)
            elif ri % 2:
                self.rect((self.left, self.y, self.right, self.y + rh), fill=(250, 249, 245))
            if image_col:
                self._square_image(row_images[ri] if row_images and ri < len(row_images) else None,
                                   col_x_right(len(cols) - 1) - widths[-1], col_x_right(len(cols) - 1),
                                   self.y, rh, img_size)
            elif has_imgs and row_images[ri]:
                try:
                    im = Image.open(io.BytesIO(row_images[ri])).convert("RGB")
                    im = ImageOps.contain(im, (img_size, img_size), Image.LANCZOS)   # يكبّر أو يصغّر ليملأ المربع بوضوح
                    ix = self.right - pad - img_size + (img_size - im.width) // 2
                    iy = self.y + (rh - im.height) // 2
                    self.paste(im, ix, iy)
                    self.rect((ix - 1, iy - 1, ix + im.width, iy + im.height), outline=LINE, width=1)
                except Exception:  # noqa: BLE001 — صورة تالفة لا توقف الطباعة
                    pass
            vcenter = (rh - n * line_h) // 2 if has_imgs else pad
            for i, lines in enumerate(wrapped):
                xr = col_x_right(i)
                color = (row_colors[ri][i] if row_colors and row_colors[ri][i] else INK)
                for li, ln in enumerate(lines):
                    self._cell(ln, xr - widths[i], xr - (shift if i == 0 else 0), self.y + max(pad, vcenter) + li * line_h,
                               size, bool(bold_first_col and i == 0), color, cols[i][2], pad)
            self.y += rh
            if not grid:
                self.hline(color=LINE, width=1)

    def _square_image(self, data: bytes | None, x1: int, x2: int, y: int, rh: int, size: int) -> None:
        """صورة الصنف في مربع أبيض بإطار ذهبي، متوسّطة في خليتها. بلا صورة: أيقونة صندوق رمادية داخل نفس الإطار."""
        cx, cy = (x1 + x2) // 2, y + rh // 2
        box = (cx - size // 2, cy - size // 2, cx + size // 2, cy + size // 2)
        self.rect(box, fill=WHITE, outline=GOLD, width=3, radius=10)
        inner = size - 10
        ix0, iy0 = box[0] + 5, box[1] + 5
        if data:
            try:
                im = Image.open(io.BytesIO(data))
                try:
                    im.draft("RGB", (inner * 2, inner * 2))   # JPEG: فكّ الترميز بحجم مصغّر مباشرة (أسرع عدة أضعاف)
                except Exception:  # noqa: BLE001
                    pass
                im = ImageOps.contain(im.convert("RGB"), (inner, inner), Image.LANCZOS)
                canvas = Image.new("RGB", (inner, inner), WHITE)
                canvas.paste(im, ((inner - im.width) // 2, (inner - im.height) // 2))
                self.pages[-1].paste(canvas, (ix0, iy0))
                self.rect(box, outline=GOLD, width=3, radius=10)      # نعيد الإطار فوق الصورة ليبقى كاملاً
                return
            except Exception:  # noqa: BLE001
                pass
        g = (190, 190, 190)
        bw, bh = int(size * 0.42), int(size * 0.3)
        bx, by = cx - bw // 2, cy - bh // 2 + 6
        self._d.rectangle((bx, by, bx + bw, by + bh), outline=g, width=4)
        self._d.rectangle((bx - 6, by - 14, bx + bw + 6, by + 4), outline=g, width=4)
        self._d.line((cx - 12, by + 16, cx + 12, by + 16), fill=g, width=4)

    def _cell(self, s, x1, x2, y, size, bold, color, align, pad):
        if align == "r":
            self.text(x2 - pad, y, s, size, bold, color, "r")
        elif align == "l":
            self.text(x1 + pad, y, s, size, bold, color, "l")
        else:
            self.text((x1 + x2) // 2, y, s, size, bold, color, "m")

    # -- إخراج
    def png_pages(self) -> list[bytes]:
        out = []
        for p in self.pages:
            b = io.BytesIO()
            p.save(b, "PNG", compress_level=1)       # ضغط خفيف: أسرع بكثير على الهاتف، والفرق في الحجم لا يُلحظ
            out.append(b.getvalue())
        return out

    def add_page_numbers(self, label: str = "صفحة {i} من {n}", size: int = 20) -> None:
        """يكتب «صفحة i من n» أسفل كل صفحة (يُستدعى بعد اكتمال المحتوى)."""
        n = len(self.pages)
        if n < 1:
            return
        keep = self._d
        for i, page in enumerate(self.pages, 1):
            self._d = ImageDraw.Draw(page)
            self.text(self.w // 2, self.h - self.margin + 16, label.format(i=i, n=n), size, color=SOFT, align="m")
        self._d = keep

    def save_pdf(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        first, rest = self.pages[0], self.pages[1:]
        first.save(path, "PDF", resolution=150.0, save_all=True, append_images=rest)
        return path


# ---- أجزاء مشتركة -------------------------------------------------------------------------------------
def _logo_band(doc: Doc, settings: dict) -> None:
    path = next(iter(ASSETS.glob("logo.*")), None)
    if path:
        img = Image.open(path).convert("RGB")
        h = int(img.height * doc.w / img.width)
        doc.paste(img.resize((doc.w, h), Image.LANCZOS), 0, 0)
        doc.y = h + 36
    else:
        doc.rect((0, 0, doc.w, 150), fill=BAND)
        doc.text(doc.w // 2, 50, settings.get("businessName", ""), 48, True, WHITE, "m")
        doc.y = 190


def _head(doc: Doc, settings: dict, right_lines: list[tuple[str, int, bool, tuple]], meta: list[str]) -> None:
    top = doc.y
    y = top
    for text, size, bold, color in right_lines:
        y += doc.text(doc.right, y, text, size, bold, color, "r")
    my = top
    for line in meta:
        my += doc.text(doc.left, my, line, 26, False, INK, "l")
    doc.y = max(y, my) + 20


def _business_lines(settings: dict, show_company: bool = True) -> list[tuple[str, int, bool, tuple]]:
    lines = [(settings.get("businessName") or "", 34, True, INK)]
    if settings.get("tagline"):
        lines.append((settings["tagline"], 24, False, SOFT))
    if settings.get("phone"):
        lines.append((f"هاتف المحل: {settings['phone']}", 24, False, SOFT))
    if show_company and settings.get("companyNumber"):
        lines.append((f"رقم الشركة: {settings['companyNumber']}", 24, False, SOFT))
    return lines


def _totals_box(doc: Doc, lines: list[tuple[str, str, tuple, bool]]) -> None:
    """صندوق المجاميع على اليسار (flex-end في RTL). lines: (التسمية، القيمة، اللون، مميّز)."""
    line_h = 46
    h = len(lines) * line_h + 24
    doc.y += 12
    doc.ensure(h + 30)
    bw = 520
    x1, x2 = doc.left, doc.left + bw
    y = doc.y + 14
    for label, value, color, grand in lines:
        if grand:
            doc.hline(y - 4, GOLD, 3, x1, x2)
        doc.text(x2, y, label, 28 if grand else 26, grand, color, "r")
        doc.text(x1, y, value, 28 if grand else 26, grand, color, "l")
        y += line_h
    doc.y = y + 10


def _draw_barcode(doc: Doc, barcode: bytes | None, max_w: int = 760, max_h: int = 300) -> None:
    """يرسم صورة باركود/QR في وسط الصفحة تحت التوقيع إن وُجدت."""
    if not barcode:
        return
    try:
        img = Image.open(io.BytesIO(barcode)).convert("RGB")
        img = ImageOps.contain(img, (max_w, max_h), Image.LANCZOS)   # يكبّر أيضاً (contain) ليسهل مسحه بالماسح/الكاميرا
        doc.ensure(img.height + 60)
        doc.y += 10
        x = (doc.w - img.width) // 2
        doc.paste(img, x, doc.y)
        doc.y += img.height + 24
    except Exception:
        pass


def _signature_and_footer(doc: Doc, settings: dict, signer: str, barcode: bytes | None = None) -> None:
    doc.ensure(190)
    doc.y += 40
    x1, x2 = doc.left, doc.left + 440
    doc.hline(doc.y, SOFT, 2, x1, x2)
    doc.text((x1 + x2) // 2, doc.y + 8, signer, 24, False, SOFT, "m")
    doc.y += 90
    _draw_barcode(doc, barcode)
    footer = "شكرًا لتعاملكم معنا" + (f" — للتواصل: {settings['phone']}" if settings.get("phone") else "")
    doc.text(doc.w // 2, doc.y, footer, 24, False, SOFT, "m")


def resolve_barcode(inv: Invoice | None, settings: dict, override: bytes | None = None) -> bytes | None:
    """أولوية: override (مسودة) ← باركود محفوظ على الفاتورة ← باركود المتجر في الإعدادات."""
    if override:
        return override
    if inv is not None:
        b64 = (inv.extra or {}).get("barcode")
        if b64 and isinstance(b64, str):
            try:
                import base64
                return base64.b64decode(b64)
            except Exception:
                pass
    return store_barcode_bytes(settings)


def store_barcode_bytes(settings: dict | None) -> bytes | None:
    """باركود المتجر: من الملف إن وُجد، وإلا من النسخة المحفوظة داخل الإعدادات (تبقى حتى لو نُقل الملف أو فُتح من جهاز آخر)."""
    st = settings or {}
    path = st.get("storeBarcode") or ""
    if path:
        try:
            p = Path(path)
            if p.is_file():
                return p.read_bytes()
        except OSError:
            pass
    b64 = st.get("storeBarcodeB64")
    if b64 and isinstance(b64, str):
        try:
            import base64
            return base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            pass
    return None


def _m(v) -> str:
    return fmt_money(v)


# ---- المستندات ------------------------------------------------------------------------------------------
def _row_images(items, images: dict | None, settings: dict) -> list[bytes | None] | None:
    """قائمة صور البنود (None لبند بلا صورة). عمود الصورة يظهر دائماً ما لم تُعطَّل «طباعة الصور» من الإعدادات."""
    if settings.get("printImages") is False or not items:
        return None
    images = images or {}
    return [images.get(getattr(l, "id", None)) for l in items]


def _cols(price_title: str, imgs: bool) -> list[tuple[str, float, str]]:
    """أعمدة جدول البنود بترتيب شركة العمر (من اليمين): الصنف، الكود، الكمية، السعر، الإجمالي، ثم الصورة."""
    if imgs:
        return [("الصنف", 34, "r"), ("الكود", 14, "m"), ("الكمية", 9, "m"), (price_title, 13, "m"),
                ("الإجمالي", 14, "m"), ("الصورة", 16, "m")]
    return [("الصنف", 38, "r"), ("الكود", 16, "m"), ("الكمية", 11, "m"), (price_title, 15, "m"), ("الإجمالي", 20, "m")]


def invoice_doc(inv: Invoice, settings: dict, images: dict[str, bytes] | None = None,
                barcode: bytes | None = None) -> Doc:
    doc = Doc()
    _logo_band(doc, settings)
    meta = [f"فاتورة رقم: {inv.number}", f"التاريخ: {arabic_date(inv.date)}"] + ([f"العميل: {inv.customer}"] if inv.customer else [])
    _head(doc, settings, _business_lines(settings), meta)
    doc.hline(color=GOLD, width=3)
    doc.y += 14
    imgs = _row_images(inv.items, images, settings)
    doc.table(
        _cols("السعر", imgs is not None),
        [[l.name, l.code or "—", str(l.qty), _m(l.price), _m(l.price * l.qty)] + ([""] if imgs is not None else [])
         for l in inv.items],
        row_images=imgs, img_size=PRINT_IMG_SIZE, image_col=imgs is not None,
        header_fill=CREAM, header_color=INK,
    )
    lines: list[tuple[str, str, tuple, bool]] = []
    if inv.discount:
        lines += [("المجموع الفرعي", _m(inv.subtotal), INK, False), ("الخصم", "-" + _m(inv.discount), RED, False)]
    lines += [("الإجمالي الكلي", _m(inv.total), INK, True), ("المدفوع الآن", _m(inv.paid), INK, False)]
    if inv.customer and inv.customer_prior_debt:
        lines.append(("دين سابق على العميل", _m(inv.customer_prior_debt), RED, False))
    if inv.remaining > 0:
        lines.append(("المتبقي من هذه الفاتورة", _m(inv.remaining), RED, False))
    if inv.customer and inv.customer_total_debt_after > 0:
        lines.append(("إجمالي الدين على العميل الآن", _m(inv.customer_total_debt_after), RED, True))
    _totals_box(doc, lines)
    if inv.notes:
        doc.ensure(60)
        doc.text(doc.right, doc.y, f"ملاحظات: {inv.notes}", 26)
        doc.y += 50
    bc = resolve_barcode(inv, settings, override=barcode)
    _signature_and_footer(doc, settings, "توقيع العميل", barcode=bc)

    def _excel() -> bytes:                       # زر «Excel» في شاشة العرض يستدعيها (تُبنى عند الطلب فقط)
        from .xlsx_invoice import invoice_xlsx
        return invoice_xlsx(inv, settings, images, barcode=barcode)
    doc.excel = _excel
    return doc


def purchase_doc(p: Purchase, settings: dict, images: dict[str, bytes] | None = None) -> Doc:
    doc = Doc()
    _logo_band(doc, settings)
    meta = [f"رقم: {p.number}", f"التاريخ: {arabic_date(p.date)}"]
    if p.supplier:
        meta.append(f"المورد: {p.supplier}")
    if p.supplier_invoice_number:
        meta.append(f"رقم فاتورة المورد: {p.supplier_invoice_number}")
    _head(doc, settings, [(settings.get("businessName") or "", 34, True, INK), ("فاتورة شراء", 26, False, SOFT)], meta)
    doc.hline(color=GOLD, width=3)
    doc.y += 14
    imgs = _row_images(p.items, images, settings)
    doc.table(
        _cols("تكلفة الوحدة", imgs is not None),
        [[l.name, l.code or "—", str(l.qty), _m(l.cost), _m(l.cost * l.qty)] + ([""] if imgs is not None else [])
         for l in p.items],
        row_images=imgs, img_size=PRINT_IMG_SIZE, image_col=imgs is not None,
        header_fill=CREAM, header_color=INK,
    )
    lines = [("الإجمالي الكلي", _m(p.subtotal), INK, True)]
    if p.remaining > 0:
        lines += [("المدفوع", _m(p.paid), INK, False), ("المتبقي (دين علينا)", _m(p.remaining), RED, False)]
    _totals_box(doc, lines)
    if p.notes:
        doc.ensure(60)
        doc.text(doc.right, doc.y, f"ملاحظات: {p.notes}", 26)
        doc.y += 50
    _signature_and_footer(doc, settings, "توقيع المستلم")
    return doc


def voucher_doc(v: Voucher, settings: dict) -> Doc:
    """سند قبض/دفع بالعرض (landscape) مع شعار جانبي — مطابق لتخطيط vdoc الأصلي."""
    doc = Doc(landscape=True)
    is_receipt = v.type == "receipt"
    title = "سند قبض" if is_receipt else "سند دفع"
    top = doc.y
    logo = next(iter(ASSETS.glob("voucher_logo.*")), None)
    if logo:
        img = Image.open(logo).convert("RGB")
        lh = 230
        img = img.resize((int(img.width * lh / img.height), lh), Image.LANCZOS)
        doc.paste(img, doc.right - img.width, top)
    cx = doc.w // 2
    y = top
    y += doc.text(cx, y, settings.get("businessName") or "", 40, True, INK, "m")
    if settings.get("tagline"):
        y += doc.text(cx, y, settings["tagline"], 24, False, SOFT, "m")
    tw = doc.measure(title, 38, True) + 90
    doc.rect((cx - tw // 2, y + 10, cx + tw // 2, y + 80), outline=GOLD, width=3, radius=14)
    doc.text(cx, y + 20, title, 38, True, INK, "m")
    my = top
    meta = [f"رقم السند: {v.number}"] + ([f"الرقم: {v.ref_number}"] if v.ref_number else []) + \
           [f"التاريخ: {arabic_date(v.date)}"] + ([f"هاتف: {settings['phone']}"] if settings.get("phone") else [])
    for line in meta:
        my += doc.text(doc.left, my, line, 26, False, INK, "l")
    doc.y = top + 260
    doc.hline(color=GOLD, width=3)
    doc.y += 30
    from_label = "استلمنا مبلغًا من" if is_receipt else "دفعنا مبلغًا إلى"
    doc.y += doc.text(doc.right, doc.y, f"{from_label}: {v.person}", 32, True)
    body = ("لقد قبضنا من المذكور أعلاه مبلغاً وقدره " if is_receipt else "لقد دفعنا للمذكور أعلاه مبلغاً وقدره ") + _m(v.amount)
    doc.y += doc.text(doc.right, doc.y + 6, body, 28) + 30
    doc.table(
        [("المبلغ", 20, "m"), ("الرصيد السابق", 22, "m"), ("المتبقي بعد السند", 24, "m"), ("ملاحظات", 34, "r")],
        [[_m(v.amount), _m(v.previous_balance) if v.previous_balance is not None else "—",
          _m(v.remaining_balance) if v.remaining_balance is not None else "—", v.note or "—"]],
    )
    doc.y += 90
    for xr, label in ((doc.right, "توقيع المُسلِّم"), (doc.left + 480, "اسم وتوقيع المستلم")):
        doc.hline(doc.y, SOFT, 2, xr - 440, xr)
        doc.text(xr - 220, doc.y + 8, label, 24, False, SOFT, "m")
    doc.y += 90
    doc.text(doc.w // 2, doc.y, "شكرًا لتعاملكم معنا" + (f" — للتواصل: {settings['phone']}" if settings.get("phone") else ""),
             24, False, SOFT, "m")
    return doc


def _statement_logo(doc: Doc) -> None:
    logo = next(iter(ASSETS.glob("voucher_logo.*")), None)
    if logo:
        img = Image.open(logo).convert("RGB")
        lh = 200
        img = img.resize((int(img.width * lh / img.height), lh), Image.LANCZOS)
        doc.paste(img, (doc.w - img.width) // 2, doc.y)
        doc.y += lh + 20


def customer_statement_doc(st: CustomerStatement, settings: dict, limit: int | None = None) -> Doc:
    """st يجب أن يكون مبنياً بـ oldest_first=True (الأقدم أولاً كما في الكشف المطبوع)."""
    c = st.customer
    doc = Doc()
    _statement_logo(doc)
    title = "كشف حساب" + (f" (آخر {limit} فاتورة)" if limit else " كامل")
    meta = [title, f"العميل: {c.name}"] + ([f"هاتف العميل: {c.phone}"] if c.phone else []) + [f"تاريخ الكشف: {arabic_date()}"]
    _head(doc, settings, _business_lines(settings, False), meta)
    rows, colors = [], []
    for inv in st.invoices:
        paid = is_invoice_paid(inv)
        for l in inv.items:
            rows.append([inv.number, arabic_date(inv.date), l.name, l.code or "—", _m(l.price), str(l.qty),
                         _m(l.price * l.qty), "مدفوع" if paid else "غير مدفوع"])
            colors.append([None] * 7 + [GREEN if paid else RED])
    doc.table([("رقم الفاتورة", 15, "m"), ("التاريخ", 15, "m"), ("الصنف", 21, "r"), ("الكود", 11, "m"),
               ("السعر", 10, "m"), ("الكمية", 8, "m"), ("الإجمالي", 11, "m"), ("الحالة", 9, "m")],
              rows, size=21, header_size=21, empty_text="لا توجد فواتير مسجلة لهذا العميل.", row_colors=colors or None)
    if st.transactions:
        doc.ensure(120)
        doc.y += 24
        doc.text(doc.right, doc.y, "الحركات المسجلة (دفعات وديون يدوية)", 27, True)
        doc.y += 52
        tx_rows, tx_colors = [], []
        for h in st.transactions:
            pay = h.type == "payment"
            label = "دفعة (سند قبض" + (f" رقم {h.voucher_number}" if h.voucher_number else "") + ")" if pay else "دين يدوي"
            if h.note:
                label += f" · {h.note}"
            tx_rows.append([f"{arabic_date(h.date)} — {label}", ("-" if pay else "+") + _m(h.amount)])
            tx_colors.append([None, GREEN if pay else RED])
        doc.table([("البيان", 75, "r"), ("المبلغ", 25, "m")], tx_rows, size=22, header_size=22, row_colors=tx_colors)
    _totals_box(doc, [
        ("عدد الفواتير", str(len(st.invoices)), INK, False),
        ("عدد الأصناف", str(sum(len(i.items) for i in st.invoices)), INK, False),
        ("إجمالي المبيعات", _m(st.total_sales), INK, True),
        ("إجمالي المدفوع", _m(st.total_paid), GREEN, False),
        ("الرصيد المتبقي (دين)", _m(c.balance), RED, False),
    ])
    _signature_and_footer(doc, settings, "توقيع العميل")
    return doc


def supplier_statement_doc(st: SupplierStatement, settings: dict) -> Doc:
    s = st.supplier
    doc = Doc()
    _statement_logo(doc)
    _head(doc, settings, _business_lines(settings, False),
          ["كشف حساب كامل", f"المورد: {s.name}", f"تاريخ الكشف: {arabic_date()}"])
    rows = [[p.number, arabic_date(p.date), l.name, l.code or "—", _m(l.cost), str(l.qty), _m(l.cost * l.qty)]
            for p in st.purchases for l in p.items]
    doc.table([("رقم الفاتورة", 15, "m"), ("التاريخ", 16, "m"), ("الصنف", 24, "r"), ("الكود", 12, "m"),
               ("السعر", 11, "m"), ("الكمية", 9, "m"), ("الإجمالي", 13, "m")],
              rows, size=21, header_size=21, empty_text="لا توجد فواتير شراء مسجلة لهذا المورد.")
    if st.transactions:
        doc.ensure(120)
        doc.y += 24
        doc.text(doc.right, doc.y, "الحركات المسجلة", 27, True)
        doc.y += 52
        tx_rows, tx_colors = [], []
        for h in st.transactions:
            pay = h.type == "payment"
            label = "دفعة (سند دفع)" if pay else ("دين فاتورة شراء" + (f" رقم {h.purchase_number}" if h.purchase_number else "")
                                                  if h.type == "purchase_debt" else "رصيد افتتاحي")
            tx_rows.append([f"{arabic_date(h.date)} — {label}", ("-" if pay else "+") + _m(h.amount)])
            tx_colors.append([None, GREEN if pay else RED])
        doc.table([("البيان", 75, "r"), ("المبلغ", 25, "m")], tx_rows, size=22, header_size=22, row_colors=tx_colors)
    _totals_box(doc, [
        ("عدد فواتير الشراء", str(len(st.purchases)), INK, False),
        ("عدد الأصناف", str(sum(len(p.items) for p in st.purchases)), INK, False),
        ("إجمالي المشتريات", _m(st.total_purchases), INK, True),
        ("إجمالي المدفوع", _m(st.total_paid), GREEN, False),
        ("الرصيد المتبقي (علينا)", _m(s.balance), RED, False),
    ])
    _signature_and_footer(doc, settings, "توقيع المورد")
    return doc
