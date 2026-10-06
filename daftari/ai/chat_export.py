"""تصدير محادثة «مساعد شركة العمر» إلى Excel وPDF (المحادثة كاملة أو رسالة واحدة).

  • build_entries   — يحوّل رسائل الشاشة (role, content) إلى مدخلات موحّدة (المرسل/الوقت/النص/الصور).
  • chat_workbook   — ملف Excel: ورقة «المحادثة» (رقم، وقت، مرسل، رسالة) + ورقة مستقلة لكل جدول كتبه المساعد.
  • chat_doc        — مستند PDF/صور بنفس محرك الفواتير (printing.render): خط عربي، شعار، جداول، صور، ترقيم صفحات.

الجداول التي كتبها المساعد بصيغة Markdown تُرسم في الـ PDF كجداول حقيقية وتُصدَّر في Excel كأوراق قابلة للفرز والمعادلات.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from . import excel

DEFAULT_AUDIT_NOTE = "تم التدقيق بشكل ممنهج ومنظم في كتابة المعلومات"
DEFAULT_AI_TYPE = "مساعد شركة العمر"
CHAT_LOGO = "chat_logo.png"          # الشعار الافتراضي (PNG شفاف الخلفية)
LOGO_MAX_SIDE = 520
USER_LABEL = "أنت"
BOT_LABEL = "مساعد شركة العمر"
STAMP_FMT = "%Y-%m-%d %H:%M"


def now_stamp() -> str:
    return datetime.now().strftime(STAMP_FMT)


def default_filename(prefix: str = "محادثة-المساعد") -> str:
    return f"{prefix}-{datetime.now():%Y-%m-%d-%H%M}"


# ---------------------------------------------------------------------------------------------- المدخلات
@dataclass
class ChatEntry:
    role: str                       # user | assistant | system
    sender: str
    text: str
    time: str = ""
    kind: str = "text"              # text | error | file | doc | pictures
    images: list[tuple[str, bytes]] = field(default_factory=list)    # (تعليق، JPEG) — مرفقات المالك أو صور أرسلها المساعد


def build_entries(messages: Sequence[tuple[str, Any]], stamps: Sequence[str] | None = None, *,
                  doc_label: Callable[[str], str] | None = None,
                  image_of: Callable[[str], bytes | None] | None = None) -> list[ChatEntry]:
    """رسائل الشاشة ← مدخلات. الصور المرفقة (user_image) تُدمج مع نص المالك التالي لها في مدخل واحد."""
    stamps = list(stamps or [])
    out: list[ChatEntry] = []
    held: list[tuple[str, bytes]] = []          # صور المالك بانتظار نصّه
    held_time = ""

    def flush_images() -> None:
        nonlocal held, held_time
        if held:
            out.append(ChatEntry("user", USER_LABEL, "(صورة مرفقة)", held_time, images=held))
        held, held_time = [], ""

    for i, (role, content) in enumerate(messages):
        when = stamps[i] if i < len(stamps) else ""
        if role == "user_image":
            if isinstance(content, (bytes, bytearray)) and content:
                held.append(("", bytes(content)))
                held_time = held_time or when
            continue
        if role == "user":
            text = str(content or "").strip()
            if text.startswith("📷") and held:               # نص الصورة الافتراضي لا قيمة له في الطباعة
                text = ""
            out.append(ChatEntry("user", USER_LABEL, text, held_time or when, images=held))
            held, held_time = [], ""
            continue
        flush_images()
        if role == "assistant":
            out.append(ChatEntry("assistant", BOT_LABEL, str(content or ""), when))
        elif role == "error":
            out.append(ChatEntry("system", "تنبيه", str(content or ""), when, kind="error"))
        elif role == "file":
            out.append(ChatEntry("assistant", BOT_LABEL, f"ملف Excel أنشأه المساعد: {Path(str(content)).name}", when, kind="file"))
        elif role == "doc":
            label = doc_label(str(content)) if doc_label else str(content)
            out.append(ChatEntry("assistant", BOT_LABEL, f"مستند أصدره المساعد: {label}", when, kind="doc"))
        elif role == "pictures":
            items = content if isinstance(content, list) else []
            imgs: list[tuple[str, bytes]] = []
            for it in items:
                data = image_of(it.get("id", "")) if image_of else None
                if data:
                    imgs.append((str(it.get("name") or ""), data))
            names = "، ".join(str(it.get("name") or "") for it in items if isinstance(it, dict))
            out.append(ChatEntry("assistant", BOT_LABEL, f"صور أصناف أرسلها المساعد: {names}", when, kind="pictures", images=imgs))
        elif role == "catalog_pictures":                  # صور كتالوج (FMI…): تُطبع مع التعليق والمصدر
            items = content if isinstance(content, list) else []
            imgs = [(str(it.get("label") or ""), it["data"]) for it in items if isinstance(it, dict) and it.get("data")]
            names = "، ".join(str(it.get("label") or "") for it in items if isinstance(it, dict))
            out.append(ChatEntry("assistant", BOT_LABEL, f"صور من الكتالوج: {names}", when, kind="pictures", images=imgs))
        elif role == "link":
            info = content if isinstance(content, dict) else {}
            out.append(ChatEntry("assistant", BOT_LABEL, f"رابط الموقع: {info.get('name', '')} — {info.get('url', '')}", when))
    flush_images()
    return out


# ---------------------------------------------------------------------------------------------- Markdown
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_EMPH = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_BULLET = re.compile(r"^\s*[-*+•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*(\d{1,3})[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE = re.compile(r"^([-*_])\1{2,}$")
_NUMERIC = re.compile(r"^[\s$+\-]*\d[\d,.\s]*%?\s*\$?$")
# خط المستندات عربي لا يحوي الإيموجي (تظهر مربعات)؛ نحذفها من الـ PDF فقط
_EMOJI = re.compile("[\U0001F000-\U0001FFFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]")


def inline(s: str) -> str:
    s = _LINK.sub(r"\1 (\2)", str(s))
    s = re.sub(r"(\*\*|__|`|~~)", "", s)
    s = _EMPH.sub(r"\1", s)
    return s.replace("<br>", " ").replace("<br/>", " ").strip()


def parse_blocks(md: str) -> list[tuple]:
    """يقسّم نص Markdown إلى كتل: ("h", مستوى, نص) ("p", نص) ("li", علامة, نص) ("table", عناوين, صفوف) ("hr",) ("gap",)."""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    blocks: list[tuple] = []
    i, n, in_code = 0, len(lines), False
    while i < n:
        line = lines[i]
        st = line.strip()
        if st.startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            if st:
                blocks.append(("p", line.rstrip()))
            i += 1
            continue
        if not st:
            blocks.append(("gap",))
            i += 1
            continue
        if i + 1 < n and "|" in line and excel._SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            headers = excel._cells(line)
            rows, j = [], i + 2
            while j < n and "|" in lines[j] and lines[j].strip():
                rows.append(excel._cells(lines[j]))
                j += 1
            blocks.append(("table", headers, rows))
            i = j
            continue
        if _RULE.match(st):
            blocks.append(("hr",))
        elif (m := _HEADING.match(st)):
            blocks.append(("h", len(m.group(1)), inline(m.group(2))))
        elif (m := _BULLET.match(line)):
            blocks.append(("li", "•", inline(m.group(1))))
        elif (m := _NUMBERED.match(line)):
            blocks.append(("li", f"{m.group(1)}.", inline(m.group(2))))
        else:
            blocks.append(("p", inline(st.lstrip(">").strip())))
        i += 1
    return blocks


def plain_text(md: str, table_names: Sequence[str] | None = None) -> str:
    """نص عادي مقروء لخلية إكسل: بلا رموز Markdown، والجدول يُستبدل بإشارة إلى ورقته المستقلة (إن وُجدت)."""
    out: list[str] = []
    t = 0
    for b in parse_blocks(md):
        k = b[0]
        if k == "gap":
            if out and out[-1] != "":
                out.append("")
        elif k == "h":
            out.append(b[2])
        elif k == "li":
            out.append(f"{b[1]} {b[2]}")
        elif k == "table":
            name = table_names[t] if table_names and t < len(table_names) else None
            t += 1
            out.append(f"[جدول «{name}» — في ورقة مستقلة داخل هذا الملف]" if name
                       else "\n".join(" | ".join(r) for r in [b[1], *b[2]]))
        elif k == "p":
            out.append(b[1])
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------------------------- Excel
def chat_workbook(entries: Sequence[ChatEntry], business: str = "") -> bytes:
    """ورقة «المحادثة» + ورقة لكل جدول Markdown كتبه المساعد (قابلة للفرز والتصفية والمعادلات)."""
    table_sheets: list[excel.Sheet] = []
    names_by_entry: dict[int, list[str]] = {}
    for idx, e in enumerate(entries):
        if e.role != "assistant" or e.kind != "text":
            continue
        for sh in excel.parse_markdown_tables(e.text):
            if re.fullmatch(r"جدول \d+", sh.name):
                sh.name = f"جدول {len(table_sheets) + 1}"
            table_sheets.append(sh)
            names_by_entry.setdefault(idx, []).append(sh.name)
    rows: list[list[Any]] = []
    for idx, e in enumerate(entries):
        if e.role == "assistant" and e.kind == "text":
            body = plain_text(e.text, names_by_entry.get(idx))
        else:
            body = plain_text(e.text) if e.kind == "text" else e.text
        if e.images and e.kind != "pictures":
            body = (body + "\n" if body and body != "(صورة مرفقة)" else "") + f"[{len(e.images)} صورة مرفقة]"
        rows.append([idx + 1, e.time, e.sender, body])
    chat = excel.Sheet("المحادثة", ["#", "الوقت", "المرسل", "الرسالة"], rows, widths={0: 6, 1: 18, 2: 20, 3: 110})
    return excel.build_workbook([chat, *table_sheets], business)


# ---------------------------------------------------------------------------------------------- PDF
# ثيم الطباعة: أسود وأبيض ليناسب الشعار الأسود
BLACK, DARK_GRAY, MID_GRAY = (20, 20, 20), (90, 90, 90), (200, 200, 200)
_USER_BAND = (234, 234, 234)


def clean_logo(data: bytes, max_side: int = LOGO_MAX_SIDE) -> bytes:
    """يجهّز شعاراً للطباعة: PNG بخلفية شفافة مقصوص على حدوده.

    • صورة شفافة أصلاً (PNG بقناة ألفا): تُستخدم كما هي.
    • صورة بخلفية بيضاء/فاتحة (JPG): تُزال الخلفية المتصلة بحواف الصورة فقط، فيبقى لمعان الشعار الداخلي سليماً.
    """
    from PIL import Image, ImageFilter
    try:
        import numpy as np
    except ImportError:      # أندرويد: بلا numpy → عتبة بسيطة بـ Pillow وحدها
        np = None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:  # noqa: BLE001
        raise ValueError("تعذّرت قراءة الصورة — اختر ملف PNG/JPG صالحاً") from e
    rgba = im.convert("RGBA")
    if np is None:
        a_lo, _ = rgba.getchannel("A").getextrema()
        if a_lo > 250:
            mask = rgba.convert("L").point(lambda v: 0 if v >= 235 else 255)
            rgba.putalpha(mask.filter(ImageFilter.GaussianBlur(0.8)))
        alpha = None
    else:
        alpha = np.array(rgba.getchannel("A"))
    if alpha is not None and alpha.min() > 250:                                   # لا شفافية: نزيل الخلفية الفاتحة المتصلة بالحواف
        gray = np.array(rgba.convert("L"))
        try:
            import cv2
            k = 51
            bgest = cv2.GaussianBlur(cv2.dilate(gray, np.ones((k, k), np.uint8)), (k, k), 0).astype(np.int16)  # خلفية محلية (تدرّج/ظل)
            fg = ((bgest - gray.astype(np.int16)) > 38).astype(np.uint8) * 255      # الأجزاء الأغمق من خلفيتها بوضوح
            grow = np.ones((9, 9), np.uint8)
            closed = cv2.dilate(fg, grow)                                            # يسدّ فجوات صغيرة بين أجزاء الشعار
            inv = (closed == 0).astype(np.uint8) * 255
            ff = inv.copy()
            m = np.zeros((gray.shape[0] + 2, gray.shape[1] + 2), np.uint8)
            for seed in ((0, 0), (gray.shape[1] - 1, 0), (0, gray.shape[0] - 1), (gray.shape[1] - 1, gray.shape[0] - 1)):
                if ff[seed[1], seed[0]] == 255:
                    cv2.floodFill(ff, m, seed, 128)                                  # الخلفية الخارجية المتصلة بالحواف
            solid = ff != 128                                                        # الشعار + ثغراته الداخلية (معدن فاتح) مصمتة
            solid = cv2.erode(solid.astype(np.uint8) * 255, grow) > 0                # نعيد الحافة لحجمها الأصلي
            alpha = np.where(solid | (fg > 0), 255, 0).astype(np.uint8)
        except ImportError:                                 # بلا OpenCV: عتبة بسيطة
            alpha = np.where(gray >= 235, 0, 255).astype(np.uint8)
        a_img = Image.fromarray(alpha).filter(ImageFilter.GaussianBlur(0.8))   # حواف ناعمة
        rgba.putalpha(a_img)
    bbox = rgba.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
    if not bbox:
        raise ValueError("لم أجد شعاراً في الصورة (كلها خلفية).")
    pad = 6
    rgba = rgba.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad), min(rgba.width, bbox[2] + pad), min(rgba.height, bbox[3] + pad)))
    rgba.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    rgba.save(out, "PNG", optimize=True)
    return out.getvalue()


def logo_bytes(settings: dict | None, assets: Path | None = None) -> bytes | None:
    """شعار الطباعة: المرفوع من الإعدادات إن وُجد، وإلا الافتراضي من assets."""
    import base64
    settings = settings or {}
    b64 = settings.get("chatPrintLogoB64")
    if b64:
        try:
            return base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            pass
    path = settings.get("chatPrintLogo")
    if path and Path(path).exists():
        try:
            return Path(path).read_bytes()
        except OSError:
            pass
    if assets is None:
        from ..printing.render import ASSETS as assets
    default = Path(assets) / CHAT_LOGO
    return default.read_bytes() if default.exists() else None


def _pdf_text(s: Any) -> str:
    return re.sub(r"[ \t]+", " ", _EMOJI.sub("", str(s))).strip()


def _draw_images(doc, R, images: Sequence[tuple[str, bytes]], size: int = 190, gap: int = 14) -> None:
    from PIL import Image, ImageOps
    if not images:
        return
    x, row_h = doc.right - 10, 0
    doc.ensure(size + 70)
    for cap, data in images:
        if x - size < doc.left:                              # سطر جديد من الصور
            doc.y += row_h + gap
            x, row_h = doc.right - 10, 0
            doc.ensure(size + 70)
        try:
            im = ImageOps.contain(Image.open(io.BytesIO(data)).convert("RGB"), (size, size), Image.LANCZOS)
        except Exception:  # noqa: BLE001 — صورة تالفة لا توقف الطباعة
            continue
        doc.paste(im, x - size + (size - im.width) // 2, doc.y + (size - im.height) // 2)
        doc.rect((x - size, doc.y, x, doc.y + size), outline=R.LINE, width=1)
        h = size
        lines = doc.wrap(_pdf_text(cap), 20, False, size)[:2] if cap else []
        for k, ln in enumerate(lines):
            doc.text(x - size // 2, doc.y + size + 4 + k * 28, ln, 20, False, R.INK, "m")
        if lines:
            h += 4 + 28 * len(lines)
        row_h = max(row_h, h)
        x -= size + gap
    doc.y += row_h + gap


def _draw_table(doc, R, headers: list[str], rows: list[list[str]]) -> None:
    n = max([len(headers)] + [len(r) for r in rows]) if (headers or rows) else 0
    if n == 0:
        return
    heads = [_pdf_text(h) for h in headers] + [""] * (n - len(headers))
    body = [[_pdf_text(c) for c in r] + [""] * (n - len(r)) for r in rows]
    cols = []
    for c in range(n):
        cells = [r[c] for r in body]
        weight = max(6, max([len(heads[c])] + [min(len(x), 36) for x in cells]) + 2)
        numeric = bool(cells) and all((not x) or _NUMERIC.match(x) for x in cells)
        cols.append((heads[c], weight, "m" if numeric else "r"))
    doc.ensure(190)
    doc.table(cols, body, size=23, header_size=23, grid=True, bold_first_col=True)   # شبكة كاملة كما تظهر على الشاشة
    doc.y += 14


def _img_size(n: int, band: bool) -> int:
    """حجم الصورة في الطباعة حسب عددها: القليلة كبيرة وواضحة، والكثيرة (حتى 100) أصغر لتتسع في صفوف."""
    if band:
        return 190
    return 300 if n <= 3 else (230 if n <= 8 else (170 if n <= 30 else 130))


def _draw_entry(doc, R, e: ChatEntry, band: bool = True) -> None:
    """band=False: تُطبع الرسالة نفسها كما تظهر على الشاشة (بلا شريط اسم المرسل) — وضع «رسالة واحدة»."""
    user, err = e.role == "user", e.kind == "error"
    size, lh = 26, int(26 * 1.5)
    head_h = 54 if band else 0
    # نحجز مكاناً يكفي الترويسة مع أول محتوى (صور أو سطرين) حتى لا تبقى الترويسة وحدها أسفل صفحة
    doc.ensure(head_h + 14 + (190 + 70 if e.images else 2 * lh) + 16)
    top = doc.y
    if band:
        fill = (253, 236, 236) if err else (_USER_BAND if user else BLACK)       # رد المساعد: شريط أسود بنص أبيض
        doc.rect((doc.left, top, doc.right, top + head_h), fill=fill, radius=10)
        doc.text(doc.right - 18, top + 10, e.sender, 27, True, R.RED if err else (BLACK if user else (255, 255, 255)), "r")
        if e.time:
            doc.text(doc.left + 18, top + 16, e.time, 21, False, DARK_GRAY if (user or err) else MID_GRAY, "l")
        doc.y = top + head_h + 14
    _draw_images(doc, R, e.images, size=_img_size(len(e.images), band))

    if user or err or e.kind != "text":
        blocks: list[tuple] = [("p", ln) for ln in str(e.text).split("\n")]
        if user and not str(e.text).strip():
            blocks = []
    else:
        blocks = parse_blocks(e.text)
    color = R.RED if err else BLACK
    xr = doc.right - 12
    for b in blocks:
        k = b[0]
        if k == "gap":
            doc.y += 10
        elif k == "hr":
            doc.ensure(24)
            doc.hline(doc.y + 8)
            doc.y += 22
        elif k == "table":
            _draw_table(doc, R, b[1], b[2])
        elif k == "h":
            hs = {1: 35, 2: 31, 3: 29}.get(b[1], 27)
            h_lh = int(hs * 1.5)
            for ln in doc.wrap(_pdf_text(b[2]), hs, True, doc.inner_w - 24):
                doc.ensure(h_lh)
                doc.text(xr, doc.y, ln, hs, True, BLACK, "r")
                doc.y += h_lh
        elif k == "li":
            indent = 54
            lines = doc.wrap(_pdf_text(b[2]), size, False, doc.inner_w - 24 - indent)
            for li, ln in enumerate(lines):
                doc.ensure(lh)
                if li == 0:
                    if b[1] == "•":
                        doc.text(xr, doc.y, "•", size, True, BLACK, "r")
                    else:       # رقم داخل دائرة: علامة «1.» تنقلب إلى «.1» في الاتجاه العربي
                        doc.rect((xr - 40, doc.y + 2, xr, doc.y + 42), fill=(225, 225, 225), radius=20)
                        doc.text(xr - 20, doc.y + 6, b[1].rstrip("."), 22, True, BLACK, "m")
                doc.text(xr - indent, doc.y, ln, size, False, color, "r")
                doc.y += lh
        else:                                                 # p
            text = _pdf_text(b[1])
            if not text:
                continue
            for ln in doc.wrap(text, size, False, doc.inner_w - 24):
                doc.ensure(lh)
                doc.text(xr, doc.y, ln, size, False, color, "r")
                doc.y += lh
    doc.y += 12
    if band:
        doc.ensure(10)
        doc.hline(doc.y, R.LINE, 1)
        doc.y += 22


def _wrap_lines(doc, text: str, size: int, bold: bool, width: int) -> list[str]:
    return [ln for part in str(text).split("\n") for ln in doc.wrap(_pdf_text(part), size, bold, width) if ln]


def _header(doc, R, settings: dict, title: str, count: int, compact: str | None = None) -> None:
    """الشعار الأسود في الزاوية اليمنى، وبيانات النظام (قابلة للتعديل من الإعدادات) في الزاوية اليسرى."""
    from PIL import Image
    top = doc.y = 50
    logo_h, logo_w = 0, 0
    raw = logo_bytes(settings, R.ASSETS)
    if raw:
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGBA")
            logo_h = 230
            logo_w = min(int(img.width * logo_h / img.height), 420)
            logo_h = int(img.height * logo_w / img.width)
            small = img.resize((logo_w, logo_h), Image.LANCZOS)
            doc.pages[-1].paste(small, (doc.right - logo_w, top), small)       # القناة الشفافة كقناع: الخلفية لا تظهر
        except Exception:  # noqa: BLE001 — شعار تالف لا يوقف الطباعة
            logo_h = logo_w = 0
    note = (settings.get("chatPrintNote") if settings.get("chatPrintNote") is not None else DEFAULT_AUDIT_NOTE)
    ai_type = (settings.get("chatPrintAiType") if settings.get("chatPrintAiType") is not None else DEFAULT_AI_TYPE)
    y, max_w = top + 18, doc.inner_w - logo_w - 60
    for ln in _wrap_lines(doc, note, 25, True, max_w):
        doc.text(doc.left, y, ln, 25, True, BLACK, "l")
        y += 40
    if str(ai_type).strip():
        y += 6
        for ln in _wrap_lines(doc, f"نوع الذكاء : {ai_type}", 24, False, max_w):
            doc.text(doc.left, y, ln, 24, False, DARK_GRAY, "l")
            y += 38
    doc.y = max(top + logo_h, y) + 18
    doc.hline(color=BLACK, width=3)
    doc.y += 24
    if compact is not None:        # وضع «الرسالة نفسها»: سطر صغير (الشركة + الوقت) بدل عنوان كبير وعدّاد
        biz = settings.get("businessName") or ""
        if biz:
            doc.text(doc.right, doc.y, biz, 24, True, DARK_GRAY, "r")
        doc.text(doc.left, doc.y + 2, compact or f"{R.arabic_date()}  {datetime.now():%H:%M}", 22, False, DARK_GRAY, "l")
        doc.y += 48
        doc.hline(color=MID_GRAY, width=1)
        doc.y += 26
        return
    h = doc.text(doc.right, doc.y, title, 36, True, BLACK, "r")
    doc.text(doc.left, doc.y + 8, f"{R.arabic_date()}  {datetime.now():%H:%M}", 23, False, DARK_GRAY, "l")
    doc.y += h + 2
    biz = settings.get("businessName") or ""
    doc.text(doc.right, doc.y, f"{biz} — عدد الرسائل: {count}" if biz else f"عدد الرسائل: {count}", 23, False, DARK_GRAY, "r")
    doc.y += 44
    doc.hline(color=MID_GRAY, width=1)
    doc.y += 22


def chat_doc(entries: Sequence[ChatEntry], settings: dict | None = None, title: str = "محادثة مساعد شركة العمر",
             style: str = "chat"):
    """مستند طباعة للمحادثة (يُعرض ويُحفظ PDF/صورة من شاشة المستندات كأي فاتورة). أسود وأبيض بشعار العمر الأسود."""
    from ..printing import render as R
    settings = settings or {}
    doc = R.Doc()
    single = style == "message" and len(entries) == 1
    _header(doc, R, settings, title, len(entries), compact=(entries[0].time or "") if single else None)
    if not entries:
        doc.text(doc.w // 2, doc.y + 40, "لا توجد رسائل", 28, color=DARK_GRAY, align="m")
    for e in entries:
        _draw_entry(doc, R, e, band=not single)
    doc.add_page_numbers()
    return doc
