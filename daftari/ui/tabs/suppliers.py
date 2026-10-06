"""تبويب الموردين (renderSuppliersTab): الدين علينا، إضافة مورد، كشف حساب، تسجيل دفعة، تعديل، حذف."""
from __future__ import annotations

import flet as ft

from ...core.money import fmt_money, fmt_num
from ...core.reports import purchase_count_for, supplier_statement
from ...ledger import LedgerError
from ...printing.render import supplier_statement_doc, voucher_doc
from .. import widgets as w


def build(app) -> ft.Control:
    if app.sub.get("detail"):
        from . import party
        return party.build_supplier(app, app.sub["detail"])
    led = app.ledger
    total = sum((s.balance for s in led.suppliers if s.type == "we_owe"), 0)
    name_tf, amount_tf = w.field("اسم المورد", "", expand=True), w.field("الرصيد الافتتاحي (دين علينا)", "0", kind="num", expand=True)

    async def add(_):
        try:
            await led.add_supplier(name_tf.value or "", amount_tf.value or 0)
        except LedgerError as e:
            w.toast(app.page, str(e), error=True)
            return
        await app.after_change()

    cards = [_card(app, s) for s in led.suppliers if s.type == "we_owe"]
    return ft.Column([
        ft.Row([w.stat("إجمالي الدين علينا (لهم)", fmt_money(total), w.RED if total > 0 else w.INK)]),
        w.card(ft.Column([w.title("إضافة مورد", 15), ft.Row([name_tf, amount_tf]), w.btn("+ إضافة", add)], spacing=6)),
        *(cards or [w.empty("لا يوجد موردون بعد.")])], scroll=w.smooth_scroll(), expand=True, spacing=10)


def _card(app, s) -> ft.Control:
    led = app.ledger

    async def statement(_):
        st = supplier_statement(s, led.purchases, oldest_first=True)
        await app.show_document(supplier_statement_doc(st, led.settings), f"كشف-{s.name}", back=("suppliers", {}))

    def pay(_):
        box: dict = {}

        async def submit(v):
            box["v"] = await led.record_supplier_payment(s.id, v["amount"])

            async def show():
                await app.show_document(voucher_doc(box["v"], led.settings), box["v"].number, back=("suppliers", {}))
            app.run_after_dialog(show)
        w.form_dialog(app, "تسجيل دفعة لمورد (سند دفع)", [("amount", "المبلغ المدفوع", "", "num")], submit, "تسجيل",
                      f"{s.name} — الرصيد الحالي {fmt_money(s.balance)}")

    def edit(_):
        async def submit(v):
            await led.edit_supplier(s.id, v["name"], v["balance"])
        w.form_dialog(app, "تعديل المورد", [("name", "الاسم", s.name, "text"),
                                            ("balance", "الرصيد (دين علينا)", fmt_num(s.balance), "num")], submit)

    def delete(_):
        w.confirm_dialog(app, "حذف المورد", f"حذف «{s.name}» نهائياً؟", lambda: led.delete_supplier(s.id), "حذف", True)

    async def summary(_):
        await app.go("suppliers", detail=s.id)

    return w.card(ft.Column([
        ft.Row([w.avatar(s.name, 40), ft.Text(s.name, weight=ft.FontWeight.BOLD, expand=True),
                ft.Text(fmt_money(s.balance), color=w.RED if s.balance > 0 else w.TEAL_DARK, weight=ft.FontWeight.BOLD)]),
        w.muted(f"{purchase_count_for(s.name, led.purchases)} فاتورة شراء"),
        ft.Row([w.btn("ملخص", summary, "success", icon=ft.Icons.DONUT_LARGE), w.btn("كشف حساب", statement, "ghost"),
                w.btn("تسجيل دفعة", pay), w.btn("تعديل", edit, "ghost"),
                w.btn("حذف", delete, "danger")], wrap=True, spacing=6)], spacing=6), on_click=summary)
