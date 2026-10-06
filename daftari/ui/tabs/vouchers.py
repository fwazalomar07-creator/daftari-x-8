"""تبويب السندات (renderVouchersTab): سند قبض، سند دفع، سجل السندات مع بحث/طباعة/حذف."""
from __future__ import annotations

import flet as ft

from ...core.money import fmt_money
from ...core.search import fuzzy_match
from ...ledger import LedgerError
from ...printing.render import arabic_date, voucher_doc
from .. import widgets as w


def build(app) -> ft.Control:
    led = app.ledger
    list_box = ft.Column(spacing=8)
    count = w.muted("")
    state = {"q": ""}

    def new_voucher(vtype: str):
        is_r = vtype == "receipt"

        def open_form(_):
            box: dict = {}

            def suggest(text: str):
                t = (text or "").strip()
                if not t:
                    return [], ""
                acc = led.match_account(vtype, t)
                near = [n for n, _ in led.suggest_accounts(vtype, t) if not acc or n != acc.name]
                who = "العميل" if is_r else "المورد"
                if acc:
                    line = f"✓ {who} «{acc.name}» — الرصيد الحالي {fmt_money(acc.balance)}: سيُخصم المبلغ من رصيده تلقائياً"
                    return near, line
                if near:
                    return near, f"لا يوجد {who} بهذا الاسم تماماً — اختر من الأسماء المقاربة ليُخصم من رصيده"
                return [], f"لا يطابق أي {who}: سيُسجَّل السند باسم الشخص دون خصم من أي رصيد"

            async def submit(v):
                person = (v["person"] or "").strip()
                if person and not led.match_account(vtype, person) and led.suggest_accounts(vtype, person) \
                        and not box.get("warned"):
                    box["warned"] = True       # أول مرة: نوقف ونعرض التحذير؛ الضغط ثانية يسجّل بلا خصم
                    raise LedgerError("الاسم لا يطابق حساباً بالضبط — اختر أحد الأسماء المقاربة أسفل الحقل ليُخصم من رصيده، "
                                      "أو اضغط «تسجيل» مرة أخرى لتسجيل السند بلا خصم من أي رصيد")
                box["v"] = await led.add_voucher(vtype, v["person"], v["amount"], v["note"], v["ref"], v["prev"])

                async def show():
                    await app.show_document(voucher_doc(box["v"], led.settings), box["v"].number, back=("vouchers", {}))
                app.run_after_dialog(show)
            w.form_dialog(app, "🧾 سند قبض — استلام مبلغ" if is_r else "🧾 سند دفع — دفع مبلغ", [
                ("person", "الاسم (عميل/مورد أو شخص)", "", "text"), ("amount", "المبلغ", "", "num"),
                ("ref", "رقم إضافي (اختياري)", "", "text"), ("prev", "الرصيد السابق (اختياري)", "", "num"),
                ("note", "ملاحظة (اختياري)", "", "text")], submit, "تسجيل",
                "إن طابق الاسم عميلاً/مورداً موجوداً يُخصم المبلغ من رصيده تلقائياً",
                suggest={"person": suggest})
        return open_form

    def render_list() -> None:
        q = state["q"]
        shown = [v for v in led.vouchers if not q or fuzzy_match(v.number, q) or fuzzy_match(v.person, q)]
        count.value = f"{len(shown)} من {len(led.vouchers)}"
        list_box.controls = [_card(app, v) for v in shown[:120]] or [w.empty("لا توجد سندات")]

    def on_search(q: str) -> None:
        state["q"] = q
        render_list()
        app.page.update()

    render_list()
    return ft.Column([
        ft.Row([w.btn("🧾 سند قبض", new_voucher("receipt"), expand=True), w.btn("🧾 سند دفع", new_voucher("payment"), "amber", expand=True)]),
        w.title("سجل السندات", 16), count,
        w.search_bar(app, on_search, "ابحث برقم السند أو الاسم…", barcode=False), list_box],
        scroll=w.smooth_scroll(), expand=True, spacing=10)


def _card(app, v) -> ft.Control:
    led = app.ledger
    is_r = v.type == "receipt"

    async def view(_):
        await app.show_document(voucher_doc(v, led.settings), v.number, back=("vouchers", {}))

    def delete(_):
        extra = " وسيُرجَع المبلغ إلى رصيد صاحب الحساب." if v.applied_to_id else ""
        w.confirm_dialog(app, "حذف السند", f"حذف السند {v.number}؟{extra}", lambda: led.delete_voucher(v.id), "حذف", True)

    return w.card(ft.Column([
        ft.Row([ft.Text(v.number, weight=ft.FontWeight.BOLD, expand=True),
                ft.Text(("+" if is_r else "-") + fmt_money(v.amount), color=w.GREEN if is_r else w.RED, weight=ft.FontWeight.BOLD)]),
        w.muted(f"{v.person} · {arabic_date(v.date)}" + (f" · {v.note}" if v.note else "")),
        ft.Row([w.btn("🖨 عرض / طباعة", view, "ghost"), w.btn("حذف", delete, "danger")], spacing=6)], spacing=6))
