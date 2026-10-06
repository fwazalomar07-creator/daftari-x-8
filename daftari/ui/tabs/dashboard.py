"""لوحة التحكم الرئيسية — نظرة شاملة على النشاط (على نمط لوحات التحكم المالية الحديثة).

تجمع: مبيعات وأرباح اليوم، رأس المال، الديون، مخطط آخر ٧ أيام، وآخر الفواتير.
كل الأرقام تُحسب من بيانات Ledger مباشرة — لا تُخزَّن ولا تُعدَّل هنا.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import flet as ft

from ...core.calc import _parse_dt, invoice_revenue
from ...core.money import fmt_money
from ...core.reports import invoice_profit_value, invoices_on_day, is_invoice_paid, low_stock_items, top_selling
from ...printing.render import _MONTHS
from .. import widgets as w

_WEEKDAYS = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def _go(app, tab: str):
    return lambda _: app.page.run_task(app.go, tab)


def _today_label() -> str:
    d = datetime.now()
    return f"{_WEEKDAYS[d.weekday()]}، {d.day} {_MONTHS[d.month - 1]} {d.year}"


def _card_title(text: str, right: ft.Control | None = None) -> ft.Control:
    return ft.Row([ft.Text(text, size=15, weight=ft.FontWeight.W_800, color=w.INK, expand=True),
                   *([right] if right else [])], vertical_alignment=ft.CrossAxisAlignment.CENTER)


def _kpis(app, phone: bool) -> ft.Control:
    """ثلاث بطاقات رئيسية: مبيعات اليوم، ربح اليوم، فواتير اليوم (+ رأس المال والديون)."""
    led = app.ledger
    rep = led.profit()
    today = datetime.now().strftime("%Y-%m-%d")
    n_today = len(invoices_on_day(led.invoices, today))
    owed_to_us = sum((c.balance for c in led.customers if c.balance > 0), Decimal(0))
    we_owe = sum((s.balance for s in led.suppliers if getattr(s, "type", "we_owe") == "we_owe" and s.balance > 0),
                 Decimal(0))
    low = low_stock_items(led.inventory)
    main = [
        w.stat_card("مبيعات اليوم", fmt_money(rep.today_revenue), ft.Icons.POINT_OF_SALE, accent=w.TEAL, tint=w.TINT,
                    hint=f"{fmt_money(rep.total_revenue)} إجمالي المبيعات", on_click=_go(app, "profit"), expand=True),
        w.stat_card("ربح اليوم", fmt_money(rep.today_profit), ft.Icons.TRENDING_UP, accent=w.TEAL, tint=w.TINT,
                    hint=f"{fmt_money(rep.total_net_profit)} صافي الربح الكلي", on_click=_go(app, "profit"), expand=True),
        w.stat_card("فواتير اليوم", str(n_today), ft.Icons.RECEIPT_LONG_OUTLINED, accent=w.TEAL, tint=w.TINT,
                    hint=f"{len(led.invoices)} فاتورة إجمالاً", on_click=_go(app, "history"), expand=True),
    ]
    second = [
        w.stat_card("رأس المال بالمخزون", fmt_money(led.capital()), ft.Icons.INVENTORY_2_OUTLINED, accent=w.GOLD,
                    tint=w.GOLD_TINT, hint=f"{len(led.inventory)} صنف", on_click=_go(app, "inventory"), expand=True),
        w.stat_card("ديون لنا (عملاء)", fmt_money(owed_to_us), ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED,
                    accent=w.AMBER, tint=w.AMBER_TINT, hint=f"علينا للموردين: {fmt_money(we_owe)}",
                    on_click=_go(app, "customers"), expand=True),
        w.stat_card("أصناف شارفت على النفاد", str(len(low)), ft.Icons.WARNING_AMBER_OUTLINED,
                    accent=w.RED if low else w.TEAL, tint=w.RED_TINT if low else w.TINT,
                    hint="اضغط لعرض المخزون" if low else "المخزون بحالة جيدة", on_click=_go(app, "inventory"),
                    expand=True),
    ]
    if phone:      # على الهاتف: بطاقة واحدة في كل صف (Column لا Row) حتى لا تضيق الأرقام
        return ft.Column([*main, *second], spacing=12)
    return ft.Column([ft.Row(main, spacing=14), ft.Row(second, spacing=14)], spacing=14)


def _week_chart(app) -> ft.Control:
    """أعمدة آخر ٧ أيام — لكل عمود عرض ثابت (الحاوية بلا عرض كانت تختفي داخل Column)."""
    led = app.ledger
    today = datetime.now().date()
    days: list[tuple[str, Decimal, bool]] = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        total = sum((invoice_revenue(inv) for inv in led.invoices if _parse_dt(inv.date).date() == day), Decimal(0))
        days.append((_WEEKDAYS[day.weekday()][:3], total, i == 0))
    peak = max((v for _, v, _ in days), default=Decimal(0))
    top = peak if peak > 0 else Decimal(1)
    bars, grow = [], []
    for label, value, is_today in days:
        h = int(8 + 120 * (value / top))
        bar = ft.Container(width=26, height=6, border_radius=8, animate=w._anim(650),
                           bgcolor=(w.TEAL if is_today else "#6EE7B7") if value > 0 else w.LINE)
        grow.append((bar, h))
        bars.append(ft.Column([
            ft.Text(fmt_money(value) if value > 0 else "", size=11, weight=ft.FontWeight.W_700, color=w.INK,
                    text_align=ft.TextAlign.CENTER),
            bar,
            ft.Text(label, size=12, weight=ft.FontWeight.W_700 if is_today else ft.FontWeight.W_400,
                    color=w.TEAL if is_today else w.SOFT),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6, alignment=ft.MainAxisAlignment.END, expand=True))
    async def animate_in():
        """الأعمدة تنمو من الصفر لارتفاعها الحقيقي بعد ظهور اللوحة بلحظة (حركة ناعمة)."""
        await asyncio.sleep(0.12)
        for bar, h in grow:
            bar.height = h
        try:
            app.page.update()
        except Exception:  # noqa: BLE001
            pass
    try:
        app.page.run_task(animate_in)
    except Exception:  # noqa: BLE001
        for bar, h in grow:
            bar.height = h

    return w.card(ft.Column([
        _card_title("المبيعات — آخر ٧ أيام", w.chip(f"الذروة: {fmt_money(peak)}" if peak > 0 else "لا مبيعات بعد")),
        ft.Container(height=8),
        ft.Row(bars, vertical_alignment=ft.CrossAxisAlignment.END, spacing=6, height=190),
    ], spacing=4), padding=18)


def _today_invoices(app) -> ft.Control:
    """فواتير اليوم: الرقم، الزبون، الإجمالي، الربح، وحالة الدفع."""
    led = app.ledger
    today = datetime.now().strftime("%Y-%m-%d")
    mine = sorted(invoices_on_day(led.invoices, today), key=lambda i: i.date, reverse=True)
    total = sum((invoice_revenue(i) for i in mine), Decimal(0))
    profit = sum((invoice_profit_value(i) for i in mine), Decimal(0))
    head = _card_title("فواتير اليوم", ft.TextButton("عرض الكل", on_click=_go(app, "history"),
                                                      style=ft.ButtonStyle(color=w.TEAL)))
    if not mine:
        return w.card(ft.Column([head, w.empty("لا توجد فواتير اليوم بعد")], spacing=4), padding=18)
    rows = []
    for inv in mine[:8]:
        paid = is_invoice_paid(inv)
        pr = invoice_profit_value(inv)
        rows.append(ft.Container(
            ft.Row([
                w.icon_tile(ft.Icons.RECEIPT_LONG_OUTLINED, w.TEAL, w.TINT, 38),
                ft.Column([ft.Text(f"فاتورة {inv.number}", size=13, weight=ft.FontWeight.W_700, color=w.INK),
                           w.muted(inv.customer or "زبون نقدي", 12)], spacing=1, expand=True),
                ft.Column([ft.Text(fmt_money(inv.total), size=14, weight=ft.FontWeight.W_800, color=w.INK),
                           ft.Text(f"ربح {fmt_money(pr)}", size=12, color=w.TEAL if pr >= 0 else w.RED)],
                          horizontal_alignment=ft.CrossAxisAlignment.END, spacing=1),
                w.chip("مدفوعة" if paid else "آجلة", w.TEAL if paid else w.AMBER, w.TINT if paid else w.AMBER_TINT),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding(4, 8, 4, 8), border=w.border_side(bottom=True), ink=True, on_click=_go(app, "history")))
    summary = ft.Container(
        ft.Row([ft.Column([w.muted("إجمالي اليوم", 12), ft.Text(fmt_money(total), size=18, weight=ft.FontWeight.W_800, color=w.INK)],
                          spacing=0, expand=True),
                ft.Column([w.muted("ربح اليوم", 12), ft.Text(fmt_money(profit), size=18, weight=ft.FontWeight.W_800,
                                                          color=w.TEAL if profit >= 0 else w.RED)], spacing=0, expand=True)]),
        bgcolor=w.TINT, padding=12, border_radius=12)
    return w.card(ft.Column([head, summary, *rows], spacing=6), padding=18)


def _bar(value: float) -> ft.Control:
    """شريط تقدم أخضر؛ يجرّب اسم الخاصية الجديد ثم القديم حسب إصدار Flet."""
    for kw in ({"bar_height": 8}, {"height": 8}, {}):
        try:
            return ft.ProgressBar(value=value, color=w.TEAL, bgcolor=w.TINT, border_radius=6, **kw)
        except TypeError:
            continue
    return ft.ProgressBar(value=value)


def _top_items(app) -> ft.Control:
    """أكثر الأصناف مبيعاً — أشرطة نسبية بعرض ثابت محسوب."""
    tops = top_selling(app.ledger.invoices, 5)
    head = _card_title("الأكثر مبيعاً")
    if not tops:
        return w.card(ft.Column([head, w.empty("لا مبيعات بعد")], spacing=4), padding=18)
    mx = max(t.qty for t in tops) or 1
    rows = []
    for t in tops:
        rows.append(ft.Column([
            ft.Row([ft.Text(t.name, size=13, color=w.INK, expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(str(t.qty), size=13, weight=ft.FontWeight.W_800, color=w.TEAL)]),
            _bar(t.qty / mx),
        ], spacing=4))
    return w.card(ft.Column([head, *rows], spacing=12), padding=18)


def _low_stock(app) -> ft.Control:
    low = sorted(low_stock_items(app.ledger.inventory), key=lambda i: i.stock)[:5]
    head = _card_title("تنبيه المخزون", ft.TextButton("المخزون", on_click=_go(app, "inventory"),
                                                      style=ft.ButtonStyle(color=w.TEAL)))
    if not low:
        return w.card(ft.Column([head, w.banner("المخزون بحالة جيدة", "green")], spacing=8), padding=18)
    rows = [ft.Row([ft.Text(i.name, size=13, color=w.INK, expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    w.pill(f"{i.stock} متبقي", "red")]) for i in low]
    return w.card(ft.Column([head, *rows], spacing=10), padding=18)


def _quick_actions(app) -> ft.Control:
    """أزرار سريعة — Row بدون wrap وبدون expand معاً (هذا الجمع كان يكسر رسم اللوحة كلها)."""
    return ft.Row([
        w.btn("فاتورة جديدة", _go(app, "invoice"), "primary", icon=ft.Icons.ADD_SHOPPING_CART),
        w.btn("إضافة صنف", _go(app, "inventory"), "ghost", icon=ft.Icons.ADD_BOX_OUTLINED),
        w.btn("مسح فاتورة شراء", _go(app, "scan"), "ghost", icon=ft.Icons.DOCUMENT_SCANNER_OUTLINED),
        w.btn("تسجيل سند", _go(app, "vouchers"), "ghost", icon=ft.Icons.PAYMENTS_OUTLINED),
    ], spacing=10, scroll=w.smooth_scroll())


def _safe(name: str, fn, *a) -> ft.Control:
    """كل قسم يُبنى منفصلاً: لو فشل قسم يظهر سبب الفشل بدل أن تفرغ اللوحة كلها."""
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return w.banner(f"تعذّر عرض «{name}»: {type(e).__name__}: {e}", "red")


def build(app) -> ft.Control:
    led = app.ledger
    name = led.settings.get("businessName", "دفتري")
    wide = getattr(app, "_wide", False)
    pw = app.page_width() or 1000
    avail = max((pw - 264 - 48) if wide else (pw - 24), 300.0)
    phone = avail < 700

    header = ft.Column([
        w.muted(_today_label(), 13),
        ft.Text(f"أهلاً بك في {name}", size=26, weight=ft.FontWeight.W_800, color=w.INK),
        w.muted("نظرة سريعة على فواتير اليوم وأرباحه.", 14),
    ], spacing=2)

    notes: list[ft.Control] = []
    if not led.inventory and not led.invoices:
        why = led.health_notice() or ("لا توجد بيانات محمّلة بعد — اضغط زر التحديث بالأعلى لقراءتها من السحابة."
                                      if app.store is not None else "وضع محلي: لا توجد بيانات محفوظة على هذا الجهاز.")
        notes.append(w.banner(why, "amber"))

    chart = _safe("المبيعات", _week_chart, app)
    today = _safe("فواتير اليوم", _today_invoices, app)
    top = _safe("الأكثر مبيعاً", _top_items, app)
    low = _safe("تنبيه المخزون", _low_stock, app)
    if phone:
        lower: ft.Control = ft.Column([today, chart, top, low], spacing=14)
    else:
        lower = ft.Column([
            ft.Row([ft.Container(chart, expand=3), ft.Container(today, expand=2)], spacing=14,
                   vertical_alignment=ft.CrossAxisAlignment.START),
            ft.Row([ft.Container(top, expand=3), ft.Container(low, expand=2)], spacing=14,
                   vertical_alignment=ft.CrossAxisAlignment.START),
        ], spacing=14)

    return ft.Column([header, *notes, _safe("الإجراءات", _quick_actions, app),
                      _safe("المؤشرات", _kpis, app, phone), lower],
                     spacing=14, scroll=w.smooth_scroll(), expand=True)
