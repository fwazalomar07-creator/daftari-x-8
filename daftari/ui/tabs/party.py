"""صفحة ملخص العميل / المورد: شارات دائرية + بطاقات + قوائم (فواتير، سندات، حركات الحساب).

تُفتح من زر «ملخص» في قائمتي العملاء والموردين: app.go("customers", detail=<id>) أو app.go("suppliers", detail=<id>).
"""
from __future__ import annotations

import flet as ft

from ...core.money import fmt_money
from ...core.party import CustomerSummary, SupplierSummary, customer_summary, supplier_summary
from ...core.reports import customer_statement, supplier_statement
from ...ledger import LedgerError
from ...printing.render import (customer_statement_doc, invoice_doc, purchase_doc, supplier_statement_doc,
                                voucher_doc)
from .. import widgets as w

_HIST = {"initial": "رصيد افتتاحي", "invoice_debt": "دين فاتورة", "manual_debt": "دين يدوي",
         "purchase_debt": "دين مشتريات", "payment": "دفعة / سند", "manual_adjustment": "تعديل يدوي"}
_CREDIT = {"payment"}          # حركات تنقص الدين (لون أخضر)


# ------------------------------------------------------------------------------------------------ مكوّنات بصرية
def _badge(icon: str, value: str, label: str, color: str, tint: str, sub: str = "", size: int = 112) -> ft.Control:
    """شارة دائرية: أيقونة + الرقم داخل الدائرة، والعنوان تحتها."""
    circle = ft.Container(
        ft.Column([ft.Icon(icon, size=20, color=color),
                   ft.Text(value, size=15 if len(value) < 10 else 12, weight=ft.FontWeight.W_800, color=w.INK,
                           text_align=ft.TextAlign.CENTER, max_lines=1)],
                  alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2),
        width=size, height=size, border_radius=size / 2, bgcolor=tint, alignment=ft.Alignment(0, 0),
        border=w.border_all(3, "#FFFFFF"), shadow=w._soft_shadow(14, 4))
    return _labeled(circle, label, sub, size)


def _ring_badge(pct: float, label: str, color: str, tint: str, size: int = 112) -> ft.Control:
    """شارة بحلقة تقدم دائرية (نسبة مئوية) في الوسط."""
    pct = max(0.0, min(100.0, pct))
    center = ft.Container(ft.Text(f"{pct:.0f}%", size=18, weight=ft.FontWeight.W_800, color=w.INK),
                          width=size, height=size, alignment=ft.Alignment(0, 0))
    try:
        ring = ft.ProgressRing(value=pct / 100, width=size, height=size, stroke_width=9, color=color, bgcolor=tint)
        body: ft.Control = ft.Stack([ring, center], width=size, height=size)
    except Exception:  # noqa: BLE001 — إصدار لا يدعم الحلقة: دائرة عادية بالنسبة
        body = ft.Container(center, width=size, height=size, border_radius=size / 2, bgcolor=tint)
    return _labeled(body, label, "", size)


def _labeled(body: ft.Control, label: str, sub: str, size: int) -> ft.Control:
    rows = [body, ft.Text(label, size=12, weight=ft.FontWeight.W_700, color=w.INK, text_align=ft.TextAlign.CENTER)]
    if sub:
        rows.append(ft.Text(sub, size=11, color=w.SOFT, text_align=ft.TextAlign.CENTER))
    return ft.Container(ft.Column(rows, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
                        width=size + 24)


def _info_card(icon: str, label: str, value: str, color: str = w.TEAL, tint: str = w.TINT) -> ft.Container:
    return w.lift(ft.Container(
        ft.Row([w.icon_tile(icon, color, tint, 40),
                ft.Column([w.muted(label, 12), ft.Text(value, size=16, weight=ft.FontWeight.W_800, color=w.INK,
                                                       max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)],
                          spacing=1, expand=True)], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=w.PANEL, padding=14, border_radius=14, border=w.border_all(1, w.LINE),
        shadow=w._soft_shadow(10, 2), expand=True), 1.01)


def _top_items_card(top: list[tuple[str, int]], title: str) -> ft.Control:
    if not top:
        return w.card(ft.Column([w.title(title, 15), w.muted("لا توجد حركة بعد")], spacing=8))
    mx = max(q for _, q in top) or 1
    rows = []
    for name, q in top:
        try:
            bar = ft.ProgressBar(value=q / mx, color=w.TEAL, bgcolor=w.TINT, border_radius=6)
        except Exception:  # noqa: BLE001
            bar = ft.ProgressBar(value=q / mx)
        rows.append(ft.Column([ft.Row([ft.Text(name, size=13, color=w.INK, expand=True, max_lines=1,
                                               overflow=ft.TextOverflow.ELLIPSIS),
                                       ft.Text(str(q), size=13, weight=ft.FontWeight.W_800, color=w.TEAL)]), bar],
                              spacing=4))
    return w.card(ft.Column([w.title(title, 15), *rows], spacing=12))


def _tab_chip(text: str, active: bool, on_click) -> ft.Container:
    c = ft.Container(ft.Text(text, size=13, weight=ft.FontWeight.W_700, color="#FFFFFF" if active else w.INK),
                     bgcolor=w.TEAL if active else w.PANEL, border=w.border_all(1, w.TEAL if active else w.LINE),
                     padding=ft.Padding(16, 9, 16, 9), border_radius=20, ink=True, on_click=on_click)
    try:
        c.animate = w._anim(160)
    except Exception:  # noqa: BLE001
        pass
    return c


def _row(left_icon: str, title: str, sub: str, amount: str, amount_color: str, trail: ft.Control | None = None,
         on_click=None, tint: str = w.TINT, icon_color: str = w.TEAL) -> ft.Control:
    return w.row_card(ft.Row([
        w.icon_tile(left_icon, icon_color, tint, 40),
        ft.Column([ft.Text(title, size=13, weight=ft.FontWeight.W_700, color=w.INK),
                   w.muted(sub, 12)], spacing=1, expand=True),
        ft.Column([ft.Text(amount, size=14, weight=ft.FontWeight.W_800, color=amount_color),
                   *([trail] if trail else [])], horizontal_alignment=ft.CrossAxisAlignment.END, spacing=3),
    ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER), on_click=on_click)


def _history_rows(history) -> list[ft.Control]:
    out = []
    for h in sorted(history, key=lambda h: h.date or "", reverse=True)[:100]:
        credit = h.type in _CREDIT
        ref = h.invoice_number or h.purchase_number or h.voucher_number or h.note or ""
        out.append(_row(ft.Icons.SOUTH_WEST if credit else ft.Icons.NORTH_EAST, _HIST.get(h.type, h.type),
                        f"{(h.date or '')[:10]}" + (f" · {ref}" if ref else ""),
                        ("−" if credit else "+") + fmt_money(abs(h.amount)), w.TEAL if credit else w.RED,
                        tint=w.TINT if credit else w.RED_TINT, icon_color=w.TEAL if credit else w.RED))
    return out


# ------------------------------------------------------------------------------------------------ العميل
def build_customer(app, cid: str) -> ft.Control:
    led = app.ledger
    c = next((x for x in led.customers if x.id == cid), None)
    if c is None:
        return _missing(app, "customers", "الزبون غير موجود (ربما حُذف)")
    s: CustomerSummary = customer_summary(c, led.invoices, led.vouchers)
    state = {"tab": "invoices"}
    list_box = ft.Column(spacing=8)
    chips = ft.Row(spacing=8, wrap=True)
    back = ("customers", {"detail": cid})

    def render_list() -> None:
        t = state["tab"]
        if t == "invoices":
            rows = []
            for inv in s.invoices[:100]:
                from ...core.reports import invoice_profit_value, is_invoice_paid
                pr = invoice_profit_value(inv)

                async def open_inv(_, inv=inv):
                    await app.show_document(invoice_doc(inv, led.settings, app.line_images(inv)), inv.number, back=back)
                rows.append(_row(ft.Icons.RECEIPT_LONG_OUTLINED, f"فاتورة {inv.number}",
                                 f"{inv.date[:10]} · {sum(l.qty for l in inv.items)} قطعة", fmt_money(inv.total), w.INK,
                                 ft.Row([w.pill(f"ربح {fmt_money(pr)}", "green" if pr >= 0 else "red"),
                                         w.pill("مدفوعة" if is_invoice_paid(inv) else f"متبقي {fmt_money(inv.remaining)}",
                                                "gray" if is_invoice_paid(inv) else "amber")], spacing=4),
                                 on_click=open_inv))
            list_box.controls = rows or [w.empty("لا توجد فواتير لهذا الزبون")]
        elif t == "receipts":
            rows = []
            for v in s.receipts[:100]:
                async def open_v(_, v=v):
                    await app.show_document(voucher_doc(v, led.settings), v.number, back=back)
                rows.append(_row(ft.Icons.PAYMENTS_OUTLINED, f"سند قبض {v.number}",
                                 f"{(v.date or '')[:10]}" + (f" · {v.note}" if v.note else ""), fmt_money(v.amount),
                                 w.TEAL, w.muted(f"المتبقي بعده {fmt_money(v.remaining_balance)}", 11)
                                 if v.remaining_balance is not None else None, on_click=open_v))
            list_box.controls = rows or [w.empty("لا توجد سندات قبض لهذا الزبون")]
        else:
            list_box.controls = _history_rows(c.history) or [w.empty("لا توجد حركات")]
        chips.controls = [
            _tab_chip(f"الفواتير ({s.count})", t == "invoices", lambda _: switch("invoices")),
            _tab_chip(f"سندات القبض ({len(s.receipts)})", t == "receipts", lambda _: switch("receipts")),
            _tab_chip("حركات الحساب", t == "history", lambda _: switch("history"))]

    def switch(t: str) -> None:
        state["tab"] = t
        render_list()
        app.page.update()

    async def statement(_):
        st = customer_statement(c, led.invoices, oldest_first=True)
        await app.show_document(customer_statement_doc(st, led.settings, None), f"كشف-{c.name}", back=back)

    def pay(_):
        box: dict = {}

        async def submit(v):
            box["v"] = await led.record_customer_payment(c.id, v["amount"])

            async def show():
                await app.show_document(voucher_doc(box["v"], led.settings), box["v"].number, back=back)
            app.run_after_dialog(show)
        w.form_dialog(app, "سند قبض", [("amount", "المبلغ المقبوض", "", "num")], submit, "تسجيل",
                      f"{c.name} — الرصيد الحالي {fmt_money(c.balance)}")

    got = s.paid_at_sale + s.receipts_total
    badges = ft.Row([
        _badge(ft.Icons.SHOPPING_BAG_OUTLINED, fmt_money(s.sales_total), "المبيعات", w.TEAL, w.TINT, f"{s.count} فاتورة"),
        _badge(ft.Icons.TRENDING_UP, fmt_money(s.profit), "الأرباح منه", w.TEAL if s.profit >= 0 else w.RED,
               w.TINT if s.profit >= 0 else w.RED_TINT, f"هامش {s.margin_pct:.0f}%"),
        _badge(ft.Icons.PAYMENTS_OUTLINED, fmt_money(got), "المقبوض", w.GOLD, w.GOLD_TINT,
               f"{len(s.receipts)} سند قبض"),
        _badge(ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, fmt_money(c.balance), "الدين عليه",
               w.RED if c.balance > 0 else w.TEAL, w.RED_TINT if c.balance > 0 else w.TINT,
               "عليه دين" if c.balance > 0 else "لا دين ✓"),
        _ring_badge(float(s.collected_pct), "نسبة التحصيل", w.TEAL, w.TINT),
    ], wrap=True, spacing=8, run_spacing=14, alignment=ft.MainAxisAlignment.CENTER)

    render_list()
    return ft.Column([
        _header(app, "customers", c.name, f"{c.phone or 'بدون هاتف'}", c.balance, [
            w.btn("كشف حساب", statement, "ghost", small=True),
            *([w.btn("سند قبض", pay, small=True)] if c.balance > 0 else [])]),
        w.card(badges, padding=20),
        ft.Row([_info_card(ft.Icons.RECEIPT_OUTLINED, "متوسط الفاتورة", fmt_money(s.avg_invoice)),
                _info_card(ft.Icons.INVENTORY_2_OUTLINED, "عدد القطع المشتراة", str(s.items_qty), w.GOLD, w.GOLD_TINT),
                _info_card(ft.Icons.EVENT_AVAILABLE_OUTLINED, "آخر تعامل", (s.last_date or "—")[:10], w.AI, w.AI_TINT)],
               spacing=10),
        _top_items_card(s.top_items, "أكثر الأصناف شراءً"),
        chips, list_box], scroll=w.smooth_scroll(), expand=True, spacing=14)


# ------------------------------------------------------------------------------------------------ المورد
def build_supplier(app, sid: str) -> ft.Control:
    led = app.ledger
    sup = next((x for x in led.suppliers if x.id == sid), None)
    if sup is None:
        return _missing(app, "suppliers", "المورد غير موجود (ربما حُذف)")
    s: SupplierSummary = supplier_summary(sup, led.purchases, led.vouchers, led.invoices)
    state = {"tab": "purchases"}
    list_box = ft.Column(spacing=8)
    chips = ft.Row(spacing=8, wrap=True)
    back = ("suppliers", {"detail": sid})

    def render_list() -> None:
        t = state["tab"]
        if t == "purchases":
            rows = []
            for p in s.purchases[:100]:
                async def open_p(_, p=p):
                    await app.show_document(purchase_doc(p, led.settings, app.line_images(p)), p.number, back=back)
                rows.append(_row(ft.Icons.LOCAL_SHIPPING_OUTLINED, f"فاتورة شراء {p.number}",
                                 f"{p.date[:10]} · {sum(l.qty for l in p.items)} قطعة", fmt_money(p.subtotal), w.INK,
                                 w.pill("مسدّدة" if not p.remaining > 0 else f"متبقي {fmt_money(p.remaining)}",
                                        "gray" if not p.remaining > 0 else "amber"), on_click=open_p))
            list_box.controls = rows or [w.empty("لا توجد فواتير شراء من هذا المورد")]
        elif t == "payments":
            rows = []
            for v in s.payments[:100]:
                async def open_v(_, v=v):
                    await app.show_document(voucher_doc(v, led.settings), v.number, back=back)
                rows.append(_row(ft.Icons.PAYMENTS_OUTLINED, f"سند دفع {v.number}",
                                 f"{(v.date or '')[:10]}" + (f" · {v.note}" if v.note else ""), fmt_money(v.amount),
                                 w.TEAL, w.muted(f"المتبقي بعده {fmt_money(v.remaining_balance)}", 11)
                                 if v.remaining_balance is not None else None, on_click=open_v))
            list_box.controls = rows or [w.empty("لا توجد سندات دفع لهذا المورد")]
        else:
            list_box.controls = _history_rows(sup.history) or [w.empty("لا توجد حركات")]
        chips.controls = [
            _tab_chip(f"فواتير الشراء ({s.count})", t == "purchases", lambda _: switch("purchases")),
            _tab_chip(f"سندات الدفع ({len(s.payments)})", t == "payments", lambda _: switch("payments")),
            _tab_chip("حركات الحساب", t == "history", lambda _: switch("history"))]

    def switch(t: str) -> None:
        state["tab"] = t
        render_list()
        app.page.update()

    async def statement(_):
        st = supplier_statement(sup, led.purchases, oldest_first=True)
        await app.show_document(supplier_statement_doc(st, led.settings), f"كشف-{sup.name}", back=back)

    def pay(_):
        box: dict = {}

        async def submit(v):
            box["v"] = await led.record_supplier_payment(sup.id, v["amount"])

            async def show():
                await app.show_document(voucher_doc(box["v"], led.settings), box["v"].number, back=back)
            app.run_after_dialog(show)
        w.form_dialog(app, "تسجيل دفعة لمورد (سند دفع)", [("amount", "المبلغ المدفوع", "", "num")], submit, "تسجيل",
                      f"{sup.name} — الرصيد الحالي {fmt_money(sup.balance)}")

    paid = s.paid_at_purchase + s.payments_total
    badges = ft.Row([
        _badge(ft.Icons.LOCAL_SHIPPING_OUTLINED, fmt_money(s.purchases_total), "المشتريات", w.TEAL, w.TINT,
               f"{s.count} فاتورة"),
        _badge(ft.Icons.PAYMENTS_OUTLINED, fmt_money(paid), "المدفوع له", w.GOLD, w.GOLD_TINT,
               f"{len(s.payments)} سند دفع"),
        _badge(ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, fmt_money(sup.balance), "الدين علينا",
               w.RED if sup.balance > 0 else w.TEAL, w.RED_TINT if sup.balance > 0 else w.TINT,
               "علينا دين" if sup.balance > 0 else "لا دين ✓"),
        _badge(ft.Icons.TRENDING_UP, fmt_money(s.est_profit), "أرباح أصنافه", w.TEAL if s.est_profit >= 0 else w.RED,
               w.TINT if s.est_profit >= 0 else w.RED_TINT, "تقديري"),
        _ring_badge(float(s.paid_pct), "نسبة السداد", w.TEAL, w.TINT),
    ], wrap=True, spacing=8, run_spacing=14, alignment=ft.MainAxisAlignment.CENTER)

    render_list()
    return ft.Column([
        _header(app, "suppliers", sup.name, "مورد", sup.balance, [
            w.btn("كشف حساب", statement, "ghost", small=True),
            *([w.btn("تسجيل دفعة", pay, small=True)] if sup.balance > 0 else [])]),
        w.card(badges, padding=20),
        ft.Row([_info_card(ft.Icons.RECEIPT_OUTLINED, "متوسط فاتورة الشراء", fmt_money(s.avg_purchase)),
                _info_card(ft.Icons.INVENTORY_2_OUTLINED, "القطع المشتراة", str(s.items_qty), w.GOLD, w.GOLD_TINT),
                _info_card(ft.Icons.POINT_OF_SALE, "مبيعات أصنافه (تقديري)", fmt_money(s.est_sales), w.AI, w.AI_TINT)],
               spacing=10),
        w.muted("الأرباح تقديرية: ربح مبيعات الأصناف التي اشتريناها من هذا المورد، وقد يشاركه فيها موردون آخرون.", 12),
        _top_items_card(s.top_items, "أكثر الأصناف المشتراة منه"),
        chips, list_box], scroll=w.smooth_scroll(), expand=True, spacing=14)


# ------------------------------------------------------------------------------------------------ مشترك
def _header(app, tab: str, name: str, subtitle: str, balance, actions: list[ft.Control]) -> ft.Control:
    async def back(_):
        await app.go(tab)
    return ft.Column([
        ft.Row([w.btn("رجوع", back, "ghost", small=True, icon=ft.Icons.ARROW_FORWARD)]),
        ft.Row([w.avatar(name, 56),
                ft.Column([ft.Text(name, size=22, weight=ft.FontWeight.W_800, color=w.INK), w.muted(subtitle, 13)],
                          spacing=1, expand=True),
                w.pill(f"{'عليه' if tab == 'customers' else 'علينا'} {fmt_money(balance)}", "red") if balance > 0
                else w.pill("لا دين ✓", "green")],
               spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ft.Row(actions, spacing=8, wrap=True),
    ], spacing=10)


def _missing(app, tab: str, msg: str) -> ft.Control:
    async def back(_):
        await app.go(tab)
    return ft.Column([w.banner(msg, "amber"), w.btn("رجوع", back, "ghost")], spacing=12)
