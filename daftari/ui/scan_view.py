"""شاشة مسح الفاتورة (Flet ≥ 0.81 — الواجهة الحديثة: ft.run / page.services / ft.Button).

التدفق: [اختيار صورة] -> [معاينة وجودة] -> [مسح] -> جدول قابل للتعديل -> [إضافة لفاتورة الشراء]
الأسطر الضعيفة (ثقة منخفضة أو فشل التحقق الحسابي) تظهر بلون برتقالي مع سبب المشكلة.
لا يدخل شيء إلى المخزون قبل ضغط "تم" في شاشة الشراء، والأصناف الجديدة تمرّ بمرحلة التسعير.
"""
from __future__ import annotations

import base64
from decimal import Decimal
from pathlib import Path

import flet as ft

from ..core.money import fmt_money, parse_num
from ..ledger import Ledger, LedgerError, PurchaseDraft
from ..ocr.parser import ScannedInvoice, ScannedRow
from ..ocr.preprocess import ImageError
from ..ocr.engine import OcrUnavailable
from ..ocr.scanner import InvoiceScanner
from . import widgets as w

AMBER, GREEN, RED = ft.Colors.AMBER_100, ft.Colors.GREEN_50, ft.Colors.RED_100


def _image(png: bytes, height: int = 260) -> ft.Control:
    from .widgets import image_from_bytes
    return image_from_bytes(png, height=height)


class ScanView:
    def __init__(self, page: ft.Page, ledger: Ledger, draft: PurchaseDraft, scanner: InvoiceScanner | None = None):
        self.page, self.ledger, self.draft = page, ledger, draft
        self.scanner = scanner or InvoiceScanner()
        self.image_bytes: bytes | None = None
        self.result: ScannedInvoice | None = None

        self.picker = ft.FilePicker()
        page.services.append(self.picker)

        self.status = ft.Text("اختر صورة فاتورة المورد — يحلّلها مساعد شركة العمر ويستخرج الأصناف", size=14)
        self.busy = ft.ProgressRing(visible=False, width=22, height=22)
        self.preview_box = ft.Column()
        self.hints_box = ft.Column()
        self.rows_box = ft.Column(spacing=8)
        self.summary = ft.Text("", weight=ft.FontWeight.BOLD)

        self.btn_pick = ft.Button("📷 اختيار صورة الفاتورة", on_click=self.on_pick)
        self.btn_scan = ft.Button("🔍 مسح الفاتورة", on_click=self.on_scan, disabled=True)
        self.btn_add = ft.Button("➕ إضافة الأصناف لفاتورة الشراء", on_click=self.on_add, visible=False)

    # ---- بناء الواجهة ----------------------------------------------------------------------
    def build(self) -> ft.Control:
        return ft.Column(
            [
                ft.Text("مسح فاتورة شراء", size=20, weight=ft.FontWeight.BOLD),
                ft.Row([self.btn_pick, self.btn_scan, self.busy], wrap=True),
                self.status,
                self.hints_box,
                self.preview_box,
                self.summary,
                self.rows_box,
                self.btn_add,
            ],
            scroll=w.smooth_scroll(), spacing=12, expand=True,
        )

    def _busy(self, on: bool, msg: str = "") -> None:
        self.busy.visible = on
        self.btn_pick.disabled = on
        self.btn_scan.disabled = on or self.image_bytes is None
        if msg:
            self.status.value = msg
        self.page.update()

    # ---- 1) اختيار الصورة + معاينة الجودة ---------------------------------------------------------
    async def on_pick(self, _):
        files = await self.picker.pick_files(file_type=ft.FilePickerFileType.IMAGE, with_data=True)
        if not files:
            return
        f = files[0]
        data = getattr(f, "bytes", None)
        if not data and getattr(f, "path", None):  # الجوال/سطح المكتب: تُقرأ من المسار
            data = await _read_file(f.path)
        if not data:
            self.status.value = "تعذّر قراءة الصورة"
            self.page.update()
            return
        self.image_bytes, self.result = data, None
        self.rows_box.controls.clear()
        self.btn_add.visible = False
        self.summary.value = ""
        self._busy(True, "جارٍ تجهيز الصورة…")
        try:
            png, quality = await self.scanner.preview(data)
        except ImageError as e:
            self.image_bytes = None
            self._busy(False, f"⚠ {e}")
            return
        self.preview_box.controls = [ft.Text("الصورة التي سيحلّلها المساعد:", size=12), _image(png)]
        self.hints_box.controls = [
            ft.Container(ft.Text("⚠ " + h), bgcolor=AMBER, padding=8, border_radius=8) for h in quality.hints
        ] or [ft.Text("✓ جودة الصورة جيدة" + ("، وتم اكتشاف حدود الورقة" if quality.document_found else ""))]
        self._busy(False, "اضغط «مسح الفاتورة» ليستخرج المساعد الأصناف")

    # ---- 2) المسح -------------------------------------------------------------------------------
    async def on_scan(self, _):
        if not self.image_bytes:
            return
        self._busy(True, "مساعد شركة العمر يقرأ الفاتورة… (5–20 ثانية)")
        try:
            res = await self.scanner.scan(self.image_bytes)
        except OcrUnavailable as e:
            self._busy(False, f"⚠ {e}")
            return
        except ImageError as e:
            self._busy(False, f"⚠ {e}")
            return
        except Exception as e:  # noqa: BLE001 — لا نترك الواجهة معلّقة على دوّار التحميل
            self._busy(False, f"⚠ فشل المسح: {e}")
            return
        self.result = res.invoice
        self._render_rows()
        n_review = sum(r.needs_review for r in res.invoice.rows)
        self._busy(False, f"تم: {len(res.invoice.rows)} سطر، منها {n_review} يحتاج مراجعة · {res.engine}")

    # ---- 3) جدول المراجعة ---------------------------------------------------------------------------
    def _render_rows(self) -> None:
        inv = self.result
        self.rows_box.controls.clear()
        if inv is None:
            return
        head = [f"رقم فاتورة المورد: {inv.invoice_number}"] if inv.invoice_number else []
        if inv.supplier:
            head.append(f"المورد: {inv.supplier}")
        if inv.date:
            head.append(f"التاريخ: {inv.date}")
        for w in inv.warnings:
            self.rows_box.controls.append(ft.Container(ft.Text("⚠ " + w), bgcolor=AMBER, padding=8, border_radius=8))
        if head:
            self.rows_box.controls.append(ft.Text(" · ".join(head), size=12))
        for row in inv.rows:
            self.rows_box.controls.append(self._row_card(row))
        self._update_summary()
        self.btn_add.visible = bool(inv.rows)
        self.page.update()

    def _row_card(self, row: ScannedRow) -> ft.Control:
        def bind(field: str, parse: bool):
            def handler(e):
                if parse:
                    setattr(row, field, parse_num(e.control.value))
                else:
                    setattr(row, field, e.control.value)
                row.total = None if field in ("qty", "cost") else row.total
                row.issues = []          # المستخدم راجع السطر بنفسه
                row.confidence = 1.0
                card.bgcolor = GREEN
                self._update_summary()
                self.page.update()
            return handler

        def tf(label: str, value, field: str, parse: bool, width: int | None = None, kb=None):
            return ft.TextField(
                label=label, value="" if value is None else str(value), width=width, dense=True,
                keyboard_type=kb, on_change=bind(field, parse),
            )

        num = ft.KeyboardType.NUMBER
        controls: list[ft.Control] = [
            tf("الصنف", row.name, "name", False),
            ft.Row([tf("الكود", row.code, "code", False, 130),
                    tf("الكمية", row.qty, "qty", True, 90, num),
                    tf("التكلفة", row.cost, "cost", True, 110, num)], wrap=True),
        ]
        if row.issues:
            controls.append(ft.Text("؛ ".join(row.issues), size=12, color=ft.Colors.DEEP_ORANGE_800))
        card = ft.Container(ft.Column(controls, spacing=6), padding=10, border_radius=10,
                            bgcolor=AMBER if row.needs_review else GREEN)
        return card

    def _update_summary(self) -> None:
        if not self.result:
            return
        total = sum(((r.qty or 0) * (r.cost or 0) for r in self.result.rows), Decimal(0))
        stated = f" · مكتوب على الفاتورة: {fmt_money(self.result.stated_total)}" if self.result.stated_total else ""
        self.summary.value = f"المجموع المحسوب: {fmt_money(total)}{stated}"

    # ---- 4) الإضافة إلى مسودة الشراء -----------------------------------------------------------------
    async def on_add(self, _):
        if not self.result:
            return
        rows = self.result.rows
        pending_review = [r for r in rows if r.qty is None or r.cost is None or not (r.name or r.code)]
        if pending_review:
            self.status.value = f"⚠ {len(pending_review)} سطر ناقص (كمية/تكلفة/اسم) — أكمله أو احذفه قبل الإضافة"
            self.page.update()
            return
        res = self.ledger.apply_import(self.draft, [r.to_raw() for r in rows])
        if self.result.supplier and not self.draft.supplier:
            self.draft.supplier = self.result.supplier
        if self.result.invoice_number and not self.draft.supplier_invoice_number:
            self.draft.supplier_invoice_number = self.result.invoice_number
        self.status.value = (f"أُضيف {len(res.matched)} صنف مطابق للمخزون، و{len(res.pending)} صنف جديد بانتظار التسعير "
                             "— راجع شاشة الشراء ثم اضغط «تم»")
        self.btn_add.visible = False
        self.page.update()


async def _read_file(path: str) -> bytes | None:
    import asyncio
    try:
        return await asyncio.to_thread(Path(path).read_bytes)
    except OSError:
        return None
