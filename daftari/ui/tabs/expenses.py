"""تبويب المصاريف (renderExpensesTab): إضافة مصروف بنوع وفترة، وتجميع حسب الشهر مع تفصيل الأنواع."""
from __future__ import annotations

from collections import defaultdict

import flet as ft

from ...core.calc import _parse_dt
from ...core.models import EXPENSE_CATEGORIES
from ...core.money import fmt_money
from ...ledger import LedgerError
from ...printing.render import _MONTHS, arabic_date
from .. import widgets as w

PERIODS = ["شهري", "أسبوعي", "سنوي"]


def build(app) -> ft.Control:
    led = app.ledger
    state = {"cat": "rent", "period": ""}
    cat_row = ft.Row(wrap=True, spacing=6)
    amount_tf, note_tf = w.field("المبلغ", "", kind="num", expand=True), w.field("ملاحظة (اختياري)", "", expand=True)
    period_tf = w.field("الفترة (اختياري)", "", width=180)

    def render_cats() -> None:
        cat_row.controls = [ft.Container(
            ft.Text(label, size=13, color="#FFFFFF" if state["cat"] == key else w.SOFT),
            bgcolor=w.TEAL if state["cat"] == key else w.TINT, padding=8, border_radius=8,
            on_click=lambda e, k=key: pick(k)) for key, label in EXPENSE_CATEGORIES.items()]

    def pick(key: str) -> None:
        state["cat"] = key
        render_cats()
        app.page.update()

    def pick_period(p: str) -> None:
        period_tf.value = p
        app.page.update()

    async def add(_):
        try:
            await led.add_expense(state["cat"], amount_tf.value or 0, note_tf.value or "", period_tf.value or "")
        except LedgerError as e:
            w.toast(app.page, str(e), error=True)
            return
        await app.after_change()

    render_cats()
    form = w.card(ft.Column([
        w.title("إضافة مصروف", 15), cat_row, ft.Row([amount_tf, note_tf]),
        ft.Row([period_tf, *[ft.TextButton(p, on_click=lambda e, p=p: pick_period(p)) for p in PERIODS]], wrap=True),
        w.btn("+ إضافة", add)], spacing=8))

    # ---- تجميع حسب الشهر ----
    groups: dict[str, dict] = defaultdict(lambda: {"total": 0, "cats": defaultdict(int), "items": []})
    grand = 0
    for ex in led.expenses:
        k = _parse_dt(ex.date).strftime("%Y-%m")
        g = groups[k]
        g["total"] += ex.amount
        g["cats"][ex.category] += ex.amount
        g["items"].append(ex)
        grand += ex.amount

    month_cards: list[ft.Control] = []
    for k in sorted(groups, reverse=True):
        g = groups[k]
        y, m = k.split("-")
        breakdown = " · ".join(f"{EXPENSE_CATEGORIES.get(c, c)}: {fmt_money(v)}" for c, v in g["cats"].items())
        rows = []
        for ex in g["items"]:
            def delete(_, ex=ex):
                w.confirm_dialog(app, "حذف المصروف", "حذف هذا المصروف؟", lambda: led.delete_expense(ex.id), "حذف", True)
            label = EXPENSE_CATEGORIES.get(ex.category, ex.category) + (f" — {ex.note}" if ex.note else "")
            meta = arabic_date(ex.date) + (f" · الفترة: {ex.period}" if ex.period else "")
            rows.append(ft.Row([ft.Column([ft.Text(label, size=14), w.muted(meta)], expand=True, spacing=1),
                                ft.Text(fmt_money(ex.amount), weight=ft.FontWeight.BOLD, color=w.RED),
                                ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, on_click=delete)]))
        month_cards.append(w.card(ft.Column([
            ft.Row([ft.Text(f"{_MONTHS[int(m) - 1]} {y}", weight=ft.FontWeight.BOLD, expand=True),
                    ft.Text(fmt_money(g["total"]), weight=ft.FontWeight.BOLD, color=w.RED)]),
            w.muted(breakdown), ft.Divider(height=1), *rows], spacing=6)))

    return ft.Column([
        ft.Row([w.stat("إجمالي المصاريف (كل الفترة)", fmt_money(grand), w.RED)]), form,
        w.title("المصاريف حسب الشهر", 16), *(month_cards or [w.empty("لا توجد مصاريف بعد")])],
        scroll=w.smooth_scroll(), expand=True, spacing=10)
