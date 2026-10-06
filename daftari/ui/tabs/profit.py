"""تبويب الأرباح (renderProfitTab + renderProfitDayView)."""
from __future__ import annotations

from datetime import datetime

import flet as ft

from ...core.calc import _parse_dt, invoice_cost, invoice_revenue
from ...core.money import fmt_money, normalize_digits
from ...core.reports import invoices_on_day
from ...core.search import fuzzy_match
from ...printing.render import _MONTHS, arabic_date, invoice_doc
from .. import widgets as w

_WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def _month_label(key: str) -> str:
    y, m = key.split("-")
    return f"{_MONTHS[int(m) - 1]} {y}"


def _day_label(key: str) -> str:
    d = datetime.strptime(key, "%Y-%m-%d")
    return f"{_WEEKDAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]} {d.year}"


def _signed(v, good=w.TEAL_DARK) -> str:
    return good if v >= 0 else w.RED


def build(app) -> ft.Control:
    led = app.ledger
    if "day" in app.sub:
        return _day_view(app, app.sub["day"])
    if not led.invoices and not led.expenses:
        return w.card(w.empty("لا توجد فواتير أو مصاريف بعد لحساب الأرباح."))

    rep = led.profit()
    stats = ft.Column([
        ft.Row([w.stat("مبيعات اليوم", fmt_money(rep.today_revenue)),
                w.stat("أرباح اليوم", fmt_money(rep.today_profit), _signed(rep.today_profit))], spacing=8),
        ft.Row([w.stat("المبيعات كلها", fmt_money(rep.total_revenue)),
                w.stat("إجمالي رأس المال", fmt_money(rep.total_cost))], spacing=8),
        ft.Row([w.stat("إجمالي المصاريف", fmt_money(rep.total_expenses)),
                w.stat("صافي الربح (بعد المصاريف)", fmt_money(rep.total_net_profit), _signed(rep.total_net_profit))], spacing=8),
    ], spacing=8)

    # ---- الأشهر مع شريط نسبي ----
    keys = sorted(rep.months, reverse=True)
    max_net = max([abs(rep.months[k].net_profit) for k in keys] + [1])
    month_rows: list[ft.Control] = []
    for k in keys:
        m = rep.months[k]
        frac = max(0.0, min(1.0, float(abs(m.net_profit) / max_net)))
        month_rows.append(w.card(ft.Column([
            ft.Row([ft.Text(_month_label(k), weight=ft.FontWeight.BOLD, expand=True),
                    ft.Text(fmt_money(m.net_profit), color=_signed(m.net_profit), weight=ft.FontWeight.BOLD)]),
            ft.ProgressBar(value=frac, color=w.TEAL if m.net_profit >= 0 else w.RED, bgcolor=w.TINT),
            w.muted(f"مبيعات: {fmt_money(m.revenue)} · تكلفة بضاعة: {fmt_money(m.cost)} · مصاريف: {fmt_money(m.expenses)}")],
            spacing=6)))

    # ---- الأيام: بحث + فلتر تاريخ ----
    day_box = ft.Column(spacing=8)
    state = {"q": "", "date": ""}

    def render_days() -> None:
        dk_sorted = sorted((k for k, b in rep.days.items()), reverse=True)
        out: list[ft.Control] = []
        for dk in dk_sorted:
            if state["date"] and dk != state["date"]:
                continue
            if state["q"] and not fuzzy_match(_day_label(dk), state["q"]):
                continue
            b = rep.days[dk]

            async def open_day(_, dk=dk):
                await app.go("profit", day=dk)
            out.append(w.card(ft.Column([
                ft.Text(_day_label(dk), weight=ft.FontWeight.BOLD),
                ft.Row([ft.Text(f"{b.count} فاتورة"), ft.Text(f"مبيعات {fmt_money(b.revenue)}"),
                        ft.Text(f"ربح {fmt_money(b.profit)}", color=_signed(b.profit), weight=ft.FontWeight.BOLD)],
                       wrap=True, spacing=12),
                w.btn("👁 عرض فواتير اليوم", open_day)], spacing=6)))
        day_box.controls = out[:90] or [w.empty("لا أيام مطابقة")]

    def on_q(e) -> None:
        state["q"] = e.control.value or ""; render_days(); app.page.update()

    def on_date(e) -> None:
        v = normalize_digits(e.control.value or "").strip()
        state["date"] = v if len(v) == 10 else ""
        render_days(); app.page.update()

    render_days()
    return ft.Column([
        stats, w.title("الأشهر", 16), *month_rows, w.title("الأيام", 16),
        ft.Row([w.field("بحث باليوم…", "", on_change=on_q, expand=True),
                w.field("تاريخ (2026-09-28)", "", on_change=on_date, width=170)]),
        day_box], scroll=w.smooth_scroll(), expand=True, spacing=10)


def _day_view(app, day_key: str) -> ft.Control:
    led = app.ledger
    invs = invoices_on_day(led.invoices, day_key)

    async def back(_):
        await app.go("profit")

    rev = sum((invoice_revenue(i) for i in invs), 0)
    cost = sum((invoice_cost(i) for i in invs), 0)
    cards: list[ft.Control] = []
    for inv in invs:
        r, c = invoice_revenue(inv), invoice_cost(inv)

        async def view(_, inv=inv):
            await app.show_document(invoice_doc(inv, led.settings, app.line_images(inv)), inv.number, back=("profit", {"day": day_key}))
        cards.append(w.card(ft.Column([
            ft.Row([ft.Text(inv.number, weight=ft.FontWeight.BOLD, expand=True), ft.Text(inv.customer or "—")]),
            ft.Row([ft.Text(f"إجمالي {fmt_money(r)}"), ft.Text(f"تكلفة {fmt_money(c)}"),
                    ft.Text(f"ربح {fmt_money(r - c)}", color=_signed(r - c), weight=ft.FontWeight.BOLD)], wrap=True, spacing=12),
            w.btn("👁 عرض", view, "ghost")], spacing=6)))
    return ft.Column([
        ft.Row([w.btn("رجوع", back, "ghost"), w.title(_day_label(day_key), 16)]),
        ft.Row([w.stat("المبيعات", fmt_money(rev)), w.stat("الربح", fmt_money(rev - cost), _signed(rev - cost))], spacing=8),
        *(cards or [w.empty("لا فواتير بهذا اليوم")])], scroll=w.smooth_scroll(), expand=True, spacing=10)
