"""مساعد شركة العمر — دردشة مع Gemini مربوطة بالمخزون والفواتير والعملاء وقاعدة Supabase.

- يرى كل البيانات عبر أدوات القراءة، ويكتب فقط عبر Ledger بعد موافقتك (إلا مع «التنفيذ المباشر»).
- الإعدادات من .env: GEMINI_API_KEY · GEMINI_MODEL · GEMINI_THINKING · GEMINI_MAX_OUTPUT_TOKENS.
- يدعم رفع صور (قطع غيار/فواتير) والبحث في جوجل والإدخال الصوتي.
"""
from __future__ import annotations

import asyncio
import inspect

import flet as ft

from ...ai import chat_export as ce
from ...ai import excel
from ...ai.agent import Assistant
from ...ai.gemini import GeminiClient, GeminiConfig, GeminiError
from ...ai.llm import build_client, load_specs
from ...ai.tools import ToolBox
from ...ai.voice import MAX_SECONDS, VoiceError
from ...data.images import ImageError
from .. import widgets as w

SUGGESTIONS = [
    "ملخص أداء المتجر اليوم",
    "ما الأصناف التي قاربت على النفاد؟",
    "من هم العملاء المدينون وكم عليهم؟",
    "أكثر الأصناف مبيعاً وأرباحاً",
    "أصناف سعر بيعها أقل من تكلفتها",
    "صدّر لي المخزون إلى ملف Excel",
    "أرني صورة صنف من المخزون",
    "أنشئ فاتورة بيع لزبون",
    "أصدر سند قبض أو سند دفع",
]

TOOL_LABELS = {
    "business_summary": "أجمع ملخص المتجر", "search_items": "أبحث في المخزون", "list_low_stock": "أفحص المخزون المنخفض",
    "list_customers": "أراجع العملاء", "get_customer_statement": "أفتح كشف الحساب", "list_invoices": "أقرأ الفواتير",
    "get_invoice": "أفتح الفاتورة", "list_purchases": "أقرأ فواتير الشراء", "list_suppliers": "أراجع الموردين",
    "list_expenses": "أقرأ المصاريف", "list_vouchers": "أقرأ السندات", "best_sellers": "أحسب الأكثر مبيعاً",
    "profit_overview": "أحلل الأرباح", "list_data_keys": "أستعرض قاعدة البيانات", "read_data_key": "أقرأ من قاعدة البيانات",
    "list_project_files": "أستعرض ملفات البرنامج", "read_project_file": "أقرأ كود البرنامج", "save_proposal": "أكتب الاقتراح",
    "add_item": "أضيف صنفاً", "edit_item": "أعدّل الصنف", "edit_item_prices": "أعدّل الأسعار", "restock_item": "أضيف كمية",
    "add_customer": "أضيف زبوناً", "record_customer_payment": "أسجّل سند القبض", "add_expense": "أسجّل المصروف",
    "export_excel": "أجهّز ملف Excel", "export_dataset": "أصدّر البيانات إلى Excel",
    "show_item_images": "أجلب صور الأصناف",
    "create_invoice": "أنشئ الفاتورة", "create_voucher": "أصدر السند",
    "google_search": "أبحث في جوجل", "googleSearch": "أبحث في جوجل",
    "search_catalogs": "أبحث في كتالوجات الفلاتر",
    "open_catalog": "أجهّز رابط الكتالوج",
    "read_webpage": "أقرأ صفحة الويب",
    "attach_catalog_image": "أضيف الصورة للصنف",
    "clean_image_background": "أنظّف خلفية الصورة",
    "attach_user_image": "أضيف الصورة للصنف",
    "read_excel_rows": "أقرأ ملف Excel",
    "import_excel_items": "أسجّل أصناف الملف",
    "fill_missing_images": "أجلب صور الأصناف الناقصة",
}


def _state(app) -> dict:
    st = app.ai_state
    if not st:
        st.update(messages=[], busy=False, auto=False, assistant=None, cfg=GeminiConfig.from_env(),
                  pending=[], listening=False)
    st.setdefault("pending", [])
    st.setdefault("listening", False)
    st.setdefault("stamps", [])
    st.setdefault("reply_images", {})          # فهرس رد المساعد ← [(تعليق، JPEG)] صور رافقت الرد (للطباعة/الإكسل)
    return st


MAX_REPLY_IMAGES = 100          # صور الرد الواحد التي تخرج مع الطباعة/الإكسل (1…100)
THUMB_SIDE = 220                # مصغّرات المعرض في الشاشة (الأصل يبقى للتكبير والتصدير)
_thumb_cache: dict[int, bytes] = {}


def _thumb_bytes(data: bytes) -> bytes:
    """مصغّر خفيف للمعرض: مئة صورة بحجمها الكامل تُثقل الشاشة. نحتفظ بالأصل للتكبير والتصدير."""
    key = hash(data)
    got = _thumb_cache.get(key)
    if got is not None:
        return got
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((THUMB_SIDE, THUMB_SIDE))
        b = io.BytesIO()
        im.save(b, "JPEG", quality=72)
        got = b.getvalue()
    except Exception:  # noqa: BLE001
        got = data
    if len(_thumb_cache) > 400:
        _thumb_cache.clear()
    _thumb_cache[key] = got
    return got


def _add(st: dict, role: str, content) -> None:
    """يضيف رسالة للمحادثة مع وقتها (للطباعة والتصدير). الطوابع موازية للرسائل بالفهرس."""
    msgs, stamps = st["messages"], st.setdefault("stamps", [])
    del stamps[len(msgs):]                          # الرسائل أُفرغت/استُبدلت من الخارج
    stamps.extend([""] * (len(msgs) - len(stamps)))
    msgs.append((role, content))
    stamps.append(ce.now_stamp())


def _stamp(st: dict, i: int) -> str:
    stamps = st.get("stamps") or []
    return stamps[i] if i < len(stamps) else ""


def _doc_title(app, ref: str) -> str:
    kind, _, ident = ref.partition(":")
    led = app.ledger
    rec = next((x for x in (led.invoices if kind == "invoice" else led.vouchers) if x.id == ident), None)
    if rec is None:
        return ref
    return ("فاتورة بيع " if kind == "invoice" else ("سند قبض " if rec.type == "receipt" else "سند دفع ")) + rec.number


def _entries(app, st: dict, messages=None, stamps=None) -> list:
    return ce.build_entries(st["messages"] if messages is None else messages,
                            st.get("stamps") if stamps is None else stamps,
                            doc_label=lambda ref: _doc_title(app, ref), image_of=app.images.get_local)


def _confirm_factory(app):
    async def confirm(message: str) -> bool:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def decide(ok: bool):
            def handler(_):
                app.page.pop_dialog()
                if not fut.done():
                    fut.set_result(ok)
            return handler

        app.page.show_dialog(ft.AlertDialog(
            modal=True, shape=ft.RoundedRectangleBorder(radius=20),
            title=ft.Row([w.icon_tile(ft.Icons.AUTO_AWESOME, w.AI, w.AI_TINT, 34),
                          ft.Text("المساعد يطلب موافقتك", weight=ft.FontWeight.W_700)], spacing=10),
            content=ft.Column([ft.Text(message, size=14),
                               w.muted("تؤخذ نسخة احتياطية تلقائياً قبل التنفيذ.")], tight=True, spacing=8, width=360),
            actions=[ft.TextButton("رفض", on_click=decide(False)),
                     ft.Button("موافقة وتنفيذ", on_click=decide(True), bgcolor=w.AI, color="#FFFFFF",
                               style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)))]))
        return await fut
    return confirm


def _get_assistant(app) -> Assistant:
    st = _state(app)
    if st["assistant"] is None:
        tools = ToolBox(app.ledger, app.store, app.home, _confirm_factory(app), lambda: st["auto"], images=app.images)
        client = build_client(load_specs(app.ledger.settings))
        tools.search_provider = client.grounded_search        # بحث جوجل الرسمي عبر Gemini (لا يتأثر بحجب المحركات)
        st["assistant"] = Assistant(client, tools, app.ledger.settings.get("businessName", "المتجر"))
    return st["assistant"]


def _file_card(app, path: str) -> ft.Control:
    """بطاقة ملف Excel أنشأه المساعد: الاسم والحجم وزرّا فتح وحفظ باسم."""
    from pathlib import Path
    p = Path(path)
    size = f"{p.stat().st_size / 1024:.1f} كيلوبايت" if p.exists() else ""

    async def save_it(_):
        try:
            data = await asyncio.to_thread(p.read_bytes)
        except OSError as e:
            w.toast(app.page, f"تعذّر قراءة الملف: {e}", error=True)
            return
        await app.save_with_toast(p.name, data, "الملف")

    async def open_it(_):
        if app.is_web():                    # الفتح يجري على الخادم لا على جهازك → ننزّله عبر المتصفح
            await save_it(_)
        elif not app.open_file(p):
            w.toast(app.page, f"تعذّر الفتح تلقائياً — استخدم «حفظ باسم…». الملف هنا: {p}", error=True)

    return ft.Row([ft.Container(
        ft.Row([w.icon_tile(ft.Icons.TABLE_CHART_OUTLINED, w.TEAL, w.TINT, 44),
                ft.Column([ft.Text(p.name, size=14, weight=ft.FontWeight.W_700, color=w.INK, max_lines=1,
                                   overflow=ft.TextOverflow.ELLIPSIS),
                           w.muted(f"ملف Excel · {size}", 12)], spacing=1, expand=True),
                w.btn("تنزيل" if app.is_web() else "فتح", open_it, "primary", small=True,
                      icon=ft.Icons.DOWNLOAD if app.is_web() else ft.Icons.OPEN_IN_NEW),
                w.btn("حفظ باسم…", save_it, "ghost", small=True, icon=ft.Icons.SAVE_ALT)],
               spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=w.PANEL, padding=12, border_radius=14, border=w.border_all(1, "#A7F3D0"),
        shadow=w._soft_shadow(10, 2), expand=True, animate=w._anim(300))],
        vertical_alignment=ft.CrossAxisAlignment.START)


def _doc_card(app, ref: str) -> ft.Control:
    """بطاقة مستند أنشأه المساعد (فاتورة/سند): زر لعرضه وطباعته أو حفظه PDF — ref = «invoice:<id>» أو «voucher:<id>»."""
    from ...printing.render import invoice_doc, voucher_doc
    kind, _, ident = ref.partition(":")
    led = app.ledger
    rec = next((x for x in (led.invoices if kind == "invoice" else led.vouchers) if x.id == ident), None)
    if rec is None:
        return w.muted("المستند غير موجود (ربما حُذف).")
    label = ("فاتورة بيع " if kind == "invoice" else ("سند قبض " if rec.type == "receipt" else "سند دفع ")) + rec.number

    async def view(_):
        doc = invoice_doc(rec, led.settings, app.line_images(rec)) if kind == "invoice" else voucher_doc(rec, led.settings)
        await app.show_document(doc, rec.number, back=("assistant", {}))

    icon = ft.Icons.RECEIPT_LONG_OUTLINED if kind == "invoice" else ft.Icons.PAYMENTS_OUTLINED
    return ft.Row([ft.Container(
        ft.Row([w.icon_tile(icon, w.AI, w.AI_TINT, 44),
                ft.Column([ft.Text(label, size=14, weight=ft.FontWeight.W_700, color=w.INK),
                           w.muted("تم الإصدار — اضغط للعرض والطباعة", 12)], spacing=1, expand=True),
                w.btn("عرض / طباعة", view, "ai", small=True, icon=ft.Icons.PRINT_OUTLINED)],
               spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=w.PANEL, padding=12, border_radius=14, border=w.border_all(1, "#D9D2F5"),
        shadow=w._soft_shadow(10, 2), expand=True, animate=w._anim(300))],
        vertical_alignment=ft.CrossAxisAlignment.START)


def _table_export_button(app, text: str) -> ft.Control | None:
    """إن احتوى رد المساعد على جداول Markdown: زر يحوّلها كلها إلى ملف Excel (ورقة لكل جدول)."""
    tables = excel.parse_markdown_tables(text)
    if not tables:
        return None

    async def export(_):
        try:
            data = await asyncio.to_thread(excel.build_workbook, tables, app.ledger.settings.get("businessName", ""))
            name = excel.safe_filename(f"{tables[0].name}-{len(tables)}جداول" if len(tables) > 1 else tables[0].name)
        except Exception as e:  # noqa: BLE001
            w.toast(app.page, f"تعذّر إنشاء الملف: {e}", error=True)
            return
        await app.save_with_toast(name, data, "ملف Excel")

    label = "تصدير الجدول إلى Excel" if len(tables) == 1 else f"تصدير {len(tables)} جداول إلى Excel"
    return w.btn(label, export, "ghost", small=True, icon=ft.Icons.TABLE_VIEW_OUTLINED)


def _pictures_card(app, items: list) -> ft.Control:
    """صور أصناف أرسلها المساعد: مصغّرات يكبّرها الضغط (نفس عارض صور المخزون)."""
    by_id = {i.id: i for i in app.ledger.inventory}
    tiles = []
    for it in items or []:
        item = by_id.get(it.get("id") if isinstance(it, dict) else None)
        if item is None:
            continue
        tiles.append(ft.Column([
            w.thumb(app, item, 112, zoom=True),
            ft.Text(item.name, size=12, weight=ft.FontWeight.W_600, color=w.INK, max_lines=2, width=116,
                    overflow=ft.TextOverflow.ELLIPSIS, text_align=ft.TextAlign.CENTER),
            w.muted(f"{item.code or '—'} · المخزون {item.stock}", 11),
        ], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER, width=122))
    if not tiles:
        return w.muted("الصور غير متاحة (ربما حُذف الصنف).")
    inner = ft.Column([w.muted("صور من المخزون — اضغط على الصورة لتكبيرها", 12),
                       ft.Row(tiles, wrap=True, spacing=12, run_spacing=12)], spacing=8)
    return ft.Row([w.icon_tile(ft.Icons.IMAGE_OUTLINED, w.AI, w.AI_TINT, 32),
                   ft.Container(inner, bgcolor=w.PANEL, padding=ft.Padding(14, 10, 14, 10), border_radius=16,
                                border=w.border_all(1, w.LINE), expand=True, animate=w._anim(300))],
                  vertical_alignment=ft.CrossAxisAlignment.START, spacing=10)


def _safe_filename(label: str, n: int = 1) -> str:
    import re
    base = re.sub(r'[\\/:*?"<>|\s]+', "-", (label or "").strip()).strip("-.")[:50] or "catalog-image"
    return f"{base}.jpg" if n == 1 else f"{base}-{n}.jpg"


def _image_actions(app, im: dict, compact: bool = False) -> ft.Control:
    """أزرار صورة الكتالوج: حفظ/تحميل على الجهاز + ربطها بصنف من المخزون."""
    async def save(_):
        await app.save_with_toast(_safe_filename(im.get("label", "")), im["data"], "الصورة")

    def link(_):
        w.link_image_to_item(app, im["data"], im.get("label", ""))
    if compact:                    # تحت المصغّرة: أيقونتان بنص مساعد (عرض 132px فقط)
        return ft.Row([
            ft.IconButton(icon=ft.Icons.DOWNLOAD, tooltip="تحميل الصورة", on_click=save, icon_size=20),
            ft.IconButton(icon=ft.Icons.ADD_PHOTO_ALTERNATE_OUTLINED, tooltip="إضافتها كصورة لصنف في المخزون", on_click=link,
                          icon_size=20)], alignment=ft.MainAxisAlignment.CENTER, spacing=0)
    return ft.Row([w.btn("تحميل", save, "ghost", small=True, icon=ft.Icons.DOWNLOAD),
                   w.btn("إضافتها لصنف", link, "primary", small=True, icon=ft.Icons.ADD_PHOTO_ALTERNATE_OUTLINED)],
                  alignment=ft.MainAxisAlignment.CENTER, spacing=8, wrap=True)


def _catalog_pictures_card(app, items: list) -> ft.Control:
    """صور من كتالوج الفلاتر (FMI…): مصغّرات (تكبير بالضغط) مع أزرار تحميل وربط بصنف من المخزون."""
    tiles = []
    for im in items or []:
        data = im.get("data") if isinstance(im, dict) else None
        if not data:
            continue

        def zoom(_, im=im):
            body = ft.Column([
                ft.Container(w.image_from_bytes(im["data"], width=340, height=340, fit=w._contain()), bgcolor="#FFFFFF",
                             padding=6, border_radius=16, border=w.border_all(1, w.LINE), alignment=ft.Alignment(0, 0)),
                ft.Text(im.get("label", ""), size=15, weight=ft.FontWeight.W_700, color=w.INK, text_align=ft.TextAlign.CENTER),
                w.muted(f"المصدر: {im.get('source', '')}", 12),
                _image_actions(app, im)],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8, tight=True)
            w.info_dialog(app, "صورة من الكتالوج", body)

        box = ft.Container(w.image_from_bytes(_thumb_bytes(data), height=124, width=124, fit=w._contain()), width=124, height=124,
                           border_radius=14, bgcolor="#FFFFFF", clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                           border=w.border_all(1, w.LINE), on_click=zoom, ink=True, tooltip="اضغط للتكبير")
        tiles.append(ft.Column([
            box,
            ft.Text(im.get("label", ""), size=12, weight=ft.FontWeight.W_600, color=w.INK, max_lines=2, width=128,
                    overflow=ft.TextOverflow.ELLIPSIS, text_align=ft.TextAlign.CENTER),
            w.muted(im.get("source", ""), 11),
            _image_actions(app, im, compact=True)],
            spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER, width=132))
    if not tiles:
        return w.muted("لا صور متاحة.")
    inner = ft.Column([w.muted("صور من الكتالوج — اضغط للتكبير · ⬇ تحميل · ➕ إضافتها كصورة لصنف في المخزون", 12),
                       ft.Row(tiles, wrap=True, spacing=12, run_spacing=12)], spacing=8)
    return ft.Row([w.icon_tile(ft.Icons.IMAGE_SEARCH, w.AI, w.AI_TINT, 32),
                   ft.Container(inner, bgcolor=w.PANEL, padding=ft.Padding(14, 10, 14, 10), border_radius=16,
                                border=w.border_all(1, w.LINE), expand=True, animate=w._anim(300))],
                  vertical_alignment=ft.CrossAxisAlignment.START, spacing=10)


def _link_card(app, info: dict) -> ft.Control:
    """موقع (كتالوج) طلب المساعد فتحه: زر يفتحه في المتصفح."""
    async def go(_):
        if not await w.open_url(app.page, info.get("url", "")):
            w.toast(app.page, "تعذّر فتح الرابط — انسخه وافتحه في المتصفح: " + str(info.get("url", "")), error=True)
    inner = ft.Column([ft.Text(info.get("name", "الموقع"), size=14, weight=ft.FontWeight.W_600, color=w.INK),
                       w.muted(info.get("url", ""), 12),
                       w.btn("فتح الموقع", go, "primary", small=True, icon=ft.Icons.OPEN_IN_NEW)], spacing=6)
    return ft.Row([w.icon_tile(ft.Icons.PUBLIC, w.AI, w.AI_TINT, 32),
                   ft.Container(inner, bgcolor=w.PANEL, padding=ft.Padding(14, 10, 14, 10), border_radius=16,
                                border=w.border_all(1, w.LINE), expand=True, animate=w._anim(300))],
                  vertical_alignment=ft.CrossAxisAlignment.START, spacing=10)


def _image_export_button(app, role: str, text: str, stamp: str, images: list) -> ft.Control:
    """«إصدار الصورة»: يختار المالك طباعة/PDF أو Excel، وتخرج صور الرد (الفلتر…) مع الجدول."""
    async def as_pdf():
        try:
            entries = ce.build_entries([(role, text)], [stamp], doc_label=lambda ref: _doc_title(app, ref),
                                       image_of=app.images.get_local)
            entries[0].images = list(images)
            doc = await asyncio.to_thread(ce.chat_doc, entries, app.ledger.settings, "رد مساعد شركة العمر", "message")
            await app.show_document(doc, ce.default_filename("رد-المساعد-مع-الصورة"), back=("assistant", {}))
        except Exception as e:  # noqa: BLE001
            w.toast(app.page, f"تعذّر إنشاء PDF: {e}", error=True)

    async def as_xlsx():
        try:
            tables = excel.parse_markdown_tables(text)
            data = await asyncio.to_thread(excel.build_workbook, tables, app.ledger.settings.get("businessName", ""),
                                           list(images))
            name = excel.safe_filename((tables[0].name if tables else "صور-الصنف") + "-مع-الصور")
        except Exception as e:  # noqa: BLE001
            w.toast(app.page, f"تعذّر إنشاء الملف: {e}", error=True)
            return
        await app.save_with_toast(name, data, "ملف Excel")

    def open_choice(_):
        def pick(fn):
            async def go(_e):
                app.page.pop_dialog()
                await fn()
            return go
        content = ft.Column([
            w.muted(f"{len(images)} صورة ستخرج مع الجدول والمعلومات.", 12),
            w.btn("طباعة / PDF مع الصورة", pick(as_pdf), "primary", icon=ft.Icons.PRINT_OUTLINED),
            w.btn("ملف Excel مع الصورة", pick(as_xlsx), "ghost", icon=ft.Icons.TABLE_VIEW_OUTLINED)],
            spacing=10, tight=True)
        app.page.show_dialog(w._dialog_style(ft.AlertDialog(
            title=ft.Text("إصدار الصورة في الطباعة أو الإكسل", weight=ft.FontWeight.W_700),
            content=ft.Container(content, width=340),
            actions=[ft.TextButton("إغلاق", on_click=lambda _e: app.page.pop_dialog())])))
    return w.btn("إصدار الصورة (طباعة / إكسل)", open_choice, "primary", small=True, icon=ft.Icons.IMAGE_OUTLINED)


def _message_pdf_button(app, role: str, text: str, stamp: str) -> ft.Control:
    """طباعة هذا الرد وحده: يُفتح كمستند (عرض + حفظ PDF/صورة) كالفواتير."""
    async def pdf(_):
        try:
            entries = ce.build_entries([(role, text)], [stamp], doc_label=lambda ref: _doc_title(app, ref),
                                       image_of=app.images.get_local)
            doc = await asyncio.to_thread(ce.chat_doc, entries, app.ledger.settings, "رد مساعد شركة العمر", "message")
            await app.show_document(doc, ce.default_filename("رد-المساعد"), back=("assistant", {}))
        except Exception as e:  # noqa: BLE001 — مثلاً لا يوجد خط عربي
            w.toast(app.page, f"تعذّر إنشاء PDF: {e}", error=True)
    return w.btn("طباعة / PDF", pdf, "ghost", small=True, icon=ft.Icons.PICTURE_AS_PDF_OUTLINED)


def _bubble(app, role: str, text, stamp: str = "", images: list | None = None) -> ft.Control:
    if role == "user_image":
        data = text if isinstance(text, (bytes, bytearray)) else b""
        inner = w.image_from_bytes(bytes(data), width=168, height=168, fit=w._contain()) if data else w.muted("صورة")
        return ft.Row([ft.Container(inner, bgcolor=w.PANEL, padding=6, border_radius=16,
                                    border=w.border_all(1, "#D9D2F5"), clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                                    animate=w._anim(300))],
                      alignment=ft.MainAxisAlignment.START)
    if role == "user":
        return ft.Row([ft.Container(ft.Text(text, color="#FFFFFF", size=14, selectable=True), bgcolor=w.AI,
                                    padding=ft.Padding(14, 10, 14, 10), border_radius=16, animate=w._anim(300))],
                      alignment=ft.MainAxisAlignment.START)
    if role == "error":
        return w.banner(text, "red")
    if role == "file":
        return _file_card(app, text)
    if role == "doc":
        return _doc_card(app, text)
    if role == "pictures":
        return _pictures_card(app, text)
    if role == "link":
        return _link_card(app, text)
    if role == "catalog_pictures":
        return _catalog_pictures_card(app, text)

    async def _tap_link(e):
        await w.open_url(app.page, getattr(e, "data", "") or "")
    body = ft.Markdown(text, selectable=True, extension_set=ft.MarkdownExtensionSet.GITHUB_WEB, on_tap_link=_tap_link)
    xl = _table_export_button(app, text)
    img_btn = _image_export_button(app, role, text, stamp, images) if images else None
    actions = ft.Row([*([xl] if xl else []), _message_pdf_button(app, role, text, stamp), *([img_btn] if img_btn else [])],
                     wrap=True, spacing=8, run_spacing=4)
    inner = ft.Column([body, actions], spacing=8)
    return ft.Row([w.icon_tile(ft.Icons.AUTO_AWESOME, w.AI, w.AI_TINT, 32),
                   ft.Container(inner, bgcolor=w.PANEL, padding=ft.Padding(14, 10, 14, 10), border_radius=16,
                                border=w.border_all(1, w.LINE), expand=True, animate=w._anim(300))],
                  vertical_alignment=ft.CrossAxisAlignment.START, spacing=10)


def _setup_card(cfg: GeminiConfig) -> ft.Control:
    return w.card(ft.Column([
        ft.Row([w.icon_tile(ft.Icons.KEY, w.AMBER, w.AMBER_TINT, 40), w.title("فعّل المساعد بمفتاح ذكاء اصطناعي", 16)], spacing=12),
        ft.Text("افتح الإعدادات ← «مزوّدو الذكاء الاصطناعي» واختر أي مزوّد (Gemini، OpenAI، Claude، Groq، OpenRouter، DeepSeek، "
                "أو نموذج محلي Ollama) والصق مفتاحه. يمكنك إضافة أكثر من مزوّد: إذا انتهت حصة الأول ينتقل البرنامج للثاني تلقائياً.",
                size=13, color=w.INK),
        w.muted(f"النموذج الحالي: {cfg.model} · التفكير: {cfg.thinking or 'افتراضي'}"),
    ], spacing=10), padding=18)


def _round_action(icon, tooltip: str, on_click, *, active: bool = False, color: str = w.AI, tint: str = w.AI_TINT):
    """زر دائري تفاعلي (رفع صورة / ميكروفون) مع حركة ضمنية عند المرور."""
    bg = w.RED if active else tint
    fg = "#FFFFFF" if active else color
    c = ft.Container(
        ft.IconButton(icon=icon, icon_color=fg, tooltip=tooltip, on_click=on_click, icon_size=22),
        bgcolor=bg, border_radius=14, width=48, height=48, animate=w._anim(300),
    )
    return w.lift(w.press_feedback(c), 1.06)


def _voice(app):
    """مسجّل الصوت المباشر (يُنشأ مرة واحدة لكل جلسة)."""
    from ...ai.voice import VoiceRecorder
    rec = getattr(app, "_voice", None)
    if rec is None:
        rec = app._voice = VoiceRecorder(app.page, app.home / "voice")
    return rec


async def _call(fn, *a, **kw):
    if fn is None:
        return None
    r = fn(*a, **kw)
    return await r if inspect.isawaitable(r) else r


def build(app) -> ft.Control:
    st = _state(app)
    cfg: GeminiConfig = st["cfg"]
    if st["listening"]:                     # غادر المستخدم الشاشة أثناء التسجيل → نلغيه حتى لا يبقى الميكروفون مفتوحاً
        st["listening"] = False
        app.page.run_task(_voice(app).cancel)
    msgs = ft.Column(spacing=12, scroll=w.smooth_scroll(), auto_scroll=True, expand=True)
    status = ft.Row([], spacing=8, visible=False)
    ring_label = ft.Text("", size=12, color=w.AI)
    pending_row = ft.Row([], wrap=True, spacing=8, run_spacing=8, visible=False)
    rec_label = ft.Text("", size=13, color=w.RED, weight=ft.FontWeight.W_700, expand=True)
    rec_bar = ft.Container(
        ft.Row([rec_label, ft.TextButton("إلغاء", on_click=lambda e: app.page.run_task(cancel_voice))],
               vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=w.RED_TINT, padding=ft.Padding(14, 4, 8, 4), border_radius=12, visible=False)

    def render_messages() -> None:
        if not st["messages"]:
            msgs.controls = [_welcome(), _chips()]
        else:
            ri = st.get("reply_images") or {}
            msgs.controls = [_bubble(app, r, t, _stamp(st, i), ri.get(i)) for i, (r, t) in enumerate(st["messages"])]

    def refresh_pending() -> None:
        thumbs = []
        for i, p in enumerate(list(st["pending"])):
            def drop(_=None, idx=i):
                if 0 <= idx < len(st["pending"]):
                    st["pending"].pop(idx)
                    refresh_pending()

            if p.get("kind") == "excel":      # ملف Excel: شريحة نصية بدل صورة مصغّرة
                thumbs.append(ft.Container(ft.Row([
                    ft.Icon(ft.Icons.TABLE_VIEW_OUTLINED, color=w.TEAL, size=20),
                    ft.Text(f"{p['name']} · {p['rows']} صف", size=12, color=w.INK, max_lines=1),
                    ft.IconButton(icon=ft.Icons.CLOSE, icon_size=14, tooltip="إزالة الملف", on_click=drop)],
                    spacing=4, tight=True), bgcolor=w.TINT, border_radius=12, padding=ft.Padding(10, 4, 4, 4),
                    border=w.border_all(1, w.LINE)))
                continue
            thumbs.append(ft.Stack([
                ft.Container(w.image_from_bytes(p["data"], width=64, height=64, fit=w._contain()),
                             width=64, height=64, border_radius=12, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                             border=w.border_all(1, w.LINE), bgcolor=w.PANEL, animate=w._anim(300)),
                ft.Container(ft.IconButton(icon=ft.Icons.CLOSE, icon_size=14, icon_color="#FFFFFF",
                                           tooltip="إزالة الصورة", on_click=drop),
                             bgcolor="#CCDC2626", border_radius=10, right=0, top=0, width=22, height=22),
            ], width=64, height=64))
        pending_row.controls = thumbs
        pending_row.visible = bool(thumbs)
        try:
            app.page.update()
        except Exception:  # noqa: BLE001
            pass

    def refresh_voice() -> None:
        mic_slot.controls = [_round_action(
            ft.Icons.STOP_ROUNDED if st["listening"] else ft.Icons.MIC_NONE if False else ft.Icons.MIC,
            "إيقاف التسجيل" if st["listening"] else "تعرف صوتي",
            on_voice, active=st["listening"])]
        try:
            app.page.update()
        except Exception:  # noqa: BLE001
            pass

    def set_busy(label: str | None) -> None:
        st["busy"] = label is not None
        status.visible = label is not None
        ring_label.value = label or ""
        status.controls = [ft.ProgressRing(width=16, height=16, stroke_width=2, color=w.AI), ring_label] if label else []
        send_btn.disabled = label is not None
        upload_btn.disabled = label is not None
        app.page.update()

    async def send_text(text: str) -> None:
        text = (text or "").strip()
        pend = list(st.get("pending") or [])
        images = [(p.get("mime") or "image/jpeg", p["data"]) for p in pend if p.get("data") and p.get("kind") != "excel"]
        excels = [p for p in pend if p.get("kind") == "excel"]
        if excels and not text:
            text = "سجّل أصناف هذا الملف في المخزون" if not images else "تعامل مع الملف والصورة المرفقين"
        if (not text and not images) or st["busy"]:
            return
        ai = _get_assistant(app)
        if not getattr(ai.client, "ready", cfg.ready):
            w.toast(app.page, "أضف مفتاح مزوّد ذكاء اصطناعي من الإعدادات أولاً", error=True)
            return
        for _, data in images:
            _add(st, "user_image", data)
        shown_text = text or "📷 صورة مرفقة"
        if excels:
            shown_text = "📊 " + "، ".join(p["name"] for p in excels) + "\n" + shown_text
            text = "\n".join(p["summary"] for p in excels) + "\n" + text      # النموذج يرى ملخص الجدول مع الطلب
        ai.tools.user_images = [p.get("raw") or p["data"] for p in pend if p.get("data") and p.get("kind") != "excel"]
        ai.tools.cleaned_image = None
        for p in excels:
            ai.tools.excel_files[p["name"]] = p["table"]
        _add(st, "user", shown_text)
        st["pending"].clear()
        input_tf.value = ""
        render_messages()
        refresh_pending()
        set_busy("أفكّر…")

        def on_event(kind: str, data: dict) -> None:
            if kind == "tool_start":
                ring_label.value = TOOL_LABELS.get(data["name"], data["name"]) + "…"
            else:
                ring_label.value = "أحلّل النتائج…"
            app.page.update()

        writes_before = ai.tools.writes_done
        exports_before = len(ai.tools.exports)
        docs_before = len(ai.tools.documents)
        pics_before = len(ai.tools.pictures)
        links_before = len(ai.tools.links)
        cpics_before = len(ai.tools.catalog_pictures)
        notes_before = len(ai.tools.notices)
        try:
            answer = await ai.ask(text, on_event, images=images or None)
            _add(st, "assistant", answer)
            ans_idx = len(st["messages"]) - 1
            reply_imgs: list[tuple[str, bytes]] = []                # صور هذا الرد: تخرج مع الطباعة/الإكسل
            seen: set[int] = set()
            for group in ai.tools.pictures[pics_before:]:
                for it in group:
                    data = app.images.get_local(it.get("id", ""))
                    if data and hash(data) not in seen:
                        seen.add(hash(data))
                        reply_imgs.append((" · ".join(x for x in (it.get("name", ""), it.get("code", "")) if x), data))
            for group in ai.tools.catalog_pictures[cpics_before:]:
                for im in group:
                    if im.get("data") and hash(im["data"]) not in seen:
                        seen.add(hash(im["data"]))
                        reply_imgs.append((im.get("label", ""), im["data"]))
            if reply_imgs:
                st.setdefault("reply_images", {})[ans_idx] = reply_imgs[:MAX_REPLY_IMAGES]
            for group in ai.tools.pictures[pics_before:]:        # صور أصناف أرسلها المساعد
                _add(st, "pictures", group)
            for note in ai.tools.notices[notes_before:]:             # «أضفتُ الصورة تلقائياً…»
                _add(st, "assistant", note)
            for group in ai.tools.catalog_pictures[cpics_before:]:   # صور الفلتر من الكتالوج
                _add(st, "catalog_pictures", group)
            for info in ai.tools.links[links_before:]:           # مواقع طلب المساعد فتحها (FMI …)
                _add(st, "link", info)
            for info in ai.tools.exports[exports_before:]:       # ملفات Excel التي أنشأها المساعد في هذا الرد
                _add(st, "file", info["path"])
            for info in ai.tools.documents[docs_before:]:        # فواتير/سندات أنشأها المساعد: بطاقة عرض وطباعة
                _add(st, "doc", f'{info["kind"]}:{info["id"]}')
        except GeminiError as e:
            _add(st, "error", str(e))
        except Exception as e:  # noqa: BLE001 — لا ننهار أمام خطأ غير متوقع
            _add(st, "error", f"خطأ غير متوقع: {type(e).__name__}: {e}")
        render_messages()
        set_busy(None)
        if ai.tools.writes_done != writes_before:
            app.drafts.save_invoice(app.invoice_draft)  # البيانات تغيّرت؛ الشاشات الأخرى تُبنى من جديد عند الدخول

    async def on_send(_):
        await send_text(input_tf.value or "")

    async def on_submit(_):
        await send_text(input_tf.value or "")

    async def on_upload(_):
        if st["busy"]:
            return
        got = await app.pick_file_bytes(extensions=["jpg", "jpeg", "png", "webp", "xlsx", "csv"])
        if not got:
            return
        name, raw = got
        if name.lower().rsplit(".", 1)[-1] in ("xlsx", "xlsm", "csv", "xls", "txt"):
            from ...ai.excel_in import parse_table
            try:
                table = await asyncio.to_thread(parse_table, name, raw)
            except ValueError as e:
                w.toast(app.page, str(e), error=True)
                return
            head = " | ".join(table["headers"])
            sample = "\n".join(" | ".join(r) for r in table["rows"][:6])
            summary = (f"[ملف Excel مرفوع: «{name}» — {len(table['rows'])} صف. الأعمدة: {head}\nأول الصفوف:\n{sample}\n"
                       f"استخدم read_excel_rows لقراءة المزيد، وimport_excel_items لتسجيل الأصناف.]")
            st["pending"].append({"kind": "excel", "name": name, "rows": len(table["rows"]), "table": table,
                                  "summary": summary, "data": None})
            refresh_pending()
            w.toast(app.page, "أُرفق ملف Excel — اكتب طلبك أو اضغط إرسال")
            return
        try:
            from ...ai.media import compress_for_vision, is_image_mime, mime_of
            mime = mime_of(name, raw)
            if not is_image_mime(mime):
                w.toast(app.page, "اختر صورة (JPG / PNG / WEBP) أو ملف Excel (.xlsx / .csv)", error=True)
                return
            jpeg = await asyncio.to_thread(compress_for_vision, raw)
        except ImageError as e:
            w.toast(app.page, str(e), error=True)
            return
        st["pending"].append({"name": name, "mime": "image/jpeg", "data": jpeg, "raw": raw})
        refresh_pending()
        w.toast(app.page, "أُرفقت الصورة — اكتب سؤالك أو اضغط إرسال")

    async def _transcribe_bytes(data: bytes, mime: str) -> str:
        return await _get_assistant(app).client.transcribe_audio(data, mime)

    async def finish_voice() -> None:
        """يوقف التسجيل، ينسخه إلى نص عبر Gemini، ثم يرسله فوراً كسؤال."""
        if not st["listening"]:
            return
        st["listening"] = False
        rec_bar.visible = False
        refresh_voice()
        try:
            audio, mime = await _voice(app).stop()
        except VoiceError as e:
            w.toast(app.page, str(e), error=True)
            return
        if not (cfg.ready or any(sp.kind == "gemini" and sp.ready for sp in load_specs(app.ledger.settings))):
            w.toast(app.page, "نسخ الصوت يحتاج مفتاح Gemini — أضفه من الإعدادات", error=True)
            return
        set_busy("أنفّص الصوت…")
        try:
            spoken = await _transcribe_bytes(audio, mime)
        except GeminiError as e:
            _add(st, "error", str(e))
            render_messages()
            set_busy(None)
            return
        set_busy(None)
        await send_text(spoken)

    async def cancel_voice(_=None) -> None:
        if not st["listening"]:
            return
        st["listening"] = False
        rec_bar.visible = False
        refresh_voice()
        await _voice(app).cancel()

    async def tick() -> None:
        rec = _voice(app)
        while st["listening"] and rec.recording:
            secs = rec.elapsed
            rec_label.value = f"🔴 يسجّل… {secs // 60:02d}:{secs % 60:02d} — تحدّث ثم اضغط ⏹ للإرسال"
            try:
                rec_bar.update()
            except Exception:  # noqa: BLE001 — غادر المستخدم الشاشة
                return
            if secs >= MAX_SECONDS:
                await finish_voice()
                return
            await asyncio.sleep(0.4)

    async def on_voice(_):
        if st["busy"]:
            return
        if st["listening"]:
            await finish_voice()
            return
        try:
            await _voice(app).start()
        except VoiceError as e:
            w.toast(app.page, str(e), error=True)
            return
        st["listening"] = True
        rec_bar.visible = True
        refresh_voice()
        app.page.run_task(tick)

    async def export_chat_pdf(_):
        if not st["messages"]:
            w.toast(app.page, "لا توجد رسائل لطباعتها بعد", error=True)
            return
        try:
            doc = await asyncio.to_thread(ce.chat_doc, _entries(app, st), app.ledger.settings)
            await app.show_document(doc, ce.default_filename(), back=("assistant", {}))
        except Exception as e:  # noqa: BLE001
            w.toast(app.page, f"تعذّر إنشاء PDF: {e}", error=True)

    async def export_chat_excel(_):
        if not st["messages"]:
            w.toast(app.page, "لا توجد رسائل لتصديرها بعد", error=True)
            return
        try:
            data = await asyncio.to_thread(ce.chat_workbook, _entries(app, st), app.ledger.settings.get("businessName", ""))
        except Exception as e:  # noqa: BLE001
            w.toast(app.page, f"تعذّر إنشاء ملف Excel: {e}", error=True)
            return
        await app.save_with_toast(ce.default_filename() + ".xlsx", data, "المحادثة (Excel)")

    def make_chip(text: str):
        async def click(_):
            await send_text(text)
        return click

    def _chips() -> ft.Control:
        chips = []
        for s in SUGGESTIONS:
            c = ft.Container(ft.Text(s, size=12, color=w.AI, weight=ft.FontWeight.W_600), bgcolor=w.AI_TINT,
                             padding=ft.Padding(12, 8, 12, 8), border_radius=20, on_click=make_chip(s),
                             animate=w._anim(300), ink=True)
            chips.append(w.lift(c, 1.04))
        return ft.Row(chips, wrap=True, spacing=8, run_spacing=8)

    def _welcome() -> ft.Control:
        name = app.ledger.settings.get("businessName", "المتجر")
        return w.card(ft.Column([
            ft.Row([w.icon_tile(ft.Icons.AUTO_AWESOME, w.AI, w.AI_TINT, 44),
                    ft.Column([w.title("مساعد شركة العمر", 18), w.muted(f"مدير ذكي لـ «{name}» — يرى بياناتك الحقيقية", 12)],
                              spacing=0, expand=True)], spacing=12),
            ft.Text("اسأله عن المبيعات والأرباح والمخزون والديون، وأرفق صورة قطعة غيار أو فاتورة، أو تحدّث بالزر الصوتي. "
                    "يبحث في جوجل عن الأسعار والمواصفات، وينشئ فواتير وسندات بعد موافقتك.", size=13, color=w.SOFT),
        ], spacing=10), padding=18)

    def toggle_auto(e) -> None:
        st["auto"] = bool(e.control.value)

    def clear(_):
        st["messages"].clear()
        st["stamps"] = []
        st["reply_images"] = {}
        st["pending"].clear()
        if st["listening"]:
            app.page.run_task(_voice(app).cancel)
        st["listening"] = False
        rec_bar.visible = False
        if st["assistant"]:
            st["assistant"].reset()
        render_messages()
        refresh_pending()
        app.page.update()

    input_tf = w.field("اكتب سؤالك أو طلبك للمساعد…", "", on_submit=on_submit, expand=True)
    try:
        narrow = 0 < float(app.page.width or 0) < 600        # عرض غير معروف (0/None) = نعامله كشاشة عريضة
    except Exception:  # noqa: BLE001
        narrow = False
    if narrow:       # الهاتف: زر إرسال دائري بأيقونة فقط ليبقى للحقل عرض كافٍ
        send_btn = ft.Container(ft.IconButton(icon=ft.Icons.SEND_ROUNDED, icon_color="#FFFFFF", tooltip="إرسال",
                                              on_click=on_send, icon_size=22), bgcolor=w.AI, border_radius=14, width=48, height=48)
    else:
        send_btn = w.btn("إرسال", on_send, "ai", icon=ft.Icons.SEND_ROUNDED)
    upload_btn = _round_action(ft.Icons.ADD_PHOTO_ALTERNATE_OUTLINED, "رفع صورة أو ملف Excel", on_upload)
    mic_slot = ft.Row([_round_action(
        ft.Icons.STOP_ROUNDED if st["listening"] else ft.Icons.MIC,
        "إيقاف التسجيل" if st["listening"] else "تعرف صوتي",
        on_voice, active=st["listening"])], spacing=0)

    render_messages()
    refresh_pending()
    # الترويسة: سطر النموذج بعرض كامل (كان يُعصَر إلى ~10px في الهاتف العمودي فيصير حرفاً تحت حرف ويدفع شريط الإرسال خارج الشاشة)،
    # وتحته صف أدوات يلتف تلقائياً (wrap) عند ضيق الشاشة.
    model_line = ft.Text(f"النموذج: {cfg.model} · التفكير: {cfg.thinking or 'افتراضي'} · بحث جوجل مفعّل", size=11,
                         color=w.SOFT, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
    header = ft.Column([
        ft.Container(model_line, width=None, alignment=ft.Alignment(1, 0)),
        ft.Row([
            ft.Switch(label="تنفيذ مباشر بدون سؤال", value=st["auto"], on_change=toggle_auto, active_color=w.AI),
            ft.Row([
                ft.IconButton(icon=ft.Icons.PICTURE_AS_PDF_OUTLINED, tooltip="طباعة المحادثة (PDF)", icon_color=w.SOFT,
                              on_click=export_chat_pdf),
                ft.IconButton(icon=ft.Icons.TABLE_VIEW_OUTLINED, tooltip="تصدير المحادثة إلى Excel", icon_color=w.SOFT,
                              on_click=export_chat_excel),
                ft.IconButton(icon=ft.Icons.DELETE_SWEEP_OUTLINED, tooltip="محادثة جديدة", icon_color=w.SOFT, on_click=clear),
            ], spacing=0, tight=True),
        ], wrap=True, alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=4, run_spacing=0),
    ], spacing=0, tight=True)

    composer = ft.Container(
        ft.Column([
            rec_bar,
            pending_row,
            ft.Row([upload_btn, mic_slot, ft.Container(input_tf, expand=True, width=None), send_btn],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
        ], spacing=8),
        bgcolor=w.PANEL, padding=ft.Padding(10, 10, 10, 10), border_radius=16,
        border=w.border_all(1, w.LINE), shadow=w._soft_shadow(10, 2), animate=w._anim(300),
    )

    return ft.Column([
        header,
        *([] if (cfg.ready or any(sp.ready for sp in load_specs(app.ledger.settings))) else [_setup_card(cfg)]),
        msgs, status, composer,
    ], expand=True, spacing=10)
