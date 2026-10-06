"""أدوات واجهة مشتركة (Flet ≥ 0.81) — نظام تصميم عصري متجاوب.

الهوية البصرية: أخضر داكن عميق على خلفية رمادية فاتحة، بطاقات بيضاء مستديرة
بظلال ناعمة — على نمط لوحات التحكم المالية الحديثة.
كل الدوال تحافظ على نفس تواقيعها السابقة حتى تعمل كل التبويبات دون تعديل.
"""
from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any, Awaitable, Callable

import flet as ft

from .safe import safe_update  # noqa: F401  (يُستعمل كـ w.safe_update)

from ..ledger import LedgerError

# ---- لوحة الألوان (هوية «دفتري» 2026: أخضر زمردي + رمادي مزرق هادئ + بنفسجي للمساعد الذكي) ------------------
# الألوان قابلة للتغيير ديناميكياً عبر apply_theme() حسب اختيار المستخدم في الإعدادات.
TEAL, TEAL_DARK, TINT = "#059669", "#047857", "#D1FAE5"        # أخضر نعناعي: #059669 للنصوص/الأيقونات، #D1FAE5 للخلفيات الخفيفة
BG, PANEL, INK, SOFT, LINE = "#F8F9FA", "#FFFFFF", "#1F2937", "#6B7280", "#E5E7EB"
AMBER, AMBER_TINT, RED, RED_TINT = "#B45309", "#FEF3C7", "#DC2626", "#FEE2E2"
GOLD, GOLD_TINT, GREEN, GREEN_TINT = "#B7791F", "#FBF3DC", "#059669", "#D1FAE5"
AI, AI_TINT = "#6D5BD0", "#EFEBFF"                              # لون المساعد الذكي
SLATE_TINT = "#F3F4F6"

RADIUS = 12

# ثيمات جاهزة: الاسم ← قاموس الألوان الأساسية
THEMES: dict[str, dict[str, str]] = {
    "green": {  # الافتراضي — أخضر زمردي
        "TEAL": "#059669", "TEAL_DARK": "#047857", "TINT": "#D1FAE5",
        "BG": "#F8F9FA", "PANEL": "#FFFFFF", "INK": "#1F2937", "SOFT": "#6B7280", "LINE": "#E5E7EB",
        "AI": "#6D5BD0", "AI_TINT": "#EFEBFF", "SLATE_TINT": "#F3F4F6",
        "GREEN": "#059669", "GREEN_TINT": "#D1FAE5",
    },
    "black": {  # داكن أنيق
        "TEAL": "#34D399", "TEAL_DARK": "#10B981", "TINT": "#064E3B",
        "BG": "#0F172A", "PANEL": "#1E293B", "INK": "#F1F5F9", "SOFT": "#94A3B8", "LINE": "#334155",
        "AI": "#A78BFA", "AI_TINT": "#2E1065", "SLATE_TINT": "#1E293B",
        "GREEN": "#34D399", "GREEN_TINT": "#064E3B",
    },
    "gray": {  # رمادي هادئ
        "TEAL": "#4B5563", "TEAL_DARK": "#374151", "TINT": "#F3F4F6",
        "BG": "#F3F4F6", "PANEL": "#FFFFFF", "INK": "#111827", "SOFT": "#6B7280", "LINE": "#E5E7EB",
        "AI": "#6366F1", "AI_TINT": "#EEF2FF", "SLATE_TINT": "#E5E7EB",
        "GREEN": "#4B5563", "GREEN_TINT": "#F3F4F6",
    },
    "blue": {  # أزرق احترافي
        "TEAL": "#2563EB", "TEAL_DARK": "#1D4ED8", "TINT": "#DBEAFE",
        "BG": "#F0F7FF", "PANEL": "#FFFFFF", "INK": "#1E3A5F", "SOFT": "#64748B", "LINE": "#BFDBFE",
        "AI": "#7C3AED", "AI_TINT": "#EDE9FE", "SLATE_TINT": "#EFF6FF",
        "GREEN": "#2563EB", "GREEN_TINT": "#DBEAFE",
    },
    "purple": {  # بنفسجي للمساعد
        "TEAL": "#7C3AED", "TEAL_DARK": "#6D28D9", "TINT": "#EDE9FE",
        "BG": "#FAF5FF", "PANEL": "#FFFFFF", "INK": "#2E1065", "SOFT": "#6B7280", "LINE": "#DDD6FE",
        "AI": "#EC4899", "AI_TINT": "#FCE7F3", "SLATE_TINT": "#F5F3FF",
        "GREEN": "#7C3AED", "GREEN_TINT": "#EDE9FE",
    },
}

THEME_LABELS = {
    "green": "أخضر (افتراضي)",
    "black": "أسود داكن",
    "gray": "رمادي هادئ",
    "blue": "أزرق احترافي",
    "purple": "بنفسجي",
}

_CURRENT_THEME = "green"


def apply_theme(name: str) -> str:
    """يطبّق ثيماً بالاسم ويحدّث المتغيرات العامة للألوان. يرجع الاسم المطبّق."""
    global TEAL, TEAL_DARK, TINT, BG, PANEL, INK, SOFT, LINE
    global AI, AI_TINT, SLATE_TINT, GREEN, GREEN_TINT, _CURRENT_THEME
    key = (name or "green").strip().lower()
    if key not in THEMES:
        key = "green"
    p = THEMES[key]
    TEAL, TEAL_DARK, TINT = p["TEAL"], p["TEAL_DARK"], p["TINT"]
    BG, PANEL, INK, SOFT, LINE = p["BG"], p["PANEL"], p["INK"], p["SOFT"], p["LINE"]
    AI, AI_TINT = p["AI"], p["AI_TINT"]
    SLATE_TINT = p["SLATE_TINT"]
    GREEN, GREEN_TINT = p["GREEN"], p["GREEN_TINT"]
    _CURRENT_THEME = key
    return key


def current_theme() -> str:
    return _CURRENT_THEME

# ---- سرعة الحركة (سلو موشن) ---------------------------------------------------------------------------
# كل المدد بالمللي ثانية. للتسريع/التبطيء كله دفعة واحدة: متغير البيئة DAFTARI_MOTION (1 = الافتراضي البطيء،
# 0.5 = أسرع بالضعف، 1.5 = أبطأ، 0.1 = شبه فوري).
try:
    MOTION_SCALE = max(0.05, float(os.getenv("DAFTARI_MOTION", "1")))
except ValueError:
    MOTION_SCALE = 1.0


def ms(base: int) -> int:
    return max(1, int(base * MOTION_SCALE))


NAV_FADE_MS = 120         # تلاشي القسم الجديد — AnimatedSwitcher duration
NAV_FADE_OUT_MS = 80      # تلاشي القسم القديم
NAV_SLIDE_MS = 120        # انزلاق محتوى القسم من الأسفل لمكانه
NAV_ITEM_MS = 120         # تلوّن عنصر القائمة النشط
ITEM_REVEAL_MS = 220      # ظهور كل بطاقة صنف (تلاشٍ + صعود)
ITEM_STEP_MS = 25         # الفاصل بين ظهور بطاقة والتي بعدها
ITEM_REVEAL_MAX = 8       # أقصى عدد بطاقات تتحرك (الباقي يظهر فوراً حتى لا يثقل الشاشة)
HOVER_MS = 140            # ارتفاع البطاقة عند مرور الماوس (Implicit Animation)


def _soft_shadow(blur: int = 14, y: int = 3) -> ft.BoxShadow:
    return ft.BoxShadow(spread_radius=0, blur_radius=blur, offset=ft.Offset(0, y),
                        color="#0F1F2937")      # أسود مزرق بشفافية ~6% — ظل خفيف ناعم


def border_all(width: float = 1, color: str = LINE) -> ft.Border:
    """إطار كامل — متوافق مع Flet 0.81 و 1.x (لا يعتمد على ft.border.all المحذوفة)."""
    s = ft.BorderSide(width, color)
    return ft.Border(s, s, s, s)


def border_side(*, top: bool = False, bottom: bool = False, left: bool = False,
                right: bool = False, width: float = 1, color: str = LINE) -> ft.Border:
    s = ft.BorderSide(width, color)
    return ft.Border(s if top else None, s if right else None,
                     s if bottom else None, s if left else None)


# ---- عناصر بسيطة ---------------------------------------------------------------------------------
def title(text: str, size: int = 20) -> ft.Text:
    return ft.Text(text, size=size, weight=ft.FontWeight.W_800, color=INK)


def muted(text: str, size: int = 13) -> ft.Text:
    return ft.Text(text, size=size, color=SOFT)


def btn(label: str, on_click: Callable | None = None, kind: str = "primary", disabled: bool = False,
        expand: bool | int | None = None, icon: str | None = None, small: bool = False) -> ft.Control:
    """kind: primary | danger | ghost | success | amber | ai — أزرار مستديرة عصرية. small = حجم مدمج للصفوف."""
    shape = ft.RoundedRectangleBorder(radius=10 if small else 12)
    pad = ft.Padding(14, 9, 14, 9) if small else ft.Padding(22, 15, 22, 15)
    dur = 160
    if kind == "ghost":      # رمادي نظيف: أبيض + إطار رمادي، وعند المرور يصير نعناعياً ناعماً
        kw = dict(shape=shape, side=ft.BorderSide(1, LINE), color=INK, bgcolor=PANEL, padding=pad)
        bg = _states(default=PANEL, hover="#F0FDF4", pressed=TINT)
        try:
            return ft.OutlinedButton(label, on_click=on_click, disabled=disabled, expand=expand, icon=icon,
                                     style=ft.ButtonStyle(**{**kw, **({"bgcolor": bg} if bg else {})},
                                                          animation_duration=dur))
        except Exception:  # noqa: BLE001 — إصدار لا يدعم الحالات: نستخدم الشكل الثابت
            return ft.OutlinedButton(label, on_click=on_click, disabled=disabled, expand=expand, icon=icon,
                                     style=ft.ButtonStyle(**kw))
    colors = {"primary": TEAL, "danger": RED, "success": GREEN, "amber": AMBER, "ai": AI}
    darker = {"primary": TEAL_DARK, "danger": "#B91C1C", "success": TEAL_DARK, "amber": "#92400E", "ai": "#5B4BB5"}
    base = colors.get(kind, TEAL)
    bg = _states(default=base, hover=darker.get(kind, TEAL_DARK), pressed=darker.get(kind, TEAL_DARK))
    try:
        style = ft.ButtonStyle(shape=shape, padding=pad, elevation=0, animation_duration=dur,
                               **({"bgcolor": bg} if bg else {}),
                               **({"overlay_color": _states(hover="#14FFFFFF", pressed="#26FFFFFF")} if bg else {}))
    except Exception:  # noqa: BLE001
        style = ft.ButtonStyle(shape=shape, padding=pad, elevation=0)
    return ft.Button(label, on_click=on_click, bgcolor=base, color="#FFFFFF", icon=icon, disabled=disabled,
                     expand=expand, style=style)


def field(label: str, value: Any = "", *, kind: str = "text", on_change: Callable | None = None,
          width: int | None = None, expand: bool | int | None = None, hint: str | None = None,
          autofocus: bool = False, on_submit: Callable | None = None) -> ft.TextField:
    """kind: text | num | int | password | multiline — حقل بإطار رفيع وزوايا مستديرة."""
    kb = {"num": ft.KeyboardType.NUMBER, "int": ft.KeyboardType.NUMBER}.get(kind)
    kw = dict(
        label=label, value="" if value is None else str(value), keyboard_type=kb, dense=True,
        password=kind == "password", can_reveal_password=kind == "password",
        multiline=kind == "multiline", min_lines=2 if kind == "multiline" else None,
        on_change=on_change, width=width, expand=expand, hint_text=hint, autofocus=autofocus,
        on_submit=on_submit, filled=True, bgcolor=PANEL, border_color=LINE, border_width=1,
        focused_border_color=TEAL, focused_border_width=1.6, border_radius=12,
        content_padding=ft.Padding(14, 14, 14, 14),
        label_style=ft.TextStyle(color=SOFT), text_style=ft.TextStyle(color=INK, size=14),
    )
    if kind in ("text", "multiline"):
        # لوحات المفاتيح (Gboard وغيرها) تعيد كتابة الكلمة وتبدّل مكان المؤشر عند خلط العربي والإنجليزي بسبب
        # التصحيح التلقائي والاقتراحات؛ تعطيلهما يثبّت النص كما يُكتب (أسماء الأصناف: «زيت DONG 10w40»).
        extra = dict(autocorrect=False, enable_suggestions=False, text_align=ft.TextAlign.START)
    else:
        extra = {}
    for attempt in (extra, {k: v for k, v in extra.items() if k == "text_align"}, {}):
        try:
            return ft.TextField(**kw, **attempt)
        except TypeError:        # إصدار Flet لا يعرف أحد هذه الوسائط: نجرّب الأقل ثم الأساسي
            continue
    return ft.TextField(**kw)


def card(content: ft.Control, bgcolor: str = PANEL, padding: int = 16, on_click: Callable | None = None) -> ft.Container:
    c = ft.Container(content, bgcolor=bgcolor, padding=padding, border_radius=RADIUS,
                     on_click=on_click, shadow=_soft_shadow(blur=16, y=4),
                     border=border_all(1, LINE), animate=_anim(300), animate_scale=_anim(300))
    return lift(press_feedback(c) if on_click else c, 1.012 if on_click else 1.008)


def banner(text: str, kind: str = "amber") -> ft.Container:
    bg, fg = {"amber": (AMBER_TINT, AMBER), "red": (RED_TINT, RED), "green": (GREEN_TINT, GREEN),
              "info": (TINT, TEAL)}[kind]
    return ft.Container(ft.Text(text, color=fg, size=13), bgcolor=bg, padding=12, border_radius=12)


def stat(label: str, value: str, color: str = INK) -> ft.Container:
    """بطاقة إحصائية صغيرة (تحافظ على التوافق مع التبويبات القديمة)."""
    c = ft.Container(
        ft.Column([muted(label), ft.Text(value, size=18, weight=ft.FontWeight.W_700, color=color)], spacing=2),
        bgcolor=PANEL, padding=14, border_radius=RADIUS, expand=True, border=border_all(1, LINE),
        shadow=_soft_shadow(10, 2), animate=_anim(300),
    )
    return lift(c, 1.01)


def stat_card(label: str, value: str, icon: str, *, accent: str = TEAL, tint: str = TINT,
              hint: str = "", on_click: Callable | None = None, width: float | None = None,
              expand: bool | int | None = None) -> ft.Container:
    """بطاقة إحصائية كبيرة على نمط لوحات التحكم الحديثة (أيقونة + عنوان + رقم ضخم)."""
    c = ft.Container(
        ft.Column([
            ft.Row([
                ft.Container(ft.Icon(icon, color=accent, size=20), bgcolor=tint, padding=10,
                             border_radius=12),
                ft.Container(expand=True),
            ]),
            ft.Text(label, size=12, color=SOFT),
            ft.Text(value, size=26, weight=ft.FontWeight.W_800, color=INK),
            ft.Text(hint, size=12, color=SOFT) if hint else ft.Container(height=0),
        ], spacing=6),
        bgcolor=PANEL, padding=18, border_radius=14, shadow=_soft_shadow(),
        border=border_all(1, LINE), on_click=on_click, width=width, expand=expand,
        animate=_anim(300), animate_scale=_anim(300),
    )
    return lift(press_feedback(c) if on_click else c)


def chip(text: str, color: str = TEAL, tint: str = TINT) -> ft.Container:
    """وسم صغير ملوّن (للحالات: مدفوعة / آجلة …)."""
    return ft.Container(ft.Text(text, size=11, weight=ft.FontWeight.W_600, color=color),
                        bgcolor=tint, padding=ft.Padding(10, 4, 10, 4), border_radius=20)


def empty(text: str) -> ft.Control:
    return ft.Container(
        ft.Column([ft.Icon(ft.Icons.INBOX_OUTLINED, color=LINE, size=44),
                   ft.Text(text, color=SOFT, text_align=ft.TextAlign.CENTER)],
                  horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
        padding=40, alignment=ft.Alignment(0, 0))


def row_between(*controls: ft.Control, wrap: bool = False) -> ft.Row:
    return ft.Row(list(controls), alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=wrap,
                  vertical_alignment=ft.CrossAxisAlignment.CENTER)


def section_header(text: str) -> ft.Control:
    """عنوان قسم صغير بأحرف متباعدة مثل لوحات التحكم الحديثة."""
    return ft.Text(text.upper() if text.isascii() else text, size=11, weight=ft.FontWeight.W_700,
                   color=SOFT)


# ---- إشعار + حوارات ----------------------------------------------------------------------------------
def toast(page: ft.Page, message: str, error: bool = False) -> None:
    page.show_dialog(ft.SnackBar(ft.Text(message), bgcolor=RED if error else TEAL_DARK, duration=3500))


async def _maybe_await(x: Any) -> Any:
    return await x if inspect.isawaitable(x) else x


def _dialog_style(d: ft.AlertDialog) -> ft.AlertDialog:
    d.shape = ft.RoundedRectangleBorder(radius=20)
    return d


def confirm_dialog(app, title_text: str, message: str, on_confirm: Callable[[], Awaitable | None],
                   confirm_label: str = "تأكيد", danger: bool = False) -> None:
    async def ok(_):
        app.page.pop_dialog()
        try:
            await _maybe_await(on_confirm())
        except LedgerError as e:
            toast(app.page, str(e), error=True)
        await app.after_change()

    def cancel(_):
        app.page.pop_dialog()

    app.page.show_dialog(_dialog_style(ft.AlertDialog(
        modal=True, title=ft.Text(title_text, weight=ft.FontWeight.W_700),
        content=ft.Text(message),
        actions=[ft.TextButton("إلغاء", on_click=cancel),
                 ft.Button(confirm_label, on_click=ok, bgcolor=RED if danger else TEAL, color="#FFFFFF",
                           style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)))],
    )))


def form_dialog(app, title_text: str, fields: list[tuple], on_submit: Callable[[dict], Awaitable | None],
                submit_label: str = "حفظ", subtitle: str = "", extra: list | None = None,
                suggest: dict[str, Callable[[str], tuple[list[str], str]]] | None = None) -> None:
    """fields: (key, label, value, kind). on_submit(values) يرفع LedgerError لإظهار رسالة والبقاء بالحوار.

    suggest: {مفتاح_الحقل: دالة(نص) → (قائمة أسماء مقترحة، سطر حالة)}: تُعرض الأسماء المقاربة كأزرار تحت الحقل أثناء الكتابة،
    والضغط على اسم يملأ الحقل به، وسطر الحالة يوضّح ما سيحدث (مثلاً: «سيُخصم من رصيد فلان»).
    """
    controls = {k: field(label, v, kind=kind) for k, label, v, kind in fields}
    err = ft.Text("", color=RED, size=12)
    sug_boxes: dict[str, ft.Column] = {}
    for key, fn in (suggest or {}).items():
        box = ft.Column([], spacing=4, tight=True)
        status = ft.Text("", size=12, color=SOFT)
        sug_boxes[key] = ft.Column([status, box], spacing=4, tight=True)

        def refresh(_=None, key=key, fn=fn, box=box, status=status):
            names, line = fn(controls[key].value or "")
            status.value = line

            def pick(_e, n=None):
                controls[key].value = n
                refresh()
                app.page.update()
            box.controls = [ft.OutlinedButton(n, on_click=lambda e, n=n: pick(e, n),
                                              style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)))
                            for n in names]
            try:
                app.page.update()
            except Exception:  # noqa: BLE001
                pass
        controls[key].on_change = refresh

    async def ok(_):
        try:
            await _maybe_await(on_submit({k: c.value for k, c in controls.items()}))
        except LedgerError as e:
            err.value = str(e)
            app.page.update()
            return
        app.page.pop_dialog()
        await app.after_change()

    def cancel(_):
        app.page.pop_dialog()

    ordered: list = []
    for k, c in controls.items():
        ordered.append(c)
        if k in sug_boxes:
            ordered.append(sug_boxes[k])
    body = ft.Column([*(muted(subtitle) for _ in [0] if subtitle), *(extra or []), *ordered, err],
                     tight=True, spacing=10, scroll=smooth_scroll(), width=360)
    app.page.show_dialog(_dialog_style(ft.AlertDialog(
        modal=True, title=ft.Text(title_text, weight=ft.FontWeight.W_700), content=body,
        actions=[ft.TextButton("إلغاء", on_click=cancel),
                 ft.Button(submit_label, on_click=ok, bgcolor=TEAL, color="#FFFFFF",
                           style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)))],
    )))


def info_dialog(app, title_text: str, content: ft.Control) -> None:
    def close(_):
        app.page.pop_dialog()

    app.page.show_dialog(_dialog_style(ft.AlertDialog(
        title=ft.Text(title_text, weight=ft.FontWeight.W_700),
        content=ft.Container(content, width=380),
        actions=[ft.TextButton("إغلاق", on_click=close)],
    )))


def choice_dialog(app, title_text: str, options: list[tuple[str, Callable[[], Awaitable | None]]],
                  message: str = "") -> None:
    """حوار بأزرار خيارات (مثل: مدفوعة / غير مدفوعة)."""
    def make(handler):
        async def go(_):
            app.page.pop_dialog()
            try:
                await _maybe_await(handler())
            except LedgerError as e:
                toast(app.page, str(e), error=True)
            await app.after_change()
        return go

    def close(_):
        app.page.pop_dialog()

    app.page.show_dialog(_dialog_style(ft.AlertDialog(
        title=ft.Text(title_text, weight=ft.FontWeight.W_700),
        content=ft.Column([*(ft.Text(message) for _ in [0] if message),
                           *[ft.Button(label, on_click=make(h), bgcolor=TEAL, color="#FFFFFF",
                                       style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)))
                             for label, h in options]],
                          tight=True, spacing=8),
        actions=[ft.TextButton("إلغاء", on_click=close)],
    )))


# ---- شريط بحث مع زر باركود (يُحدّث القائمة فقط، لا يعيد بناء الصفحة فيبقى التركيز) -------------------------
def search_bar(app, on_text: Callable[[str], None], placeholder: str = "بحث…", barcode: bool = True) -> ft.Row:
    # تأخير بسيط (debounce): لا نعيد رسم القائمة مع كل حرف، فالحقل لا يتجمّد ولا تضيع/تتبدّل الأحرف أثناء الكتابة السريعة
    tick = {"n": 0}

    async def fire(n: int, q: str) -> None:
        await asyncio.sleep(0.25)
        if n == tick["n"]:
            r = on_text(q)
            if asyncio.iscoroutine(r):
                await r

    def changed(e) -> None:
        tick["n"] += 1
        q = e.control.value
        try:
            app.page.run_task(fire, tick["n"], q)
        except Exception:  # noqa: BLE001 — لا يوجد run_task: ننفّذ مباشرة
            on_text(q)

    tf = field(placeholder, "", on_change=changed, expand=True)

    async def scan(_):
        code = await app.scan_barcode()
        if code:
            tf.value = code
            on_text(code)
            app.page.update()

    ctrls: list[ft.Control] = [tf]
    if barcode:
        ctrls.append(ft.Container(
            ft.IconButton(icon=ft.Icons.QR_CODE_SCANNER, icon_color=TEAL,
                          tooltip="مسح باركود من صورة", on_click=scan),
            bgcolor=TINT, border_radius=12))
    return ft.Row(ctrls, vertical_alignment=ft.CrossAxisAlignment.CENTER)


def _contain():
    """قيمة «احتواء الصورة كاملة» (الاسم تغيّر بين الإصدارات: BoxFit / ImageFit)."""
    for name in ("BoxFit", "ImageFit"):
        enum = getattr(ft, name, None)
        if enum is not None and hasattr(enum, "CONTAIN"):
            return enum.CONTAIN
    return None


def image_from_bytes(data: bytes, height: int | None = None, width: int | None = None, fit=None) -> ft.Control:
    """صورة من bytes. اسم الوسيط تغيّر بين إصدارات Flet فنجرّب الطرق المتاحة."""
    import base64
    b64 = base64.b64encode(data).decode()
    last: Exception | None = None
    for src in ({"src": data}, {"src_base64": b64}):
        for extra in (({"fit": fit} if fit is not None else {}), {}):
            try:
                return ft.Image(height=height, width=width, **src, **extra)
            except TypeError as e:
                last = e
    raise last  # type: ignore[misc]


# ======================================= مكوّنات التصميم الحديث (2026) =======================================
_AVATAR_PALETTE = [("#0B7A5B", "#E7F5EF"), ("#6D5BD0", "#EFEBFF"), ("#C26A0B", "#FEF3E2"),
                   ("#2563EB", "#E8F0FE"), ("#D64545", "#FDECEC"), ("#0E7490", "#E0F4F8")]


def initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "؟"
    return parts[0][0] + (parts[1][0] if len(parts) > 1 else "")


def avatar(name: str, size: int = 42) -> ft.Container:
    """دائرة بحرفَي الاسم الأولين بلون ثابت لكل اسم (مثل تطبيقات المحاسبة الحديثة)."""
    fg, bg = _AVATAR_PALETTE[sum(map(ord, name or "?")) % len(_AVATAR_PALETTE)]
    return ft.Container(ft.Text(initials(name), size=size * 0.36, weight=ft.FontWeight.W_700, color=fg),
                        width=size, height=size, bgcolor=bg, border_radius=size / 2,
                        alignment=ft.Alignment(0, 0))


_PILL = {"green": (GREEN, GREEN_TINT), "red": (RED, RED_TINT), "amber": (AMBER, AMBER_TINT),
         "gray": (SOFT, SLATE_TINT), "ai": (AI, AI_TINT), "gold": (GOLD, GOLD_TINT)}


def pill(text: str, kind: str = "gray", on_click: Callable | None = None) -> ft.Container:
    """وسم حالة مستدير: green | red | amber | gray | ai | gold."""
    fg, bg = _PILL.get(kind, _PILL["gray"])
    return ft.Container(ft.Text(text, size=11, weight=ft.FontWeight.W_700, color=fg), bgcolor=bg,
                        padding=ft.Padding(10, 4, 10, 4), border_radius=20, on_click=on_click)


def icon_tile(icon: str, color: str = TEAL, tint: str = TINT, size: int = 40) -> ft.Container:
    return ft.Container(ft.Icon(icon, color=color, size=size * 0.5), width=size, height=size, bgcolor=tint,
                        border_radius=size * 0.3, alignment=ft.Alignment(0, 0))


def page_header(title_text: str, subtitle: str = "", *actions: ft.Control) -> ft.Control:
    """ترويسة صفحة: عنوان كبير + سطر فرعي + أزرار إجراءات على الطرف."""
    left = ft.Column([title(title_text, 22), *([muted(subtitle, 13)] if subtitle else [])], spacing=2, expand=True)
    return ft.Row([left, *actions], vertical_alignment=ft.CrossAxisAlignment.CENTER, wrap=False)


def kpi(label: str, value: str, color: str = INK, icon: str | None = None, tint: str = SLATE_TINT,
        hint: str = "") -> ft.Container:
    """بطاقة مؤشر مدمجة (للشرائط العلوية في الأقسام)."""
    head = ft.Row([*([icon_tile(icon, color, tint, 34)] if icon else []),
                   ft.Column([muted(label, 12), ft.Text(value, size=19, weight=ft.FontWeight.W_800, color=color),
                              *([muted(hint, 11)] if hint else [])], spacing=1, expand=True)],
                  spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    return ft.Container(head, bgcolor=PANEL, padding=14, border_radius=RADIUS, border=border_all(1, LINE),
                        shadow=_soft_shadow(10, 2), expand=True, animate=_anim(300))


def table_header(*cols: tuple[str, int]) -> ft.Control:
    """رأس جدول: (عنوان، وزن العرض). الوزن 0 = عمود فارغ بعرض ثابت."""
    return ft.Container(
        ft.Row([ft.Text(lbl, size=11, weight=ft.FontWeight.W_700, color=SOFT, expand=wt or None,
                        width=None if wt else 120) for lbl, wt in cols], spacing=10),
        bgcolor=SLATE_TINT, padding=ft.Padding(14, 9, 14, 9), border_radius=10)


def row_card(content: ft.Control, on_click: Callable | None = None, bgcolor: str = PANEL) -> ft.Container:
    """صف قائمة بطاقي خفيف (بلا ظل ثقيل) — للجداول والقوائم الطويلة."""
    c = ft.Container(content, bgcolor=bgcolor, padding=ft.Padding(14, 12, 14, 12), border_radius=12,
                     border=border_all(1, LINE), on_click=on_click, animate=_anim(300), animate_scale=_anim(300))
    return lift(press_feedback(c) if on_click else c, 1.006)


def money_text(value: str, color: str = INK, size: int = 14, bold: bool = True) -> ft.Text:
    return ft.Text(value, size=size, color=color, weight=ft.FontWeight.W_700 if bold else ft.FontWeight.NORMAL)


# ======================================= صور الأصناف + بطاقة الصنف (تصميم الشبكة) =======================================
TILE_THUMB = 84      # صورة بطاقة الصنف في الفاتورة (كانت 54)


def thumb(app, item, size: int = 56, zoom: bool = False) -> ft.Control:
    """صورة مصغّرة للصنف: من الكاش المحلي إن وُجدت، وإلا أيقونة هادئة (لا تنتظر الشبكة أبداً).

    zoom=True: الضغط على الصورة يفتح عرضاً كبيراً لها (show_image)."""
    try:
        data = app.images.get_local(item.id)
    except Exception:  # noqa: BLE001
        data = None
    if data:
        box = ft.Container(image_from_bytes(data, height=size, width=size, fit=_contain()), width=size, height=size,
                           border_radius=size * 0.22, bgcolor="#FFFFFF", clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                           border=border_all(1, LINE))
    else:
        box = icon_tile(ft.Icons.INVENTORY_2_OUTLINED, SOFT, SLATE_TINT, size)
    if zoom:
        box.on_click = lambda _: show_image(app, item)
        box.tooltip = "اضغط لعرض صورة المنتج"
        box.ink = True
    return box


def show_image(app, item) -> None:
    """نافذة تعرض صورة الصنف بحجم كبير (الصورة المخزّنة بحدود 320px، وتُعرض كاملة دون قص)."""
    try:
        data = app.images.get_local(item.id)
    except Exception:  # noqa: BLE001
        data = None
    try:
        data = app.images.get_best_local(item.id) or data
    except Exception:  # noqa: BLE001
        pass
    if not data:
        toast(app.page, f"لا توجد صورة لـ «{item.name}» — أضفها من «تعديل» في المخزون")
        return
    body = ft.Column([
        ft.Container(image_from_bytes(data, width=320, height=320, fit=_contain()), bgcolor="#FFFFFF", padding=6,
                     border_radius=16, border=border_all(1, LINE), alignment=ft.Alignment(0, 0)),
        ft.Text(item.name, size=15, weight=ft.FontWeight.W_700, color=INK, text_align=ft.TextAlign.CENTER),
        muted(f"الكود: {item.code or '—'} · المخزون: {item.stock}", 12),
    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8, tight=True)
    info_dialog(app, "صورة المنتج", body)


def item_tile(app, item, *, width: int, big: str, on_click: Callable | None = None, badge: ft.Control | None = None,
              selected: bool = False, dim: bool = False, actions: ft.Control | None = None) -> ft.Container:
    """بطاقة صنف على شكل الشبكة المطلوبة: الصورة بجانب الاسم، الكود تحته، والرقم الأخضر الكبير أسفل البطاقة."""
    head = ft.Column([
        ft.Row([thumb(app, item, TILE_THUMB)], alignment=ft.MainAxisAlignment.CENTER),
        ft.Text(item.name, size=14, weight=ft.FontWeight.W_700, color=INK, max_lines=2,
                overflow=ft.TextOverflow.ELLIPSIS, text_align=ft.TextAlign.RIGHT),
        ft.Text(item.code or "—", size=12, color=SOFT, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
    ], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.END)
    foot = ft.Row([*([badge] if badge else []), ft.Container(expand=True),
                   ft.Text(big, size=20, weight=ft.FontWeight.W_800, color=TEAL)],
                  vertical_alignment=ft.CrossAxisAlignment.CENTER)
    c = ft.Container(
        ft.Column([head, foot, *([actions] if actions else [])], spacing=8),
        width=width, bgcolor=PANEL if not dim else SLATE_TINT, padding=12, border_radius=14,
        shadow=_soft_shadow(10, 2) if not dim else None,
        border=border_all(2 if selected else 1, TEAL if selected else LINE), ink=on_click is not None and not dim,
        opacity=0.55 if dim else 1, on_click=None if dim else on_click,
        animate=_anim(300), animate_scale=_anim(300))
    return lift(press_feedback(c), 1.02) if on_click is not None and not dim else c


def image_picker(app, state: dict, current_item=None) -> ft.Control:
    """صف «صورة الصنف»: معاينة + زر اختيار. الصورة تُضغط تلقائياً إلى ~10KB وتوضع في state['jpeg'].

    الاستخدام داخل form_dialog(extra=[w.image_picker(app, state)]) ثم بعد الحفظ: await w.save_picked_image(app, state, item).
    """
    from ..data.images import ImageError, prepare_product_image
    preview = ft.Container(width=72, height=72, border_radius=16, bgcolor=SLATE_TINT, alignment=ft.Alignment(0, 0),
                           content=thumb(app, current_item, 72) if current_item is not None
                           else ft.Icon(ft.Icons.ADD_PHOTO_ALTERNATE_OUTLINED, color=SOFT, size=28))
    info = muted("اختر صورة — ستُصغَّر تلقائياً إلى حوالي 10 كيلوبايت", 12)

    async def pick(_):
        got = await app.pick_file_bytes(images=True)
        if not got:
            return
        try:
            jpeg, full = await asyncio.to_thread(prepare_product_image, got[1])
        except ImageError as e:
            toast(app.page, str(e), error=True)
            return
        state["jpeg"], state["full"] = jpeg, full
        preview.content = image_from_bytes(full, height=72, width=72)
        info.value = (f"تم تجهيز الصورة: تُرفع بحجم {len(jpeg) / 1024:.1f} كيلوبايت "
                      f"وتبقى بالجودة الكاملة على هذا الجهاز")
        app.page.update()

    return ft.Row([preview, ft.Column([btn("🖼 اختيار صورة", pick, "ghost", small=True), info], spacing=4, expand=True)],
                  spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)


async def save_picked_image(app, state: dict, item_id: str) -> None:
    """بعد حفظ الصنف: يخزّن الصورة المختارة (إن وُجدت) ويحدّث بصمتها في سجل الصنف."""
    jpeg = state.get("jpeg")
    if not jpeg:
        return
    from ..data.images import fingerprint
    await app.images.put(item_id, jpeg, full=state.get("full"))
    await app.ledger.set_item_image(item_id, fingerprint(jpeg))


# ======================================= حركة وتفاعل (Hover / Tap) =======================================
def _anim(ms: int = 180, curve: str = "EASE_OUT"):
    """كائن حركة بمنحنى ناعم؛ إن لم يدعمه الإصدار نرجع الرقم (مللي ثانية) فقط."""
    try:
        return ft.Animation(ms, getattr(ft.AnimationCurve, curve, ft.AnimationCurve.EASE_OUT))
    except Exception:  # noqa: BLE001
        return ms


def smooth_scroll() -> ft.ScrollMode:
    """وضع التمرير الأكثر سلاسة المتاح في إصدار Flet الحالي.
    يفضّل ADAPTIVE (فيزياء منصة أصلية ناعمة) ثم AUTO."""
    adaptive = getattr(ft.ScrollMode, "ADAPTIVE", None)
    if adaptive is not None:
        return adaptive
    return ft.ScrollMode.AUTO


def lift(c: ft.Container, scale: float = 1.012, hover_shadow: ft.BoxShadow | None = None) -> ft.Container:
    """يضيف للبطاقة ارتفاعاً ناعماً عند مرور الماوس: تكبير بسيط + ظل أوضح + إطار أخضر خفيف، ثم يعود بسلاسة."""
    base_shadow, base_border = c.shadow, c.border
    hov = hover_shadow or ft.BoxShadow(spread_radius=0, blur_radius=22, offset=ft.Offset(0, 8), color="#1A1F2937")
    try:
        c.animate = _anim(ms(HOVER_MS))
        c.animate_scale = _anim(ms(HOVER_MS))
    except Exception:  # noqa: BLE001
        return c
    prev = getattr(c, "on_hover", None)

    def on_hover(e):
        on = str(getattr(e, "data", "")).lower() in ("true", "1")
        c.scale = (scale if scale >= 1.03 else 1) if on else 1   # البطاقات الكبيرة: بلا تكبير (يسبّب اهتزازاً أثناء التمرير)
        c.shadow = hov if on else base_shadow
        c.border = border_all(1, "#A7F3D0") if on else base_border
        try:
            c.update()
        except Exception:  # noqa: BLE001
            pass
        if prev:
            prev(e)
    c.on_hover = on_hover
    return c


def press_feedback(c: ft.Container) -> ft.Container:
    """ضغطة: ink ripple + انكماش لحظي صغير (يُستدعى بعد lift على العناصر القابلة للنقر)."""
    c.ink = True
    return c


def after_paint(page: ft.Page, fn: Callable, delay: float = 0.08) -> None:
    """ينفّذ fn (عادية أو async) بعد أن تُرسم الشاشة فعلاً (تأخير قصير) — أساس كل حركات الدخول."""
    async def run():
        await asyncio.sleep(delay)
        try:
            await _maybe_await(fn())
        except Exception as e:  # noqa: BLE001 — الحركة تجميلية؛ لا تُسقط البرنامج
            print("after_paint:", e)
    page.run_task(run)


def reveal(app, controls: list, *, force: bool = False, start: int = 0, step_ms: int | None = None,
           dur_ms: int | None = None, rise: float = 0.05) -> None:
    """يُظهر بطاقات الأصناف واحدة تلو الأخرى ببطء (تلاشٍ + صعود ناعم) — الحركة البطيئة بين الأصناف.

    - لا تعمل إلا عند دخول القسم (app.entering) أو عند force=True (بحث / «عرض المزيد»)، فلا تتكرر مع كل إضافة للسلة.
    - start: ابدأ من البطاقة رقم كذا (لإظهار الجديدة فقط عند «عرض المزيد»).
    - أول ITEM_REVEAL_MAX بطاقة فقط تتحرك والباقي يظهر فوراً (حفاظاً على الأداء)."""
    if not force and not getattr(app, "entering", True):
        return
    targets = [c for c in controls[start:start + ITEM_REVEAL_MAX] if isinstance(c, ft.Container)]
    if not targets:
        return
    keep: list[float] = []
    try:
        for c in targets:
            keep.append(1 if c.opacity is None else c.opacity)
            c.opacity = 0
            c.offset = ft.Offset(0, rise)
            c.animate_opacity = _anim(ms(dur_ms or ITEM_REVEAL_MS), "EASE_IN_OUT")
            c.animate_offset = _anim(ms(dur_ms or ITEM_REVEAL_MS), "EASE_OUT_CUBIC")
    except Exception:  # noqa: BLE001 — إصدار لا يدعم الإزاحة/الشفافية المتحركة: نعرضها دون حركة
        for c, o in zip(targets, keep):
            c.opacity = o
        return
    gap = ms(step_ms or ITEM_STEP_MS) / 1000

    async def play():
        for c, o in zip(targets, keep):
            try:
                c.opacity = o
                c.offset = ft.Offset(0, 0)
                c.update()
            except Exception:  # noqa: BLE001 — غادر المستخدم الشاشة؛ نتوقف بهدوء
                return
            await asyncio.sleep(gap)
    after_paint(app.page, play, 0.12)


def _states(**kw):
    """قاموس حالات الزر (hover/pressed) بحسب اسم الفئة في إصدار Flet المثبّت."""
    cs = getattr(ft, "ControlState", None) or getattr(ft, "MaterialState", None)
    if cs is None:
        return None
    m = {"hover": "HOVERED", "pressed": "PRESSED", "default": "DEFAULT"}
    try:
        return {getattr(cs, m[k]): v for k, v in kw.items()}
    except AttributeError:
        return None



# ======================================= توافق Flet: إنشاء عناصر بأمان =======================================
def safe_ctl(cls, **kw):
    """ينشئ عنصر Flet ويتكيّف مع اختلاف الإصدارات: إن رفض وسيطاً (TypeError) نجرّب بديله أو نحذفه ونعيد المحاولة.
    مثال: Dropdown في Flet الحديث يستخدم on_select بدل on_change."""
    alias = {"on_change": "on_select", "on_select": "on_change"}
    for _ in range(14):
        try:
            return cls(**kw)
        except TypeError as e:
            import re
            m = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if not m:
                raise
            bad = m.group(1)
            val = kw.pop(bad, None)
            nxt = alias.get(bad)
            if nxt and nxt not in kw and val is not None:
                kw[nxt] = val
    return cls(**kw)


def dropdown_option(key: str, label: str):
    """خيار قائمة منسدلة بحسب إصدار Flet (DropdownOption الحديث ثم dropdown.Option القديم)."""
    cls = getattr(ft, "DropdownOption", None) or getattr(getattr(ft, "dropdown", None), "Option", None)
    if cls is None:
        return key
    for kw in ({"key": key, "text": label}, {"key": key, "content": label}, {"key": key}):
        try:
            return cls(**kw)
        except TypeError:
            continue
    return cls(key)


def dropdown(label: str, value: str, options: list[tuple[str, str]], on_pick: Callable, **kw) -> ft.Control:
    """قائمة منسدلة تعمل على كل إصدارات Flet. on_pick(value) دالة عادية أو async."""
    async def handler(e):
        await _maybe_await(on_pick(e.control.value))
    base = dict(label=label, value=value, options=[dropdown_option(k, v) for k, v in options],
                on_change=handler, border_color=LINE, focused_border_color=TEAL, bgcolor=PANEL, color=INK,
                border_radius=12, content_padding=12, expand=True)
    base.update(kw)
    return safe_ctl(ft.Dropdown, **base)


# ======================================= تنقّل سلس بين الأصناف (أعلى ↔ أسفل) =======================================
SCROLL_STEP_PX = 460        # خطوة السهم الواحد (تقريباً شاشة إلا قليلاً)
SCROLL_MS = 350             # مدة الانزلاق — تُضرب بـ DAFTARI_MOTION


async def smooth_scroll_to(col: ft.Column, *, offset: float | None = None, delta: float | None = None) -> None:
    """انزلاق ناعم بمنحنى بطيء الطرفين؛ إن لم يدعم الإصدار duration/curve ننتقل مباشرة."""
    curve = getattr(ft.AnimationCurve, "EASE_IN_OUT_CUBIC", None) or getattr(ft.AnimationCurve, "EASE_IN_OUT", None)
    attempts = (dict(duration=ms(SCROLL_MS), curve=curve), dict(duration=ms(SCROLL_MS)), {})
    for extra in attempts:
        try:
            args = {k: v for k, v in (("offset", offset), ("delta", delta)) if v is not None}
            await _maybe_await(col.scroll_to(**args, **extra))
            return
        except TypeError:
            continue
        except Exception as e:  # noqa: BLE001 — العمود لم يُعرض بعد
            print("scroll_to:", e)
            return


def with_scroll_nav(col: ft.Column) -> ft.Control:
    """يلفّ عموداً قابلاً للتمرير بأزرار عائمة: أعلى · لأعلى · لأسفل · أسفل. الضغط المتتالي لا يُدخل اهتزازاً
    (نتجاهل الضغطات أثناء الحركة الجارية حتى تنتهي)."""
    lock = {"busy": False}

    def go(offset=None, delta=None):
        async def click(_):
            if lock["busy"]:
                return
            lock["busy"] = True
            try:
                await smooth_scroll_to(col, offset=offset, delta=delta)
                await asyncio.sleep(ms(SCROLL_MS) / 1000)
            finally:
                lock["busy"] = False
        return click

    def b(icon, tip, handler):
        return ft.IconButton(icon=icon, icon_size=22, icon_color=TEAL, tooltip=tip, on_click=handler,
                             width=40, height=40)

    pad = ft.Container(
        ft.Column([b(ft.Icons.KEYBOARD_DOUBLE_ARROW_UP_ROUNDED, "إلى أول القائمة", go(offset=0)),
                   b(ft.Icons.KEYBOARD_ARROW_UP_ROUNDED, "للأعلى", go(delta=-SCROLL_STEP_PX)),
                   b(ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED, "للأسفل", go(delta=SCROLL_STEP_PX)),
                   b(ft.Icons.KEYBOARD_DOUBLE_ARROW_DOWN_ROUNDED, "إلى آخر القائمة", go(offset=-1))],
                  spacing=0, tight=True),
        bgcolor=PANEL, border_radius=22, border=border_all(1, LINE), shadow=_soft_shadow(16, 4),
        padding=ft.Padding(2, 4, 2, 4), left=8, bottom=20, opacity=0.94)
    return ft.Stack([col, pad], expand=True)


async def open_url(page, url: str) -> bool:
    """يفتح رابطاً في المتصفح الخارجي بأي إصدار من Flet (launch_url قد تكون متزامنة أو async). يرجع نجاح الفتح."""
    import inspect
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return False
    try:
        res = page.launch_url(url)
        if inspect.isawaitable(res):
            await res
        return True
    except Exception as e:  # noqa: BLE001
        try:
            res = ft.UrlLauncher().launch_url(url)       # Flet >= 0.80 (خدمة)
            if inspect.isawaitable(res):
                await res
            return True
        except Exception:  # noqa: BLE001
            print("open_url:", e)
            return False


# ======================================= ربط صورة بصنف من المخزون =======================================
def pick_item_dialog(app, title_text: str, on_pick: Callable[[Any], Awaitable | None], hint: str = "") -> None:
    """حوار بحث في المخزون (بالاسم/الكود) ويرجع الصنف المختار إلى on_pick(item)."""
    from ..core.search import search_inventory
    results = ft.Column([], spacing=6, scroll=ft.ScrollMode.AUTO, height=300)

    def fill(q: str = "") -> None:
        found = search_inventory(app.ledger.inventory, q)[:12] if (q or "").strip() else list(app.ledger.inventory)[:12]
        rows = []
        for it in found:
            async def choose(_, it=it):
                app.page.pop_dialog()
                await _maybe_await(on_pick(it))
            has_img = bool(app.images.get_local(it.id))
            rows.append(ft.Container(
                ft.Row([thumb(app, it, 40),
                        ft.Column([ft.Text(it.name, size=13, weight=ft.FontWeight.W_600, color=INK, max_lines=2,
                                           overflow=ft.TextOverflow.ELLIPSIS),
                                   muted(f"{it.code or '—'} · المخزون {it.stock}" + (" · له صورة" if has_img else ""), 11)],
                                  spacing=2, expand=True)], spacing=10),
                padding=ft.Padding(8, 6, 8, 6), border_radius=12, border=border_all(1, LINE), bgcolor=PANEL,
                on_click=choose, ink=True))
        results.controls = rows or [empty("لا توجد أصناف مطابقة")]

    def changed(e):
        fill(getattr(e.control, "value", "") or "")
        app.page.update()

    search_tf = field("ابحث عن الصنف (اسم أو كود)", "", on_change=changed, autofocus=True)
    fill("")
    body = ft.Column([*([muted(hint, 12)] if hint else []), search_tf, results], spacing=8, tight=True)

    def close(_):
        app.page.pop_dialog()

    app.page.show_dialog(_dialog_style(ft.AlertDialog(
        title=ft.Text(title_text, weight=ft.FontWeight.W_700), content=ft.Container(body, width=380),
        actions=[ft.TextButton("إلغاء", on_click=close)])))


async def attach_image_to_item(app, item, data: bytes) -> bool:
    """يضع data (أي صورة) كصورة الصنف: يضغطها كبقية صور المخزون، يحفظها محلياً/سحابياً، ويحدّث بصمتها. يرجع النجاح."""
    from ..data.images import ImageError as _ImgErr, prepare_product_image, fingerprint
    try:
        jpeg, full = await asyncio.to_thread(prepare_product_image, data)
    except _ImgErr as e:
        toast(app.page, str(e), error=True)
        return False
    try:
        await app.images.put(item.id, jpeg, full=full)
        await app.ledger.set_item_image(item.id, fingerprint(jpeg))
    except Exception as e:  # noqa: BLE001
        toast(app.page, f"تعذّر حفظ الصورة للصنف: {e}", error=True)
        return False
    toast(app.page, f"تمت إضافة الصورة للصنف «{item.name}»")
    return True


def link_image_to_item(app, data: bytes, label: str = "") -> None:
    """زر «ربط بصنف»: يختار المستخدم الصنف؛ إن كان له صورة نسأله قبل الاستبدال."""
    async def picked(item):
        async def apply():
            if await attach_image_to_item(app, item, data):
                try:
                    await app.after_change()
                except Exception:  # noqa: BLE001
                    pass
        if app.images.get_local(item.id):
            confirm_dialog(app, "استبدال صورة الصنف؟", f"«{item.name}» له صورة حالية، ستُستبدل بصورة الكتالوج.", apply, "استبدال")
        else:
            await apply()
    pick_item_dialog(app, "اختر الصنف لإضافة الصورة له", picked,
                     hint=f"الصورة: {label}" if label else "")


class LazyList:
    """قائمة طويلة كسولة (ListView): الترويسة + عناصر القائمة أبناء مباشرون لـ ListView واحد، فيبني Flutter ما يظهر على الشاشة فقط
    ويعيد تدوير الباقي — بدل Column يبني مئات البطاقات دفعة واحدة (سبب التقطيع والثقل).
    الاستعمال: lazy.controls = [...] (أو append) ثم lazy.sync()؛ و lazy.view هو العنصر الذي يوضع في الصفحة."""

    def __init__(self, fixed: list | None = None, spacing: int = 10):
        self.fixed: list = fixed if fixed is not None else []
        self.controls: list = []
        self.view = ft.ListView(controls=[], expand=True, spacing=spacing, padding=0)

    def sync(self) -> None:
        self.view.controls = [*self.fixed, *self.controls]
