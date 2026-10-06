"""تبويب سجل الفواتير (renderHistoryTab) + عرض/تعديل/حذف + حالة الدفع اليدوية."""
from __future__ import annotations

from decimal import Decimal

import flet as ft

from ...core.reports import invoice_profit_value, is_invoice_paid
from ...core.money import fmt_money
from ...core.search import fuzzy_match
from ...ledger import LedgerError
from ...printing.render import arabic_date, invoice_doc
from .. import widgets as w


def build(app) -> ft.Control:
    led = app.ledger
    if not led.invoices:
        return w.card(w.empty("لا توجد فواتير سابقة بعد."))
    list_box = w.LazyList()          # ListView كسول (مئات الفواتير بلا تقطيع)
    count = w.muted("")
    state = {"q": ""}

    def render_list() -> None:
        q = state["q"]
        shown = [i for i in led.invoices if not q or fuzzy_match(i.number, q) or fuzzy_match(i.customer, q)]
        count.value = f"{len(shown)} من {len(led.invoices)}"
        list_box.controls = [_card(app, inv) for inv in shown[:120]] or [w.empty("ما في فواتير مطابقة للبحث.")]
        if len(shown) > 120:
            list_box.controls.append(w.muted(f"يُعرض أحدث 120 فاتورة — ضيّق البحث ({len(shown)} مطابقة)"))
        list_box.sync()

    def on_search(q: str) -> None:
        state["q"] = q
        render_list()
        app.page.update()

    render_list()
    total = sum((i.total for i in led.invoices), Decimal(0))
    unpaid = [i for i in led.invoices if not is_invoice_paid(i)]
    list_box.fixed.extend([
        w.page_header("سجل الفواتير", f"{len(led.invoices)} فاتورة"),
        ft.Row([w.kpi("إجمالي المبيعات", fmt_money(total), w.TEAL, ft.Icons.POINT_OF_SALE, w.TINT),
                w.kpi("فواتير غير مدفوعة", str(len(unpaid)), w.RED if unpaid else w.INK, ft.Icons.PENDING_ACTIONS,
                      w.RED_TINT if unpaid else w.SLATE_TINT)], spacing=10),
        w.search_bar(app, on_search, "ابحث برقم الفاتورة أو اسم الزبون…", barcode=False), count])
    list_box.sync()
    return list_box.view


def _card(app, inv) -> ft.Control:
    led = app.ledger
    profit = invoice_profit_value(inv)
    paid = is_invoice_paid(inv)

    async def view(_):
        await app.show_document(invoice_doc(inv, led.settings, app.line_images(inv)), inv.number, back=("history", {}))

    async def edit(_):
        d = app.invoice_draft
        if d.cart or d.custom_lines:
            w.toast(app.page, "في فاتورة قيد العمل بتبويب «فاتورة» — أنهِها أو فرّغها أولاً", error=True)
            return
        try:
            draft, warnings = await led.begin_edit_invoice(inv.id)
        except LedgerError as e:
            w.toast(app.page, str(e), error=True)
            return
        app.invoice_draft = draft
        for msg in warnings:
            w.toast(app.page, "⚠ " + msg)
        w.toast(app.page, f"تعديل الفاتورة {inv.number} — اضغط «تم» لحفظ التعديلات")
        await app.go("invoice")

    def delete(_):
        w.confirm_dialog(app, "حذف الفاتورة",
                         f"حذف الفاتورة {inv.number}؟ سيُرجَع المخزون وتُلغى الديون المرتبطة بها.",
                         lambda: led.delete_invoice(inv.id), "حذف", True)

    def status(_):
        w.choice_dialog(app, f"حالة دفع الفاتورة {inv.number}", [
            ("✔ مدفوعة", lambda: led.set_invoice_payment_status(inv.id, "paid")),
            ("✖ غير مدفوعة", lambda: led.set_invoice_payment_status(inv.id, "unpaid"))],
            "تحديد يدوي يغلب الحساب التلقائي من المتبقي")

    badge = w.pill("مدفوعة" if paid else "غير مدفوعة", "green" if paid else "red", on_click=status)
    name = inv.customer or "زبون نقدي"
    return w.row_card(ft.Column([
        ft.Row([w.avatar(name, 40),
                ft.Column([ft.Row([ft.Text(inv.number, weight=ft.FontWeight.W_700, size=14),
                                   *([w.pill("معدّلة", "gray")] if inv.edited_at else [])], spacing=6),
                           w.muted(f"{name} · {arabic_date(inv.date)}", 12)], spacing=1, expand=True),
                ft.Column([ft.Text(fmt_money(inv.total), weight=ft.FontWeight.W_800, size=16, color=w.INK),
                           badge], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.END)],
               vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12),
        ft.Row([w.pill(f"الربح {fmt_money(profit)}", "green" if profit >= 0 else "red"),
                *([w.pill(f"متبقي {fmt_money(inv.remaining)}", "amber")] if inv.remaining > 0 else [])], spacing=6),
        ft.Row([w.btn("👁 عرض / طباعة", view, small=True), w.btn("تعديل", edit, "ghost", small=True),
                w.btn("حذف", delete, "danger", small=True)], wrap=True, spacing=6, run_spacing=6)], spacing=10))
