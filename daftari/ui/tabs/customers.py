"""تبويب العملاء (renderCustomersTab): إضافة، بحث، كشف حساب، سند قبض، دين يدوي، تعديل، حذف."""
from __future__ import annotations

from decimal import Decimal

import flet as ft

from ...core.money import fmt_money, fmt_num, parse_num
from ...core.reports import customer_statement, invoice_count_for
from ...core.search import fuzzy_match
from ...ledger import LedgerError
from ...printing.render import customer_statement_doc, voucher_doc
from .. import widgets as w


def build(app) -> ft.Control:
    if app.sub.get("detail"):
        from . import party
        return party.build_customer(app, app.sub["detail"])
    led = app.ledger
    total_owed = sum((c.balance for c in led.customers if c.balance > 0), Decimal(0))
    debtors = [c for c in led.customers if c.balance > 0]
    list_box = ft.Column(spacing=8)
    count = w.muted("")
    state = {"q": "", "debtors": False}

    name_tf, phone_tf = w.field("اسم الزبون", "", expand=True), w.field("رقم الهاتف", "", expand=True)

    async def add(_):
        try:
            await led.add_customer(name_tf.value or "", phone_tf.value or "")
        except LedgerError as e:
            w.toast(app.page, str(e), error=True)
            return
        await app.after_change()

    def render_list() -> None:
        q = state["q"]
        pool = sorted(led.customers, key=lambda c: (-c.balance, c.name))        # الأكثر دَيناً أولاً
        shown = [c for c in pool if (not q or fuzzy_match(c.name, q) or fuzzy_match(c.phone, q))
                 and (not state["debtors"] or c.balance > 0)]
        count.value = f"{len(shown)} من {len(led.customers)}"
        list_box.controls = [_card(app, c) for c in shown[:150]] or [w.empty("لا يوجد عملاء." if not led.customers else "لا نتائج")]
        if len(shown) > 150:
            list_box.controls.append(w.muted(f"يُعرض أول 150 من {len(shown)} — ضيّق البحث"))

    def on_search(q: str) -> None:
        state["q"] = q
        render_list()
        app.page.update()

    def toggle_debtors(e) -> None:
        state["debtors"] = bool(e.control.value)
        render_list()
        app.page.update()

    render_list()
    return ft.Column([
        w.page_header("العملاء", f"{len(led.customers)} زبون · {len(debtors)} عليهم ديون"),
        ft.Row([w.kpi("إجمالي الديون على العملاء", fmt_money(total_owed), w.RED if total_owed > 0 else w.INK,
                      ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, w.RED_TINT if total_owed > 0 else w.TINT),
                w.kpi("عملاء مدينون", str(len(debtors)), w.AMBER, ft.Icons.PEOPLE_OUTLINE, w.AMBER_TINT),
                w.kpi("إجمالي العملاء", str(len(led.customers)), w.TEAL, ft.Icons.GROUP_OUTLINED, w.TINT)], spacing=10),
        w.card(ft.Column([ft.Row([w.icon_tile(ft.Icons.PERSON_ADD_ALT_1_OUTLINED, w.TEAL, w.TINT, 32),
                                  w.title("إضافة زبون جديد", 15)], spacing=10),
                          ft.Row([name_tf, phone_tf, w.btn("+ إضافة", add)], spacing=8,
                                 vertical_alignment=ft.CrossAxisAlignment.CENTER)], spacing=10)),
        ft.Row([ft.Container(w.search_bar(app, on_search, "ابحث بالاسم أو الرقم…", barcode=False), expand=True),
                ft.Switch(label="المدينون فقط", value=False, on_change=toggle_debtors, active_color=w.TEAL)],
               vertical_alignment=ft.CrossAxisAlignment.CENTER),
        count, list_box],
        scroll=w.smooth_scroll(), expand=True, spacing=12)


def _card(app, c) -> ft.Control:
    led = app.ledger

    def statement(_):
        async def show(limit):
            st = customer_statement(c, led.invoices, oldest_first=True, limit=limit)
            await app.show_document(customer_statement_doc(st, led.settings, limit), f"كشف-{c.name}", back=("customers", {}))
        w.choice_dialog(app, f"كشف حساب {c.name}", [
            ("كشف كامل", lambda: show(None)), ("آخر 5 فواتير", lambda: show(5)),
            ("آخر 10 فواتير", lambda: show(10)), ("آخر 20 فاتورة", lambda: show(20))])

    def debt(_):
        async def submit(v):
            await led.add_manual_customer_debt(c.id, v["amount"], v["note"])
        w.form_dialog(app, "إضافة دين يدوي", [("amount", "المبلغ", "", "num"), ("note", "ملاحظة (اختياري)", "", "text")],
                      submit, "إضافة", c.name)

    def pay(_):
        box: dict = {}

        async def submit(v):
            box["v"] = await led.record_customer_payment(c.id, v["amount"])

            async def show():
                await app.show_document(voucher_doc(box["v"], led.settings), box["v"].number, back=("customers", {}))
            app.run_after_dialog(show)
        w.form_dialog(app, "سند قبض", [("amount", "المبلغ المقبوض", "", "num")], submit, "تسجيل",
                      f"{c.name} — الرصيد الحالي {fmt_money(c.balance)}")

    def edit(_):
        async def submit(v):
            await led.edit_customer(c.id, v["name"], v["phone"], v["balance"])
        w.form_dialog(app, "تعديل الزبون", [("name", "الاسم", c.name, "text"), ("phone", "الهاتف", c.phone, "text"),
                                            ("balance", "الرصيد (الدين)", fmt_num(c.balance), "num")], submit)

    def delete(_):
        w.confirm_dialog(app, "حذف الزبون", f"حذف «{c.name}» نهائياً؟ (فواتيره السابقة تبقى بالسجل)",
                         lambda: led.delete_customer(c.id), "حذف", True)

    async def summary(_):
        await app.go("customers", detail=c.id)

    btns = [w.btn("ملخص", summary, "success", small=True, icon=ft.Icons.DONUT_LARGE),
            w.btn("كشف حساب", statement, "ghost", small=True), w.btn("+ دين", debt, "danger", small=True)]
    if c.balance > 0:
        btns.append(w.btn("سند قبض", pay, small=True))
    btns += [w.btn("تعديل", edit, "ghost", small=True), w.btn("حذف", delete, "danger", small=True)]
    n_inv = invoice_count_for(c.name, led.invoices)
    balance_pill = (w.pill(f"عليه {fmt_money(c.balance)}", "red") if c.balance > 0 else w.pill("لا دين ✓", "green"))
    return w.row_card(ft.Column([
        ft.Row([w.avatar(c.name),
                ft.Column([ft.Text(c.name, weight=ft.FontWeight.W_700, size=14),
                           w.muted(f"{c.phone or 'بدون هاتف'} · {n_inv} فاتورة", 12)], spacing=1, expand=True),
                balance_pill], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12),
        ft.Row(btns, wrap=True, spacing=6, run_spacing=6)], spacing=10), on_click=summary)
