"""هيكل التطبيق — تصميم متجاوب عصري.

- شاشات عريضة (كمبيوتر/آيباد): شريط جانبي ثابت بأقسام التطبيق + منطقة محتوى ببطاقات.
- شاشات ضيقة (هاتف): شريط تنقل سفلي (لوحة · فاتورة · مخزون · سجل · المزيد).
يتحول التصميم تلقائياً عند تغيير حجم النافذة أو تدوير الجهاز.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Callable

import flet as ft

from ..data.drafts import DraftStore, invoice_has_content
from ..data.images import ImageStore
from ..ledger import InvoiceDraft, Ledger, PurchaseDraft
from ..ocr.barcode import read_barcodes_async
from ..ocr.preprocess import ImageError
from ..ocr.scanner import InvoiceScanner
from ..ocr.vision import default_client_factory
from ..printing.render import Doc
from . import widgets as w

# (المفتاح، العنوان، الأيقونة)
MAIN_NAV = [
    ("dashboard", "لوحة التحكم", ft.Icons.DASHBOARD_OUTLINED),
    ("invoice", "فاتورة جديدة", ft.Icons.ADD_SHOPPING_CART),
    ("inventory", "المخزون", ft.Icons.INVENTORY_2_OUTLINED),
    ("history", "سجل الفواتير", ft.Icons.RECEIPT_LONG_OUTLINED),
    ("profit", "الأرباح", ft.Icons.TRENDING_UP),
    ("assistant", "مساعد شركة العمر", ft.Icons.AUTO_AWESOME),
]
MORE_NAV = [
    ("customers", "العملاء", ft.Icons.GROUP_OUTLINED),
    ("suppliers", "الموردون", ft.Icons.FACTORY_OUTLINED),
    ("purchases", "فواتير الشراء", ft.Icons.SHOPPING_CART_OUTLINED),
    ("vouchers", "السندات", ft.Icons.PAYMENTS_OUTLINED),
    ("expenses", "المصاريف", ft.Icons.MONEY_OFF_OUTLINED),
    ("reports", "التقارير", ft.Icons.QUERY_STATS_OUTLINED),
    ("scan", "مسح فاتورة شراء", ft.Icons.DOCUMENT_SCANNER_OUTLINED),
    ("settings", "الإعدادات", ft.Icons.SETTINGS_OUTLINED),
]
# تبويبات الشريط السفلي للهاتف (المزيد يفتح صفحة الأقسام الباقية)
BOTTOM_TABS = [("dashboard", "الرئيسية", ft.Icons.DASHBOARD_OUTLINED),
               ("invoice", "فاتورة", ft.Icons.ADD_SHOPPING_CART),
               ("inventory", "المخزون", ft.Icons.INVENTORY_2_OUTLINED),
               ("assistant", "المساعد", ft.Icons.AUTO_AWESOME),
               ("more", "المزيد", ft.Icons.MORE_HORIZ)]
TITLES = {k: v for k, v, _ in MAIN_NAV + MORE_NAV} | {"more": "المزيد"}
# مجموعات الشريط الجانبي (مثل التصميم المطلوب): عنوان المجموعة ← عناصرها
GROUPS = [
    ("القائمة الرئيسية", ["dashboard", "invoice", "inventory", "history", "profit", "assistant"]),
    ("إدارة العملاء والموردين", ["customers", "suppliers", "vouchers", "expenses", "reports"]),
    ("المشتريات والنظام", ["purchases", "scan", "settings"]),
]
GROUP_OF = {k: g for g, keys in GROUPS for k in keys}
ALL_NAV = {k: (v, i) for k, v, i in MAIN_NAV + MORE_NAV}
WIDE_BREAKPOINT = 800   # عرض النافذة (px) الذي نتحول عنده للشريط الجانبي
POS_BREAKPOINT = 1150   # وما فوقه تُعرض شاشة الفاتورة بعمودين (أصناف | فاتورة)
CLOUD_REFRESH_SECONDS = 45  # عند دخول الشاشات الرئيسية نعيد قراءة السحابة بالخلفية (دون تجميد الواجهة) إن مرّ هذا الوقت


_WEEKDAYS_AR = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def _today_ar() -> str:
    from datetime import datetime
    from ..printing.render import _MONTHS
    d = datetime.now()
    return f"{_WEEKDAYS_AR[d.weekday()]}، {d.day} {_MONTHS[d.month - 1]} {d.year}"


class App:
    def __init__(self, page: ft.Page, ledger: Ledger, drafts: DraftStore, work_dir: Path,
                 scanner: InvoiceScanner | None = None, home: Path | None = None, store=None):
        self.page, self.ledger, self.drafts = page, ledger, drafts
        self.work_dir = Path(work_dir)
        self.home = Path(home) if home else self.work_dir.parent   # مجلد بيانات البرنامج (نسخ/اقتراحات المساعد)
        self.store = store                                          # متجر Supabase (None = وضع محلي)
        self.ai_state: dict = {}                                    # حالة محادثة المساعد (تبقى بين التبويبات)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        # تحليل الصور (فاتورة/باركود) عبر مساعد شركة العمر (Gemini) — لا OpenCV ولا Tesseract على الجوال
        self.scanner = scanner or InvoiceScanner(client_factory=default_client_factory(lambda: self.ledger.settings))

        self.invoice_draft: InvoiceDraft = drafts.load_invoice() or InvoiceDraft()
        self.purchase_draft: PurchaseDraft = drafts.load_purchase() or PurchaseDraft()
        self.recovered = invoice_has_content(self.invoice_draft)

        self.tab = "dashboard"
        self.sub: dict = {}
        self.doc_return: tuple[str, dict] = ("history", {})
        self.unlocked = not ledger.has_password

        self.body = ft.Container(expand=True, padding=0, bgcolor=w.BG)
        self.bottom = ft.Container(bgcolor=w.PANEL, padding=ft.Padding(6, 4, 6, 4),
                                   border=w.border_side(top=True))
        self.sidebar = ft.Container(width=252, bgcolor=w.PANEL,
                                    border=w.border_side(left=True))
        self.picker = ft.FilePicker()
        page.services.append(self.picker)
        self.images = ImageStore(store, self.home / "images")
        self._bg_busy = False
        self._img_job = False
        self._last_tab: str | None = None
        self._enter_flag = True      # الرسمة القادمة هي «دخول» لقسم → تُشغَّل حركات الدخول البطيئة
        self.entering = True         # تقرؤه الويدجتس (w.reveal) أثناء بناء الشاشة الحالية
        self._switcher = self._make_switcher()

    # ---- تشغيل ----------------------------------------------------------------------------------
    async def start(self) -> None:
        p = self.page
        p.title = self.ledger.settings.get("businessName", "دفتري")
        try:
            p.rtl = True
        except Exception:  # noqa: BLE001 — بعض الإصدارات تتحكم بالاتجاه بطريقة أخرى
            pass
        p.padding = 0
        # طبّق الثيم المحفوظ قبل رسم الواجهة
        theme_name = (self.ledger.settings.get("theme") or "green").lower()
        w.apply_theme(theme_name)
        # إن وُجد مفتاح Gemini في الإعدادات فضعه في البيئة
        gkey = (self.ledger.settings.get("geminiApiKey") or "").strip()
        if gkey:
            os.environ["GEMINI_API_KEY"] = gkey
        gmodel = (self.ledger.settings.get("geminiModel") or "").strip()
        if gmodel:
            os.environ["GEMINI_MODEL"] = gmodel
        p.bgcolor = w.BG
        self.body.bgcolor = w.BG
        self.bottom.bgcolor = w.PANEL
        self.sidebar.bgcolor = w.PANEL
        self._apply_font()
        import threading
        from ..printing.render import prewarm
        threading.Thread(target=prewarm, daemon=True).start()     # يجهّز الخط في الخلفية قبل أول فاتورة
        threading.Thread(target=self.images.preload, daemon=True).start()   # كل صور الأصناف إلى الذاكرة (سرعة فتح المخزون)
        for ev in ("on_resize", "on_resized", "on_media_change"):   # الاسم يختلف بين إصدارات Flet؛ كلها تُعيد الرسم عند تغيّر القياس
            try:
                setattr(p, ev, self._on_resize)
            except Exception:  # noqa: BLE001
                pass
        self._wide = self._is_wide()
        self._layout = self._layout_key()
        # SafeArea: لا يختفي المحتوى تحت النتوء (notch) وشريط الحالة وشريط إيماءات آيفون/أندرويد
        p.add(ft.SafeArea(ft.Column([self.body, self.bottom], expand=True, spacing=0), expand=True))
        p.run_task(self._background)
        await self.render()

    def _make_switcher(self):
        """مبدّل المحتوى: تلاشٍ بطيء عند التنقل بين الأقسام (يرجع None إن لم يدعمه الإصدار).
        المدد من widgets (NAV_FADE_MS …) وتُضبط كلها بمتغير DAFTARI_MOTION."""
        try:
            return ft.AnimatedSwitcher(
                content=ft.Container(), transition=ft.AnimatedSwitcherTransition.FADE, duration=w.ms(w.NAV_FADE_MS),
                reverse_duration=w.ms(w.NAV_FADE_OUT_MS), switch_in_curve=ft.AnimationCurve.EASE_OUT,
                switch_out_curve=ft.AnimationCurve.EASE_IN, expand=True)
        except Exception as e:  # noqa: BLE001 — الحركة تجميلية؛ نكمل بدونها
            print("switcher:", e)
            return None

    def _animated(self, content: ft.Control) -> ft.Control:
        """يضع المحتوى داخل المبدّل: تلاشٍ + انزلاق بطيء من الأسفل عند تغيّر القسم، وفورياً عند إعادة رسم نفس القسم."""
        sw = self._switcher
        if sw is None:
            return content
        changed = self.entering or self._last_tab != self.tab
        try:
            sw.duration = w.ms(w.NAV_FADE_MS) if changed else 1
            sw.reverse_duration = w.ms(w.NAV_FADE_OUT_MS) if changed else 1
            inner = ft.Container(content, expand=True)
            if changed:
                try:     # يبدأ منخفضاً قليلاً ثم يصعد ببطء إلى مكانه مع التلاشي
                    inner.offset = ft.Offset(0, 0.06)
                    inner.animate_offset = w._anim(w.ms(w.NAV_SLIDE_MS))

                    def settle(inner=inner):
                        inner.offset = ft.Offset(0, 0)
                        inner.update()
                    w.after_paint(self.page, settle, 0.08)
                except Exception as e:  # noqa: BLE001
                    print("slide:", e)
            sw.content = inner
        except Exception:  # noqa: BLE001
            return content
        self._last_tab = self.tab
        return sw

    def _apply_font(self) -> None:
        """خط عربي واضح لكل النصوص: Cairo/Noto إن وضعتَه في assets/fonts، وإلا خط النظام (Segoe UI / Tahoma)."""
        try:
            from ..printing.render import ASSETS
            fam = None
            fonts_dir = Path(ASSETS) / "fonts"
            for name, family in (("Cairo-Regular.ttf", "Cairo"), ("NotoNaskhArabic-Regular.ttf", "NotoNaskh"),
                                 ("NotoSansArabic-Regular.ttf", "NotoSansArabic")):
                if (fonts_dir / name).exists():
                    self.page.fonts = {family: f"fonts/{name}"}
                    fam = family
                    break
            fam = fam or ("Segoe UI" if os.name == "nt" else "Tahoma")
            self.page.theme = ft.Theme(font_family=fam)
        except Exception as e:  # noqa: BLE001 — الخط تجميلي، لا يوقف التشغيل
            print("font:", e)

    def line_images(self, doc_record) -> dict[str, bytes]:
        """صور بنود فاتورة/شراء من الكاش المحلي (فوري) لتُرسم بالمستند المطبوع."""
        out: dict[str, bytes] = {}
        for l in getattr(doc_record, "items", []):
            b = self.images.get_doc_image(l.id) if not getattr(l, "is_custom", False) else None
            if b:
                out[l.id] = b
        return out

    def _is_wide(self) -> bool:
        try:
            return (self.page.width or 0) >= WIDE_BREAKPOINT
        except Exception:  # noqa: BLE001
            return False

    def _layout_key(self) -> tuple:
        """مفتاح التخطيط الحالي: (شريط جانبي؟، فاتورة بعمودين؟). يتغيّر عند تدوير الجوال/الآيباد أو تحجيم نافذة اللابتوب."""
        return (self._is_wide(), self.pos_split())

    def _on_resize(self, _=None) -> None:
        key = self._layout_key()
        if key != getattr(self, "_layout", key):
            self._layout = key
            self._wide = key[0]
            self.page.run_task(self.render)

    async def _background(self) -> None:
        """كل 20 ثانية: إقفال الفاتورة عند انتهاء المؤقت + إعادة محاولة رفع ما حُفظ محلياً فقط."""
        while True:
            await asyncio.sleep(20)
            try:
                d = self.invoice_draft
                if d.expiry is not None and time.time() >= d.expiry:
                    if invoice_has_content(d):
                        from .tabs import invoice as invoice_tab
                        await invoice_tab.finalize(self)
                        w.toast(self.page, "انتهى الوقت المحدد — تم إقفال الفاتورة تلقائياً")
                    else:
                        d.expiry = None
                if self.ledger.repo.dirty:
                    await self.ledger.sync_pending()
            except Exception as e:  # noqa: BLE001 — لا نوقف حلقة الخلفية بسبب خطأ عابر
                print("background:", e)

    # ---- التنقل ---------------------------------------------------------------------------------
    async def go(self, tab: str, **sub) -> None:
        self.tab, self.sub = tab, sub
        self._enter_flag = True             # حركة الدخول البطيئة للقسم وبطاقاته
        await self.render()                 # ارسم فوراً من البيانات الموجودة (لا انتظار للشبكة)
        if tab in ("dashboard", "invoice", "inventory", "customers", "history") and self.store is not None \
                and time.time() - self.ledger.last_loaded > CLOUD_REFRESH_SECONDS:
            self.page.run_task(self._bg_reload)   # ثم حدّث من السحابة بالخلفية وأعد الرسم فقط إن تغيّرت البيانات

    def _signature(self) -> tuple:
        led = self.ledger
        return (len(led.inventory), sum(i.stock for i in led.inventory), len(led.invoices), len(led.customers),
                len(led.suppliers), len(led.purchases), len(led.vouchers), len(led.expenses))

    async def _bg_reload(self) -> None:
        if self._bg_busy:
            return
        self._bg_busy = True
        try:
            before, tab = self._signature(), self.tab
            if await self.ledger.reload() and self._signature() != before and self.tab == tab and not self.sub.get("doc"):
                await self.render()
                self.queue_images()
        except Exception as e:  # noqa: BLE001
            print("reload:", e)
        finally:
            self._bg_busy = False

    async def cloud_boot(self) -> None:
        """بعد فتح البرنامج بسرعة من الكاش المحلي: اجلب أحدث بيانات السحابة بالخلفية ثم حدّث الشاشة."""
        try:
            await self.ledger.reload(force=not self.ledger.repo.dirty)
        except Exception as e:  # noqa: BLE001
            w.toast(self.page, f"تعذّر الاتصال بالسحابة — تعمل الآن على النسخة المحلية ({e})", error=True)
            return
        notice = self.ledger.health_notice()
        if notice:
            w.toast(self.page, notice, error=True)
        if not self.sub.get("doc"):
            await self.render()
        self.queue_images()

    # ---- صور الأصناف: تنزيل بالخلفية على دفعات، ثم إعادة رسم هادئة ----
    def queue_images(self) -> None:
        if self._img_job or self.store is None:
            return
        ids = [i.id for i in self.ledger.inventory if getattr(i, "img", None)]
        if not ids:
            return
        self._img_job = True

        async def job():
            try:
                got = await self.images.fetch_missing(ids)
                if got and self.tab in ("inventory", "invoice", "purchases") and not self.sub.get("doc"):
                    await self.render()
            except Exception as e:  # noqa: BLE001
                print("images:", e)
            finally:
                self._img_job = False
        self.page.run_task(job)

    def page_width(self) -> float:
        try:
            return float(self.page.width or 0)
        except Exception:  # noqa: BLE001
            return 0.0

    def pos_split(self) -> bool:
        """هل نعرض الفاتورة بعمودين؟ (شاشة عريضة فعلاً، لا مجرد تجاوز حد الشريط الجانبي)"""
        return self.page_width() >= POS_BREAKPOINT

    async def refresh_cloud(self) -> None:
        """زر «تحديث»: إعادة قراءة كل شيء من Supabase وإظهار النتيجة."""
        if self.store is None:
            w.toast(self.page, "وضع محلي — لا يوجد اتصال بـ Supabase (راجع ملف .env)", error=True)
            return
        if self.ledger.repo.dirty:
            await self.ledger.sync_pending()
        try:
            await self.ledger.reload(force=not self.ledger.repo.dirty)
        except Exception as e:  # noqa: BLE001
            w.toast(self.page, f"تعذّر التحديث: {e}", error=True)
            return
        notice = self.ledger.health_notice()
        if notice:
            w.toast(self.page, notice, error=True)
        else:
            w.toast(self.page, f"تم التحديث — {len(self.ledger.inventory)} صنف و{len(self.ledger.invoices)} فاتورة")
        await self.render()

    async def after_change(self) -> None:
        """بعد أي عملية حفظ: خزّن المسودات محلياً وأعد رسم الشاشة الحالية."""
        self.drafts.save_invoice(self.invoice_draft)
        self.drafts.save_purchase(self.purchase_draft)
        await self.render()

    async def render(self) -> None:
        self.entering, self._enter_flag = self._enter_flag, False
        if not self.unlocked:
            self.body.content = self._lock_view()
            self.bottom.visible = False
            self.page.update()
            return
        try:
            content = await self._build_current()
        except Exception as e:  # noqa: BLE001 — شاشة خطأ مفهومة بدل شاشة بيضاء
            content = ft.Column([w.banner(f"تعذّر عرض الشاشة: {e}", "red"),
                                 w.btn("رجوع", lambda _: self.page.run_task(self.go, "dashboard"))])

        content = self._animated(content)
        wide = getattr(self, "_wide", self._is_wide())
        if wide:
            self.bottom.visible = False
            self.body.content = ft.Row([
                self._sidebar(),
                ft.Container(self._top_bar(content), expand=True, bgcolor=w.BG),
            ], spacing=0, expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH)
        else:
            self.bottom.visible = True
            self.bottom.content = self._bottom_bar()
            self.body.content = ft.Container(content, expand=True, padding=12, bgcolor=w.BG)
        self.page.update()

    async def _build_current(self) -> ft.Control:
        if "doc" in self.sub:
            return self._doc_view()
        from .tabs import (assistant, customers, dashboard, expenses, history, inventory, invoice, more, profit,
                           purchases, reports, settings, suppliers, vouchers)
        from .scan_view import ScanView
        builders: dict[str, Callable] = {
            "dashboard": dashboard.build,
            "inventory": inventory.build, "invoice": invoice.build, "history": history.build,
            "profit": profit.build, "more": more.build, "customers": customers.build,
            "suppliers": suppliers.build, "purchases": purchases.build, "vouchers": vouchers.build,
            "expenses": expenses.build, "reports": reports.build, "settings": settings.build,
            "assistant": assistant.build,
        }
        if self.tab == "scan":
            sv = ScanView(self.page, self.ledger, self.purchase_draft, self.scanner)
            self._scan_view = sv  # يبقى حياً حتى لا تُجمَّع معالجاته
            return self._with_back(sv.build(), "مسح فاتورة شراء")
        view = builders[self.tab](self)
        view = await view if asyncio.iscoroutine(view) else view
        if self.tab in dict((k, v) for k, v, _ in MORE_NAV):
            return self._with_back(view, TITLES[self.tab])
        return view

    def _with_back(self, content: ft.Control, title: str) -> ft.Control:
        if getattr(self, "_wide", False):  # الشريط العلوي يعرض العنوان أصلاً، ولا حاجة لزر رجوع مع الشريط الجانبي
            return ft.Column([ft.Container(content, expand=True)], expand=True, spacing=6)
        back_tab = "more"

        async def back(_):
            await self.go(back_tab)
        return ft.Column([ft.Row([ft.IconButton(icon=ft.Icons.ARROW_FORWARD, icon_color=w.TEAL,
                                                on_click=back),
                                  w.title(title)]),
                          ft.Container(content, expand=True)], expand=True, spacing=6)

    # ---- الشريط الجانبي (كمبيوتر/آيباد) ----------------------------------------------------------
    def _nav_item(self, key: str, label: str, icon: str) -> ft.Control:
        active = self.tab == key
        ai = key == "assistant"
        fg = (w.AI if ai else w.TEAL) if active else (w.AI if ai else w.SOFT)
        row = [ft.Icon(icon, size=20, color=fg),
               ft.Text(label, size=14, weight=ft.FontWeight.W_700 if active else ft.FontWeight.W_500,
                       color=(w.AI if ai else w.TEAL) if active else w.INK, expand=True)]
        if ai:
            row.append(w.pill("AI", "ai"))
        c = ft.Container(
            ft.Row(row, spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=(w.AI_TINT if ai else w.TINT) if active else None,
            border_radius=12, padding=ft.Padding(14, 11, 14, 11), ink=True,
            on_click=lambda _: self.page.run_task(self.go, key),
            animate=w._anim(300),
        )
        if active and self.entering:    # تلوّن بطيء للعنصر الجديد النشط عند التنقل
            try:
                target = w.AI_TINT if ai else w.TINT
                c.bgcolor = None
                c.animate = w._anim(w.ms(w.NAV_ITEM_MS))

                def fill(c=c, target=target):
                    c.bgcolor = target
                    c.update()
                w.after_paint(self.page, fill, 0.1)
            except Exception:  # noqa: BLE001
                pass
        if not active:      # العنصر غير النشط يتلوّن بنعومة عند مرور الماوس ثم يعود
            try:
                c.animate = w._anim(160)

                def hover(e, c=c):
                    on = str(getattr(e, "data", "")).lower() in ("true", "1")
                    c.bgcolor = w.SLATE_TINT if on else None
                    try:
                        c.update()
                    except Exception:  # noqa: BLE001
                        pass
                c.on_hover = hover
            except Exception:  # noqa: BLE001
                pass
        return c

    def _sidebar(self) -> ft.Control:
        name = self.ledger.settings.get("businessName", "دفتري")
        logo = ft.Container(
            ft.Row([
                ft.Container(ft.Icon(ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, color="#FFFFFF", size=22),
                             bgcolor=w.TEAL, width=42, height=42, border_radius=13, alignment=ft.Alignment(0, 0)),
                ft.Column([ft.Text(name, size=14, weight=ft.FontWeight.W_800, color=w.INK,
                                   overflow=ft.TextOverflow.ELLIPSIS, max_lines=2),
                           w.muted(self.ledger.settings.get("tagline", ""), 11)], spacing=0, expand=True),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding(8, 14, 8, 8))
        nav: list[ft.Control] = []
        for gtitle, keys in GROUPS:
            nav.append(ft.Container(w.section_header(gtitle), padding=ft.Padding(14, 14, 14, 4)))
            nav += [self._nav_item(k, ALL_NAV[k][0], ALL_NAV[k][1]) for k in keys]
        footer = ft.Container(
            ft.Row([
                ft.Container(ft.Icon(ft.Icons.STOREFRONT_OUTLINED, color="#FFFFFF", size=20), bgcolor=w.TEAL,
                             width=38, height=38, border_radius=12, alignment=ft.Alignment(0, 0)),
                ft.Column([ft.Text(name, size=12, weight=ft.FontWeight.W_700, color=w.INK, max_lines=1,
                                   overflow=ft.TextOverflow.ELLIPSIS),
                           w.muted(self.ledger.settings.get("tagline", "") or "الحساب الحالي", 11)],
                          spacing=0, expand=True),
                ft.IconButton(icon=ft.Icons.LOCK_OUTLINED, icon_size=18, icon_color=w.SOFT, tooltip="قفل التطبيق",
                              on_click=lambda _: self.page.run_task(self.lock)),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding(10, 10, 6, 10), border_radius=14, bgcolor=w.SLATE_TINT)
        return ft.Container(
            ft.Column([logo, ft.Column(nav, spacing=2, scroll=w.smooth_scroll(), expand=True),
                       ft.Divider(color=w.LINE, height=1), footer], spacing=6, expand=True),
            width=264, bgcolor=w.PANEL, border=w.border_side(left=True), padding=ft.Padding(10, 0, 10, 10))

    def sync_status(self) -> ft.Control:
        """وسم حالة الاتصال: متصل بالسحابة / تغييرات غير مرفوعة / وضع محلي."""
        if self.store is None:
            return w.pill("وضع محلي", "amber")
        if self.ledger.repo.dirty:
            return w.pill("غير مزامَن", "red")
        return w.pill("● متصل بـ Supabase", "green")

    def _top_bar(self, content: ft.Control) -> ft.Control:
        async def refresh(_):
            await self.refresh_cloud()

        async def ask_ai(_):
            await self.go("assistant")

        return ft.Column([
            ft.Container(
                ft.Row([
                    ft.Column([w.title((GROUP_OF.get(self.tab, "") + " / " if self.tab in GROUP_OF and self.tab != "dashboard" else "")
                                       + TITLES.get(self.tab, ""), 22), w.muted(_today_ar(), 13)], spacing=0, expand=True),
                    self.sync_status(),
                    ft.IconButton(icon=ft.Icons.SYNC, icon_color=w.SOFT, tooltip="تحديث من السحابة", on_click=refresh),
                    ft.Container(ft.IconButton(icon=ft.Icons.AUTO_AWESOME, icon_color=w.AI, tooltip="مساعد شركة العمر",
                                               on_click=ask_ai), bgcolor=w.AI_TINT, border_radius=12),
                    ft.IconButton(icon=ft.Icons.LOCK_OUTLINED, icon_color=w.SOFT,
                                  tooltip="قفل", on_click=lambda _: self.page.run_task(self.lock)),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
                padding=ft.Padding(24, 16, 24, 8)),
            ft.Container(content, expand=True,
                         padding=ft.Padding(24, 6, 24, 20)),
        ], spacing=0, expand=True)

    # ---- الشريط السفلي (هاتف) ---------------------------------------------------------------------
    def _bottom_bar(self) -> ft.Control:
        active = self.tab if self.tab in dict((k, v) for k, v, _ in BOTTOM_TABS) else "more"

        def make(key: str):
            async def click(_):
                await self.go(key)
            return click

        items = []
        for key, label, icon in BOTTOM_TABS:
            on = key == active
            ai = key == "assistant"
            col = (w.AI if ai else w.TEAL) if on else w.SOFT
            items.append(ft.Container(
                ft.Column([ft.Icon(icon, size=22, color=col),
                           ft.Text(label, size=10, weight=ft.FontWeight.W_700 if on else ft.FontWeight.W_500, color=col)],
                          horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2),
                bgcolor=(w.AI_TINT if ai else w.TINT) if on else None, border_radius=14,
                padding=ft.Padding(6, 7, 6, 7), expand=True, ink=True, on_click=make(key),
                animate=w._anim(300)))
        return ft.Row(items, spacing=2)

    # ---- القفل ---------------------------------------------------------------------------------------
    def _lock_view(self) -> ft.Control:
        pw = w.field("كلمة المرور", "", kind="password", autofocus=True)
        err = ft.Text("", color=w.RED)

        async def unlock(_):
            if self.ledger.check_password(pw.value or ""):
                await self.ledger.upgrade_legacy_password(pw.value)  # ترقية كلمة المرور النصية القديمة
                self.unlocked = True
                await self.render()
            else:
                err.value = "كلمة المرور غير صحيحة"
                pw.value = ""
                self.page.update()

        pw.on_submit = unlock
        return ft.Column([
            ft.Container(height=90),
            ft.Container(ft.Icon(ft.Icons.MENU_BOOK_OUTLINED, color="#FFFFFF", size=34),
                         bgcolor=w.TEAL, padding=18, border_radius=20, shadow=w._soft_shadow()),
            ft.Container(height=8),
            w.title(self.ledger.settings.get("businessName", "دفتري"), 24),
            w.muted("التطبيق مقفل — أدخل كلمة المرور للمتابعة"),
            ft.Container(height=8),
            ft.Container(pw, width=300),
            err,
            ft.Container(w.btn("فتح", unlock, expand=True), width=300),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=10)

    async def lock(self) -> None:
        self.unlocked = not self.ledger.has_password
        await self.render()

    # ---- ملفات / باركود ---------------------------------------------------------------------------------
    async def pick_file_bytes(self, extensions: list[str] | None = None, images: bool = False) -> tuple[str, bytes] | None:
        kw = {"file_type": ft.FilePickerFileType.IMAGE} if images else (
            {"file_type": ft.FilePickerFileType.CUSTOM, "allowed_extensions": extensions} if extensions else {})
        files = await self.picker.pick_files(with_data=True, **kw)
        if not files:
            return None
        f = files[0]
        data = getattr(f, "bytes", None)
        if not data and getattr(f, "path", None):
            data = await asyncio.to_thread(Path(f.path).read_bytes)
        return (f.name, data) if data else None

    # ---- الحفظ والفتح -------------------------------------------------------------------------------
    def is_web(self) -> bool:
        return bool(getattr(self.page, "web", False))

    def _is_mobile(self) -> bool:
        return any(k in str(getattr(self.page, "platform", "") or "").lower() for k in ("android", "ios"))

    async def save_bytes(self, name: str, data: bytes) -> str | None:
        """يحفظ ملفاً عبر نافذة «حفظ باسم». يرجع وصف المكان، أو None إن ألغى المستخدم النافذة.

        مشاكل كانت هنا:
          • على سطح المكتب تعيد Flet المسار المختار **دون أن تكتب الملف** (src_bytes لا يُكتب إلا في الويب/الجوال)،
            فكان البرنامج يقول «تم الحفظ» ولا يوجد ملف. نكتب الملف بأنفسنا ونتحقق من حجمه.
          • إلغاء النافذة كان يُعدّ نجاحاً.
          • إن كتب المستخدم الاسم بلا امتداد حُفظ الملف بلا .xlsx فلا يفتحه إكسل — نضيف الامتداد.
        """
        ext = Path(name).suffix.lower()
        web, mobile = self.is_web(), self._is_mobile()
        kw: dict = {"file_name": name, "src_bytes": data}
        if ext:
            kw.update(file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=[ext.lstrip(".")])
        try:
            try:
                path = await self.picker.save_file(dialog_title="حفظ باسم", **kw)
            except TypeError:                                   # إصدار Flet لا يعرف بعض الوسائط
                path = await self.picker.save_file(file_name=name, src_bytes=data)
        except Exception as e:  # noqa: BLE001 — المنصة/الإصدار لا يدعم نافذة الحفظ
            print("save_file:", e)
            if web:
                raise
            return await self._save_in_work_dir(name, data)
        if path:
            return str(path) if (web or mobile) else await self._ensure_saved(str(path), ext, data, name)
        # None: في الويب/الجوال يعني غالباً أن التنزيل بدأ؛ على سطح المكتب يعني أن المستخدم ألغى
        return "المكان الذي اخترته" if (web or mobile) else None

    async def _ensure_saved(self, path: str, ext: str, data: bytes, name: str) -> str:
        target = Path(path)
        if ext and not target.suffix:
            target = target.with_name(target.name + ext)

        def write() -> None:
            src = Path(path)
            if target != src and src.exists() and src.stat().st_size == len(data):
                src.replace(target)                              # كُتب بلا امتداد: نعيد تسميته بدل نسخة مكررة
            elif not target.exists() or target.stat().st_size != len(data):
                target.write_bytes(data)

        try:
            await asyncio.to_thread(write)
        except OSError as e:                                     # مثلاً الملف مفتوح في إكسل أو لا صلاحية للمجلد
            where = await self._save_in_work_dir(name, data)
            return f"{where} (تعذّر الحفظ في المكان المختار: {e})"
        return str(target)

    async def _save_in_work_dir(self, name: str, data: bytes) -> str:
        out = self.work_dir / name
        await asyncio.to_thread(out.write_bytes, data)
        return str(out)

    async def save_with_toast(self, name: str, data: bytes, label: str = "الملف") -> bool:
        """حفظ + إشعار مناسب للنتيجة (نجاح/إلغاء/خطأ). يرجع True عند النجاح."""
        try:
            where = await self.save_bytes(name, data)
        except Exception as e:  # noqa: BLE001
            w.toast(self.page, f"تعذّر الحفظ: {e}", error=True)
            return False
        if where is None:
            w.toast(self.page, "أُلغي الحفظ")
            return False
        w.toast(self.page, f"تم حفظ {label}: {where}")
        return True

    def open_file(self, path: str | Path) -> bool:
        """يفتح ملفاً بالبرنامج الافتراضي في النظام (Excel مثلاً). يرجع False إن تعذّر.
        في وضع الويب لا معنى لفتحه على الخادم — يجب تنزيله عبر المتصفح (save_bytes)."""
        if self.is_web():
            return False
        import subprocess
        import sys
        try:
            if hasattr(os, "startfile"):                        # ويندوز
                os.startfile(str(path))                         # type: ignore[attr-defined]
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
            return True
        except Exception as e:  # noqa: BLE001
            print("open_file:", e)
            return False

    async def scan_barcode(self) -> str | None:
        """يقرأ باركود/رقم صنف من صورة. أولاً قارئ OpenCV المحلي إن وُجد (ويندوز، فوري وبلا إنترنت)،
        وإلا (أو إن لم يجد شيئاً) يقرأها مساعد شركة العمر (Gemini) — وهو المسار الوحيد على الجوال."""
        from ..ai.gemini import GeminiError
        got = await self.pick_file_bytes(images=True)
        if not got:
            return None
        codes: list[str] = []
        try:
            try:
                codes = await read_barcodes_async(got[1])
            except (ImageError, RuntimeError):      # OpenCV غير متوفر (أندرويد/iOS) أو الصورة لا تُقرأ محلياً
                codes = []
            if not codes:
                w.toast(self.page, "جارٍ قراءة الباركود بالذكاء الاصطناعي…")
                codes = await self.scanner.read_barcodes(got[1])
        except (ImageError, RuntimeError, GeminiError) as e:
            w.toast(self.page, str(e), error=True)
            return None
        if not codes:
            w.toast(self.page, "لم يُقرأ باركود — صوّر الباركود كاملاً من قرب (15 سم) بإضاءة جيدة", error=True)
            return None
        return codes[0]

    def run_after_dialog(self, fn) -> None:
        """ينفّذ fn (coroutine function) بعد أن يُغلق الحوار الحالي وتُعاد رسم الصفحة —
        مثلاً لعرض مستند السند مباشرة بعد تسجيله من نافذة الإدخال."""
        async def later():
            await asyncio.sleep(0.2)
            await fn()
        self.page.run_task(later)

    # ---- المستندات ------------------------------------------------------------------------------------
    async def show_document(self, doc: Doc, filename: str, back: tuple[str, dict] | None = None) -> None:
        self.doc_return = back or (self.tab, dict(self.sub))
        pages = await asyncio.to_thread(doc.png_pages)
        self.tab, self.sub = self.tab, {"doc": doc, "pages": pages, "filename": filename}
        self._enter_flag = True
        await self.render()

    def _doc_view(self) -> ft.Control:
        doc: Doc = self.sub["doc"]
        pages: list[bytes] = self.sub["pages"]
        name: str = self.sub["filename"]

        async def save_pdf(_):
            pdf = await asyncio.to_thread(lambda: _pdf_bytes(doc))
            await self.save_with_toast(name + ".pdf", pdf, "PDF")

        async def save_img(_):
            await self.save_with_toast(name + ".png", pages[0], "الصورة")

        async def save_xlsx(_):
            try:
                data = await asyncio.to_thread(doc.excel)
            except Exception as e:  # noqa: BLE001
                w.toast(self.page, f"تعذّر إنشاء ملف Excel: {e}", error=True)
                return
            await self.save_with_toast(name + ".xlsx", data, "ملف Excel")

        async def back(_):
            tab, sub = self.doc_return
            await self.go(tab, **sub)

        xl = [w.btn("📊 Excel", save_xlsx, "ghost")] if getattr(doc, "excel", None) else []
        return ft.Column(
            [ft.Row([w.btn("رجوع", back, "ghost"), w.btn("💾 حفظ PDF", save_pdf),
                     w.btn("🖼 صورة", save_img, "ghost"), *xl], wrap=True),
             *[ft.Container(w.image_from_bytes(p, width=None), bgcolor="#FFFFFF", padding=4,
                            border_radius=12) for p in pages]],
            scroll=w.smooth_scroll(), expand=True, spacing=10)


def _pdf_bytes(doc: Doc) -> bytes:
    import io
    buf = io.BytesIO()
    first, rest = doc.pages[0], doc.pages[1:]
    first.save(buf, "PDF", resolution=150.0, save_all=True, append_images=rest)
    return buf.getvalue()
