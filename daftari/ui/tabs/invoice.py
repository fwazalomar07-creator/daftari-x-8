"""تبويب «فاتورة جديدة» (renderInvoiceTab + finalizeInvoice)."""
from __future__ import annotations

import time

import flet as ft

from ...core.calc import invoice_totals
from ...core.models import InvoiceLine, uid
from ...core.money import ZERO, fmt_money, fmt_num, parse_num, q2, to_decimal
from ...core.search import search_inventory
from ...ledger import LedgerError
from ...printing.render import invoice_doc
from .. import widgets as w

TIERS = [("retail", "مفرق"), ("wholesale", "جملة"), ("distribution", "توزيع")]


async def finalize(app) -> None:
    """إقفال الفاتورة (يدوياً أو بانتهاء المؤقت): يحفظ، يفرّغ المسودة، ويعرض المستند للطباعة."""
    led, d = app.ledger, app.invoice_draft
    barcode_bytes = d.barcode_image  # قبل clear
    inv, warnings = await led.finalize_invoice(d)
    d.clear()
    app.drafts.clear_invoice()
    for msg in warnings:
        w.toast(app.page, "⚠ " + msg)
    doc = invoice_doc(inv, led.settings, app.line_images(inv), barcode=barcode_bytes)
    await app.show_document(doc, inv.number, back=("invoice", {}))


def _lines(app):
    return app.ledger._build_invoice_lines(app.invoice_draft)


CATALOG_LIMIT = 60


def build(app) -> ft.Control:
    led, d = app.ledger, app.invoice_draft
    split = app.pos_split()
    phone = not getattr(app, "_wide", False)
    results = ft.Row(spacing=10, run_spacing=10, wrap=True)       # كتالوج الأصناف (يظهر دائماً، لا ينتظر البحث)
    cart_box = ft.Column(spacing=8)
    totals_box = ft.Column(spacing=6)
    suggest_box = ft.Row(wrap=True, spacing=6)
    debt_note = ft.Text("", color=w.RED, size=12)
    cart_count = ft.Text("", size=12, color=w.SOFT)
    state = {"q": "", "all": False}
    tile_w = max(150, int((app.page_width() - 24 - 10) / 2)) if phone and app.page_width() else 176

    # ---- المجاميع (تُحدَّث دون إعادة بناء الصفحة) ----
    def refresh_totals() -> None:
        try:
            lines = _lines(app)
        except LedgerError:
            lines = []
        t = invoice_totals(lines, d.discount, d.paid)
        cart_count.value = f"{sum(l.qty for l in lines)} قطعة · {len(lines)} بند"

        def row(label, value, color=w.INK):
            return ft.Row([ft.Text(label, color=w.SOFT, size=13),
                           ft.Text(fmt_money(value), color=color, weight=ft.FontWeight.W_700, size=13)],
                          alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
        totals_box.controls = [
            row("المجموع الفرعي", t.subtotal),
            *([row("الخصم", -t.discount, w.RED)] if t.discount else []),
            ft.Divider(color=w.LINE, height=1),
            ft.Row([ft.Text("الإجمالي", size=15, weight=ft.FontWeight.W_700, color=w.INK),
                    ft.Text(fmt_money(t.total), size=24, weight=ft.FontWeight.W_800, color=w.TEAL)],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            row("المدفوع", t.paid, w.GREEN),
            row("المتبقي (دين)", t.remaining, w.RED if t.remaining > 0 else w.INK),
        ]
        app.drafts.save_invoice(d)

    # ---- كتالوج الأصناف ----
    def render_results(animate: bool = False, force: bool = False, start: int = 0) -> None:
        """animate=True: تظهر البطاقات ببطء واحدة تلو الأخرى (دخول القسم / بحث / عرض الكل). الإضافة للسلة بلا حركة."""
        items = search_inventory(led.inventory, state["q"])
        if not state["q"].strip():
            items = sorted(items, key=lambda i: (i.stock <= 0, i.name))      # المتوفر أولاً
        shown = items if state["all"] else items[:CATALOG_LIMIT]
        out: list[ft.Control] = []
        for it in shown:
            in_cart = d.cart.get(it.id, 0)
            out_of_stock = it.stock <= 0
            low = 0 < it.stock <= 3

            def add(_, it=it):
                if not d.inc(it):
                    w.toast(app.page, f"لا يوجد مخزون كافٍ من «{it.name}»", error=True)
                    return
                render_cart(); render_results(); refresh_totals(); app.page.update()

            badge = (w.pill("نفد", "red") if out_of_stock else w.pill(f"{it.stock} متوفر", "amber" if low else "green"))
            out.append(w.item_tile(
                app, it, width=tile_w, big=fmt_money(it.price), badge=badge,
                selected=bool(in_cart), dim=out_of_stock or in_cart >= it.stock and not in_cart,
                on_click=None if out_of_stock or in_cart >= it.stock else add,
                actions=ft.Row([ft.Container(ft.Text(f"× {in_cart}", size=12, weight=ft.FontWeight.W_800, color="#FFFFFF"),
                                             bgcolor=w.TEAL, padding=ft.Padding(10, 3, 10, 3), border_radius=20)])
                if in_cart else None))
        if not out:
            out = [w.empty("لا توجد أصناف في المخزون — أضفها من تبويب المخزون" if not led.inventory else "لا نتائج مطابقة")]
        elif len(items) > len(shown):
            def more(_):
                state["all"] = True
                render_results(animate=True, force=True, start=CATALOG_LIMIT); app.page.update()
            out.append(ft.Container(w.btn(f"عرض الكل ({len(items)})", more, "ghost"), padding=8))
        results.controls = out
        if animate:
            w.reveal(app, out, force=force, start=start)

    def on_search(q: str) -> None:
        state["q"], state["all"] = q, False
        render_results(animate=True, force=True)
        app.page.update()

    # ---- السلة ----
    def render_cart() -> None:
        ctrls: list[ft.Control] = []
        for iid, qty in list(d.cart.items()):
            it = led._item(iid)
            if not it:
                continue
            price = d.price_overrides.get(iid, it.price)
            tier = d.price_tier.get(iid, "retail")

            def inc(_, it=it):
                if d.inc(it):
                    render_cart(); render_results(); refresh_totals(); app.page.update()

            def dec(_, iid=iid):
                d.dec(iid); render_cart(); render_results(); refresh_totals(); app.page.update()

            def set_tier(_, it=it, t=None):
                d.select_tier(it, t); render_cart(); refresh_totals(); app.page.update()

            def edit_price(_, it=it, iid=iid, price=price):
                async def submit(v):
                    p = parse_num(v["price"])
                    if p is None or p < 0:
                        raise LedgerError("أدخل سعر صحيح")
                    if q2(p) == it.price:
                        d.price_overrides.pop(iid, None)
                    else:
                        d.price_overrides[iid] = q2(p)
                w.form_dialog(app, "تعديل سعر الصنف لهذه الفاتورة", [("price", "السعر", fmt_num(price), "num")], submit,
                              subtitle=f"{it.name} — السعر الأصلي {fmt_money(it.price)}")

            chips = ft.Row([ft.Container(ft.Text(lbl, size=11, weight=ft.FontWeight.W_600,
                                                 color="#FFFFFF" if tier == key else w.SOFT),
                                         bgcolor=w.TEAL if tier == key else w.SLATE_TINT,
                                         padding=ft.Padding(10, 4, 10, 4), border_radius=20,
                                         on_click=lambda e, it=it, k=key: set_tier(e, it, k)) for key, lbl in TIERS],
                           spacing=4)
            stepper = ft.Container(ft.Row([
                ft.IconButton(icon=ft.Icons.REMOVE, icon_size=16, on_click=dec),
                ft.Text(str(qty), size=14, weight=ft.FontWeight.W_800),
                ft.IconButton(icon=ft.Icons.ADD, icon_size=16, on_click=inc)], spacing=0,
                vertical_alignment=ft.CrossAxisAlignment.CENTER), bgcolor=w.SLATE_TINT, border_radius=20)
            ctrls.append(w.row_card(ft.Column([
                ft.Row([ft.Column([ft.Text(it.name, weight=ft.FontWeight.W_700, size=13, max_lines=2,
                                           overflow=ft.TextOverflow.ELLIPSIS), w.muted(it.code or "—", 11)],
                                  spacing=0, expand=True),
                        ft.IconButton(icon=ft.Icons.CLOSE, icon_size=16, icon_color=w.SOFT,
                                      on_click=lambda e, i=iid: _remove(i))],
                       vertical_alignment=ft.CrossAxisAlignment.START),
                ft.Row([stepper, ft.TextButton(f"× {fmt_money(price)}", on_click=edit_price),
                        ft.Text(fmt_money(price * qty), weight=ft.FontWeight.W_800, color=w.TEAL, size=15)],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                chips], spacing=4)))
        for cl in d.custom_lines:
            ctrls.append(w.row_card(ft.Row([
                ft.Column([ft.Row([ft.Text(cl.name, weight=ft.FontWeight.W_700, size=13), w.pill("يدوي", "gold")], spacing=6),
                           w.muted(f"{cl.qty} × {fmt_money(cl.price)}")], expand=True, spacing=2),
                ft.Text(fmt_money(cl.price * cl.qty), color=w.TEAL, weight=ft.FontWeight.W_800),
                ft.IconButton(icon=ft.Icons.CLOSE, icon_size=16, on_click=lambda e, c=cl: _remove_custom(c))]),
                bgcolor=w.GOLD_TINT))
        cart_box.controls = ctrls or [w.empty("الفاتورة فارغة — اضغط على أي صنف لإضافته")]

    def _remove(iid: str) -> None:
        d.cart.pop(iid, None); d.price_overrides.pop(iid, None); d.price_tier.pop(iid, None)
        render_cart(); render_results(); refresh_totals(); app.page.update()

    def _remove_custom(cl) -> None:
        d.custom_lines = [c for c in d.custom_lines if c.id != cl.id]
        render_cart(); refresh_totals(); app.page.update()

    def add_custom(_):
        async def submit(v):
            name = v["name"].strip()
            price, qty = parse_num(v["price"]), parse_num(v["qty"])
            if not name:
                raise LedgerError("اكتب اسم الصنف")
            if price is None or price < 0:
                raise LedgerError("أدخل سعر صحيح")
            if qty is None or int(qty) <= 0:
                raise LedgerError("أدخل كمية صحيحة")
            cost = parse_num(v["cost"])
            d.custom_lines.append(InvoiceLine(id=uid(), name=name, price=q2(price), cost=q2(cost) if cost and cost > 0 else ZERO,
                                              qty=int(qty), is_custom=True))
        w.form_dialog(app, "إضافة صنف / خدمة يدوية", [
            ("name", "اسم الصنف أو الخدمة", "", "text"), ("cost", "رأس المال (اختياري، لحساب الربح)", "", "num"),
            ("price", "سعر البيع", "", "num"), ("qty", "الكمية", "1", "int")], submit, "إضافة", "غير موجودة بالمخزون")

    # ---- الزبون ----
    def customer_changed(e) -> None:
        d.customer = e.control.value or ""
        q = d.customer.strip().lower()
        matches = [c for c in led.customers if q and q in c.name.lower() and c.name.lower() != q][:5]
        suggest_box.controls = [ft.Container(ft.Row([w.avatar(c.name, 22), ft.Text(c.name, size=12)], spacing=6),
                                             bgcolor=w.TINT, padding=ft.Padding(8, 5, 10, 5), border_radius=20,
                                             on_click=lambda ev, n=c.name: pick_customer(n)) for c in matches]
        _debt_note()
        app.drafts.save_invoice(d); app.page.update()

    def _debt_note() -> None:
        c = led._find_customer(d.customer)
        debt_note.value = f"دين سابق على هذا الزبون: {fmt_money(c.balance)}" if c and c.balance > 0 else ""

    cust_tf = w.field("اسم الزبون (اختياري)", d.customer, on_change=customer_changed)

    def pick_customer(name: str) -> None:
        d.customer = name; cust_tf.value = name; suggest_box.controls = []; _debt_note(); app.drafts.save_invoice(d)
        app.page.update()

    def discount_changed(e) -> None:
        d.discount = q2(parse_num(e.control.value) or ZERO); refresh_totals(); app.page.update()

    def paid_changed(e) -> None:
        v = (e.control.value or "").strip()
        d.paid = None if v == "" else q2(parse_num(v) or ZERO)
        refresh_totals(); app.page.update()

    def notes_changed(e) -> None:
        d.notes = e.control.value or ""; app.drafts.save_invoice(d)

    def number_changed(e) -> None:
        d.custom_number = e.control.value or ""; app.drafts.save_invoice(d)

    # ---- المؤقت ----
    hours_tf = w.field("ساعات", "1", kind="num", width=90)
    timer_text = ft.Text(d.remaining_text(time.time()), color=w.AMBER)

    async def start_timer(_):
        h = parse_num(hours_tf.value)
        if h is None or h <= 0:
            w.toast(app.page, "أدخل عدد ساعات صحيح", error=True); return
        d.expiry = time.time() + float(h) * 3600
        w.toast(app.page, f"تم ضبط المؤقت — تُقفل الفاتورة تلقائياً بعد {h} ساعة")
        await app.after_change()

    async def cancel_timer(_):
        d.expiry = None
        await app.after_change()

    timer_row = ft.Row([ft.Text("⏱ إقفال تلقائي بعد:", size=13), hours_tf, w.btn("ضبط", start_timer, "ghost", small=True)]) \
        if d.expiry is None else ft.Row([timer_text, w.btn("إلغاء المؤقت", cancel_timer, "ghost", small=True)])

    # ---- أزرار الإنهاء ----
    async def done(_):
        try:
            await finalize(app)
        except LedgerError as e:
            w.toast(app.page, str(e), error=True)

    def clear(_):
        editing = bool(d.editing_id)
        msg = (f"أنت تعدّل فاتورة موجودة (رقم {d.editing_number}). تفريغها الآن سيحذفها نهائياً من السجل. متابعة؟"
               if editing else "تفريغ الفاتورة الحالية؟")

        async def go():
            if editing:
                await led.discard_editing_invoice(d)
                w.toast(app.page, "تم حذف الفاتورة نهائياً")
            d.clear(); app.drafts.clear_invoice()
        w.confirm_dialog(app, "تفريغ فاتورة قيد التعديل" if editing else "تفريغ الفاتورة", msg, go, "تفريغ", True)

    async def cancel_edit(_):
        await led.cancel_edit_invoice(d)  # أعد الفاتورة الأصلية كما كانت (مخزون ودين)
        d.clear(); app.drafts.clear_invoice()
        w.toast(app.page, "تم إلغاء التعديل وعادت الفاتورة كما كانت")
        await app.go("history")

    # ---- بناء ----
    top: list[ft.Control] = []
    if app.recovered:
        app.recovered = False
        top.append(w.banner("تم استرجاع فاتورة لم تكتمل من آخر مرة. تابعها أو فرّغها.", "info"))
    if d.editing_id:
        top.append(w.banner(f"✏️ تعديل الفاتورة {d.editing_number} — لن تُحفظ التعديلات حتى تضغط «إصدار الفاتورة».", "amber"))

    render_results(animate=True); render_cart(); refresh_totals(); _debt_note()
    discount_tf = w.field("الخصم", fmt_num(d.discount) if d.discount else "", kind="num", on_change=discount_changed, expand=True)
    paid_tf = w.field("المدفوع الآن (فارغ = كامل)", "" if d.paid is None else fmt_num(d.paid), kind="num",
                      on_change=paid_changed, expand=True)
    buttons = [w.btn("✔ إصدار الفاتورة", done, "success", expand=True), w.btn("تفريغ", clear, "danger")]
    if d.editing_id:
        buttons.insert(1, w.btn("إلغاء التعديل", cancel_edit, "ghost"))

    catalog = ft.Column([
        ft.Row([w.title("الأصناف", 16), w.pill(f"{len(led.inventory)} صنف", "gray"), ft.Container(expand=True),
                w.btn("صنف/خدمة يدوية", add_custom, "ghost", icon=ft.Icons.EDIT_NOTE, small=True)],
               vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
        w.search_bar(app, on_search, "ابحث عن صنف بالاسم أو الكود…"),
        results,
    ], spacing=10)

    # ---- باركود الفاتورة / المتجر ----
    barcode_slot = ft.Container(width=140, height=56, bgcolor=w.SLATE_TINT, border_radius=8,
                                border=w.border_all(1, w.LINE), alignment=ft.Alignment(0, 0),
                                content=ft.Text("لا باركود", size=11, color=w.SOFT))

    def _show_barcode_preview(data: bytes | None) -> None:
        if data:
            barcode_slot.content = w.image_from_bytes(data, width=140, height=56, fit=w._contain())
        else:
            barcode_slot.content = ft.Text("لا باركود", size=11, color=w.SOFT)
        try:
            barcode_slot.update()
        except Exception:
            pass

    if d.barcode_image:
        _show_barcode_preview(d.barcode_image)
    else:
        from ...printing.render import store_barcode_bytes
        _sb = store_barcode_bytes(led.settings)
        if _sb:
            _show_barcode_preview(_sb)

    async def upload_barcode(_):
        got = await app.pick_file_bytes(extensions=["png", "jpg", "jpeg", "webp", "gif"])
        if not got:
            return
        name, data = got
        try:
            from ...ai.media import compress_for_vision
            jpeg = compress_for_vision(data, max_side=500, quality=85)
        except Exception:
            jpeg = data
        d.barcode_image = jpeg
        app.drafts.save_invoice(d)
        _show_barcode_preview(jpeg)
        w.toast(app.page, "تم رفع باركود الفاتورة")

    async def clear_barcode(_):
        d.barcode_image = None
        app.drafts.save_invoice(d)
        # أعد عرض باركود المتجر إن وُجد
        from ...printing.render import store_barcode_bytes
        _show_barcode_preview(store_barcode_bytes(led.settings))
        w.toast(app.page, "تم حذف باركود الفاتورة")

    barcode_row = ft.Row([
        barcode_slot,
        ft.Column([
            w.btn("📷 رفع باركود", upload_barcode, "ghost", small=True, icon=ft.Icons.QR_CODE_2),
            w.btn("حذف", clear_barcode, "danger", small=True),
        ], spacing=4),
        ft.Container(expand=True),
        w.muted("باركود الفاتورة أو المتجر", 11),
    ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    panel = w.card(ft.Column([
        ft.Row([w.icon_tile(ft.Icons.RECEIPT_LONG_OUTLINED, w.TEAL, w.TINT, 34), w.title("الفاتورة", 16),
                ft.Container(expand=True), cart_count], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=10),
        cart_box,
        ft.Divider(color=w.LINE, height=1),
        cust_tf, suggest_box, debt_note,
        ft.Row([discount_tf, paid_tf], spacing=8),
        w.field("ملاحظات", d.notes, kind="multiline", on_change=notes_changed),
        w.field("رقم فاتورة مخصص (اختياري)", d.custom_number, on_change=number_changed) if not d.editing_id else ft.Container(),
        timer_row,
        ft.Container(totals_box, bgcolor=w.SLATE_TINT, padding=14, border_radius=12),
        barcode_row,
        ft.Row(buttons, spacing=8),
    ], spacing=10), padding=16)

    if split:  # شاشة عريضة: الكتالوج يسار والفاتورة يمين بلوحة ثابتة العرض، وكلٌّ يتمرّر وحده
        return ft.Column([*top, ft.Row([
            w.with_scroll_nav(ft.Column([catalog], scroll=w.smooth_scroll(), expand=True)),
            ft.Container(ft.Column([panel], scroll=w.smooth_scroll()), width=420),
        ], expand=True, spacing=16, vertical_alignment=ft.CrossAxisAlignment.START)], expand=True, spacing=10)
    return w.with_scroll_nav(ft.Column([*top, catalog, panel], scroll=w.smooth_scroll(), expand=True, spacing=14))
