"""تبويب التقارير (renderReportsTab): أكثر المنتجات مبيعاً + حركة صنف (بيع/شراء) مع بحث."""
from __future__ import annotations

import flet as ft

from ...core.money import fmt_money
from ...core.reports import item_movement, top_selling
from ...core.search import search_inventory
from ...printing.render import arabic_date
from .. import widgets as w


def build(app) -> ft.Control:
    led = app.ledger
    top = top_selling(led.invoices, 8)
    max_qty = max([t.qty for t in top] + [1])

    if top:
        bars = [ft.Column([
            ft.Row([ft.Text(t.name, expand=True, size=13), ft.Text(str(t.qty), weight=ft.FontWeight.BOLD, color=w.TEAL)]),
            ft.ProgressBar(value=max(0.04, t.qty / max_qty), color=w.TEAL, bgcolor=w.TINT)], spacing=3) for t in top]
    else:
        bars = [w.empty("لا توجد مبيعات بعد.")]

    movement_box, items_box = ft.Column(spacing=6), ft.Column(spacing=6)
    state = {"q": "", "id": app.sub.get("item")}

    def render_movement() -> None:
        it = led._item(state["id"]) if state["id"] else None
        if not it:
            movement_box.controls = [w.empty("اختر صنفاً من القائمة لعرض حركته.")]
            return
        rows = item_movement(it, led.invoices, led.purchases)
        lines: list[ft.Control] = [
            ft.Row([ft.Column([ft.Text(f"{m.kind}  — {m.ref}", size=14), w.muted(arabic_date(m.date))], expand=True, spacing=1),
                    ft.Text(("+" if m.qty > 0 else "") + str(m.qty), weight=ft.FontWeight.BOLD,
                            color=w.TEAL_DARK if m.qty > 0 else w.RED)]) for m in rows[:80]]
        movement_box.controls = [ft.Text(f"{it.name} — الكمية الحالية: {it.stock}", weight=ft.FontWeight.BOLD),
                                 *(lines or [w.empty("لا توجد حركة مسجلة لهذا الصنف.")])]

    def render_items() -> None:
        found = search_inventory(led.inventory, state["q"])[:25]
        out = []
        for it in found:
            def pick(_, it=it):
                state["id"] = it.id
                render_items(); render_movement(); app.page.update()
            sel = state["id"] == it.id
            out.append(w.card(ft.Column([ft.Text(it.name + (f" ({it.code})" if it.code else ""), weight=ft.FontWeight.BOLD, size=14),
                                         w.muted(f"الكمية: {it.stock}")], spacing=2),
                              bgcolor=w.TINT if sel else w.PANEL, padding=8, on_click=pick))
        items_box.controls = out or [w.empty("لا أصناف")]

    def on_search(q: str) -> None:
        state["q"] = q
        render_items(); app.page.update()

    render_items(); render_movement()
    return ft.Column([
        w.card(ft.Column([w.title("📊 أكثر المنتجات مبيعاً", 16), w.muted("أعلى 8 حسب الكمية المباعة"), *bars], spacing=8)),
        w.card(ft.Column([w.title("📦 حركة صنف", 16), w.search_bar(app, on_search, "ابحث عن صنف…"), items_box,
                          ft.Divider(), movement_box], spacing=8))],
        scroll=w.smooth_scroll(), expand=True, spacing=10)
