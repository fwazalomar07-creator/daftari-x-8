"""تبويب فواتير الشراء (renderPurchasesTab) بكل أجزائه."""
from __future__ import annotations

import flet as ft

from ...core.calc import purchase_totals
from ...core.importer import ImportError_, parse_import_file
from ...core.models import PurchaseLine
from ...core.money import ZERO, fmt_money, fmt_num, parse_num, q2
from ...core.search import fuzzy_match, search_inventory
from ...ledger import LedgerError, PurchaseDraft
from ...printing.render import arabic_date, purchase_doc
from .. import widgets as w

PAGE = 5


def build(app) -> ft.Control:
    if "pricing" in app.sub:
        return _pricing_view(app, app.sub["pricing"])
    led, d = app.ledger, app.purchase_draft
    state = {"q": "", "shown": PAGE, "hq": ""}
    picks, cart_box, totals_box, hist_box = ft.Column(spacing=8), ft.Column(spacing=8), ft.Column(spacing=4), ft.Column(spacing=8)
    more_btn = ft.Container()

    def save_draft() -> None:
        app.drafts.save_purchase(d)

    # ---- المجاميع ----
    def lines() -> list[PurchaseLine]:
        out = []
        for iid, qty in d.cart.items():
            it = led._item(iid)
            if it and qty > 0:
                out.append(PurchaseLine(id=it.id, name=it.name, code=it.code, qty=qty, cost=d.costs.get(iid, it.cost)))
        out.extend(d.orphans)          # أصناف حُذفت من المخزون لكنها ما زالت ضمن هذه الفاتورة
        return out

    def refresh_totals() -> None:
        t = purchase_totals(lines(), d.paid)
        rows = [("الإجمالي", t.total, w.TEAL), ("المدفوع", t.paid, w.GREEN), ("المتبقي (دين علينا)", t.remaining, w.RED if t.remaining > 0 else w.INK)]
        orph = [w.muted(f"محفوظ في الفاتورة (صنف محذوف من المخزون): {o.name} × {o.qty} — {fmt_money(o.cost * o.qty)}")
                for o in d.orphans]
        totals_box.controls = [*orph, *[ft.Row([ft.Text(l, color=w.SOFT), ft.Text(fmt_money(v), color=c, weight=ft.FontWeight.BOLD)],
                                      alignment=ft.MainAxisAlignment.SPACE_BETWEEN) for l, v, c in rows]]
        save_draft()

    # ---- اختيار الأصناف (5 في كل مرة + «عرض المزيد») ----
    def render_picks(animate: bool = False, force: bool = False, start: int = 0) -> None:
        found = search_inventory(led.inventory, state["q"]) if state["q"].strip() else list(led.inventory)
        visible = found[:state["shown"]]
        out: list[ft.Control] = []
        for it in visible:
            qty = d.cart.get(it.id, 0)

            def step(delta, it=it):
                def h(_):
                    cur = d.cart.get(it.id, 0) + delta
                    if cur <= 0:
                        d.cart.pop(it.id, None); d.costs.pop(it.id, None)
                    else:
                        d.cart[it.id] = cur
                        d.costs.setdefault(it.id, it.cost)
                    render_picks(); render_cart(); refresh_totals(); app.page.update()
                return h

            def cost_changed(e, it=it):
                v = parse_num(e.control.value)
                if v is not None and v >= 0:
                    d.costs[it.id] = q2(v); render_cart(); refresh_totals(); app.page.update()

            cost_val = d.costs.get(it.id, it.cost)
            stepper = ft.Container(ft.Row([
                ft.IconButton(icon=ft.Icons.REMOVE, on_click=step(-1), disabled=qty <= 0),
                ft.Text(str(qty), size=18, weight=ft.FontWeight.W_800),
                ft.IconButton(icon=ft.Icons.ADD, on_click=step(1))], spacing=0,
                vertical_alignment=ft.CrossAxisAlignment.CENTER), bgcolor=w.SLATE_TINT, border_radius=24)
            # بطاقة كبيرة: صورة المنتج 104px (تُضغط لتكبيرها) + زر «عرض الصورة» بجانب كل منتج
            out.append(w.card(ft.Column([
                ft.Row([
                    w.thumb(app, it, 124, zoom=True),
                    ft.Column([
                        ft.Text(it.name, weight=ft.FontWeight.W_800, size=16, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                        w.muted(f"الكود: {it.code or '—'}", 12),
                        ft.Row([w.pill(f"المخزون: {it.stock}", "red" if it.stock <= 3 else "gray"),
                                w.btn("عرض الصورة", lambda _, it=it: w.show_image(app, it), "ghost", small=True,
                                      icon=ft.Icons.ZOOM_IN)], wrap=True, spacing=6, run_spacing=4),
                    ], expand=True, spacing=4)], vertical_alignment=ft.CrossAxisAlignment.START, spacing=14),
                ft.Row([stepper, w.field("تكلفة الشراء للقطعة (هذه العملية)", fmt_num(cost_val), kind="num",
                                         on_change=cost_changed, expand=True)],
                       spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)], spacing=12),
                padding=14))
        picks.controls = out or [w.muted("لا أصناف مطابقة — أضِفه كصنف جديد بالأسفل")]
        left = len(found) - len(visible)

        def more(_):
            old = state["shown"]
            state["shown"] += PAGE
            render_picks(animate=True, force=True, start=old); app.page.update()
        more_btn.content = w.btn(f"عرض المزيد ({left} متبقي)", more, "ghost") if left > 0 else None
        if animate:
            w.reveal(app, picks.controls, force=force, start=start)

    def on_search(q: str) -> None:
        state["q"], state["shown"] = q, PAGE
        render_picks(animate=True, force=True); app.page.update()

    # ---- السلة ----
    def render_cart() -> None:
        ctrls: list[ft.Control] = []
        for ln in lines():
            def edit(_, ln=ln):
                async def submit(v):
                    qty, cost = parse_num(v["qty"]), parse_num(v["cost"])
                    if qty is None or int(qty) <= 0:
                        raise LedgerError("أدخل كمية صحيحة")
                    if cost is None or cost < 0:
                        raise LedgerError("أدخل تكلفة صحيحة")
                    d.cart[ln.id], d.costs[ln.id] = int(qty), q2(cost)
                w.form_dialog(app, "تعديل بند الشراء", [("qty", "الكمية", ln.qty, "int"), ("cost", "التكلفة", fmt_num(ln.cost), "num")],
                              submit, subtitle=ln.name)

            def remove(_, ln=ln):
                d.cart.pop(ln.id, None); d.costs.pop(ln.id, None)
                render_picks(); render_cart(); refresh_totals(); app.page.update()
            cit = led._item(ln.id)
            ctrls.append(w.card(ft.Row([
                *([w.thumb(app, cit, 68, zoom=True)] if cit else []),
                ft.Column([ft.Text(ln.name, weight=ft.FontWeight.BOLD), w.muted(f"{ln.qty} × {fmt_money(ln.cost)}")], expand=True, spacing=2),
                ft.Text(fmt_money(ln.cost * ln.qty), color=w.TEAL, weight=ft.FontWeight.BOLD),
                ft.IconButton(icon=ft.Icons.EDIT_OUTLINED, on_click=edit), ft.IconButton(icon=ft.Icons.CLOSE, on_click=remove)]), padding=8))
        cart_box.controls = ctrls or [w.empty("لا توجد بنود بعد")]

    # ---- استيراد ملف ----
    async def import_file(_):
        got = await app.pick_file_bytes(["xlsx", "csv", "json"])
        if not got:
            return
        try:
            payload = parse_import_file(got[0], got[1])
        except ImportError_ as e:
            w.toast(app.page, str(e), error=True)
            return
        stale = bool(d.pending)
        cart_has = any(q > 0 for q in d.cart.values())
        different = bool(payload.supplier and d.supplier and payload.supplier.strip().lower() != d.supplier.strip().lower())

        async def run(clear_first: bool):
            if clear_first:
                d.cart.clear(); d.costs.clear(); d.pending.clear(); d.supplier = d.supplier_invoice_number = d.notes = ""
                d.paid = None
            res = led.apply_import(d, payload.items)
            if res.matched:
                await led._save_inventory()
            if payload.supplier:
                d.supplier = payload.supplier
            if payload.supplier_invoice_number:
                d.supplier_invoice_number = payload.supplier_invoice_number
            if payload.notes:
                d.notes = payload.notes
            parts = [f"تم: {len(res.matched)} صنف مطابق للمخزون"]
            if res.pending:
                parts.append(f"{len(res.pending)} صنف جديد بانتظار التسعير")
            if res.fixed_up:
                parts.append("بعض الكميات/الأسعار كانت ناقصة وضُبطت مؤقتاً — راجعها")
            if res.needs_review:
                parts.append(f"{res.needs_review} صنف ناقصه الاسم أو الكود")
            if res.skipped:
                parts.append(f"{res.skipped} سطر فارغ تُجاهل")
            w.toast(app.page, "، ".join(parts))

        if stale or (cart_has and different):
            msg = (f"في فاتورة شراء جارية فيها {len(d.pending)} صنف من استيراد سابق لم يؤكَّد تسعيره."
                   if stale else f"في فاتورة جارية للمورد «{d.supplier}» وهذا الملف لمورد مختلف («{payload.supplier}»).")
            w.choice_dialog(app, "⚠ فيه فاتورة شراء غير مكتملة", [
                ("إضافة الملف فوق الموجود (نفس الفاتورة)", lambda: run(False)),
                ("إفراغ القديم ثم استيراد الملف", lambda: run(True))], msg)
        else:
            await run(False)
            await app.after_change()

    # ---- أصناف جديدة مستوردة تحتاج تسعير ----
    def pending_panel() -> ft.Control:
        if not d.pending:
            return ft.Container()
        cards = []
        for idx, p in enumerate(d.pending):
            def setter(attr, num=False, p=p):
                def h(e):
                    v = e.control.value
                    if not num:
                        setattr(p, attr, v or "")
                    else:
                        n = parse_num(v)
                        setattr(p, attr, (int(n) if attr == "qty" and n is not None else n) if n is not None else (0 if attr in ("qty", "cost") else None))
                return h

            def rm(_, idx=idx):
                d.pending.pop(idx); app.page.run_task(app.after_change)
            show = lambda x: "" if x is None else fmt_money(x)  # noqa: E731
            cards.append(w.card(ft.Column([
                w.field("اسم الصنف", p.name, on_change=setter("name")), w.field("الكود", p.code, on_change=setter("code")),
                ft.Row([w.field("الكمية", p.qty, kind="int", on_change=setter("qty", True), expand=True),
                        w.field("تكلفة الشراء", show(p.cost), kind="num", on_change=setter("cost", True), expand=True)]),
                ft.Row([w.field("جملة", show(p.price_wholesale), kind="num", on_change=setter("price_wholesale", True), expand=True),
                        w.field("توزيع", show(p.price_distribution), kind="num", on_change=setter("price_distribution", True), expand=True),
                        w.field("مفرق *", show(p.price), kind="num", on_change=setter("price", True), expand=True)]),
                w.btn("🗑 حذف هذا الصنف من الاستيراد", rm, "danger")], spacing=6), bgcolor=w.AMBER_TINT))

        async def confirm(_):
            try:
                n = await led.confirm_pending(d)
            except LedgerError as e:
                w.toast(app.page, str(e), error=True)
                return
            w.toast(app.page, f"أُضيف {n} صنف جديد للمخزون وللفاتورة")
            await app.after_change()
        return ft.Column([w.title(f"⚠ أصناف جديدة مستوردة تحتاج تسعير ({len(d.pending)})", 15), *cards,
                          w.btn("✓ تأكيد الأسعار وإضافة الأصناف", confirm, "success")], spacing=8)

    # ---- صنف جديد يدوياً ----
    def new_item(_):
        img_state: dict = {}

        async def submit(v):
            from ...data.images import fingerprint
            jpeg = img_state.get("jpeg")
            it = await led.add_new_item_to_purchase(d, v["name"], v["code"], int(parse_num(v["qty"]) or 0), v["cost"],
                                                    v["price"], v["wholesale"], v["distribution"],
                                                    img=fingerprint(jpeg) if jpeg else None)
            if jpeg:
                await app.images.put(it.id, jpeg, full=img_state.get("full"))
        w.form_dialog(app, "+ صنف جديد (غير موجود بالمخزون)", [
            ("name", "اسم الصنف", "", "text"), ("code", "الكود", "", "text"), ("qty", "الكمية المشتراة الآن", "1", "int"),
            ("cost", "تكلفة الشراء للقطعة", "", "num"), ("wholesale", "سعر الجملة (فارغ = المفرق)", "", "num"),
            ("distribution", "سعر التوزيع (فارغ = المفرق)", "", "num"), ("price", "سعر المفرق", "", "num")], submit, "إضافة",
            extra=[w.image_picker(app, img_state)])

    # ---- إنهاء ----
    async def done(_):
        try:
            p = await led.finalize_purchase(d)
        except LedgerError as e:
            w.toast(app.page, str(e), error=True)
            return
        d.__dict__.update(PurchaseDraft().__dict__)
        app.drafts.clear_purchase()
        await app.show_document(purchase_doc(p, led.settings, app.line_images(p)), p.number, back=("purchases", {}))

    def clear(_):
        editing = bool(d.editing_id)
        msg = (f"أنت تعدّل فاتورة شراء موجودة ({d.editing_number}). حذفها الآن سيحذفها نهائياً من السجل. متابعة؟"
               if editing else "حذف فاتورة الشراء الحالية؟")

        async def go():
            if editing:
                await led.discard_editing_purchase(d)
            d.__dict__.update(PurchaseDraft().__dict__)
            app.drafts.clear_purchase()
        w.confirm_dialog(app, "حذف فاتورة الشراء", msg, go, "حذف", True)

    async def cancel_edit(_):
        await led.cancel_edit_purchase(d)
        d.__dict__.update(PurchaseDraft().__dict__)
        app.drafts.clear_purchase()
        w.toast(app.page, "تم إلغاء التعديل وعادت فاتورة الشراء كما كانت")
        await app.after_change()

    # ---- السجل ----
    def render_history() -> None:
        q = state["hq"]
        shown = [p for p in led.purchases if not q or fuzzy_match(p.number, q) or fuzzy_match(p.supplier, q)
                 or fuzzy_match(p.supplier_invoice_number, q)]
        hist_box.controls = [w.muted(f"{len(shown)} من {len(led.purchases)}"),
                             *[_history_card(app, p) for p in shown[:100]]] if shown else [w.empty("لا توجد فواتير شراء")]

    def on_hsearch(q: str) -> None:
        state["hq"] = q; render_history(); app.page.update()

    # ---- بناء ----
    def sup_changed(e):
        d.supplier = e.control.value or ""; save_draft()

    def supinv_changed(e):
        d.supplier_invoice_number = e.control.value or ""; save_draft()

    def paid_changed(e):
        v = (e.control.value or "").strip()
        d.paid = None if v == "" else q2(parse_num(v) or ZERO); refresh_totals(); app.page.update()

    def notes_changed(e):
        d.notes = e.control.value or ""; save_draft()

    render_picks(animate=True); render_cart(); refresh_totals(); render_history()
    has_lines = any(q > 0 for q in d.cart.values())
    top: list[ft.Control] = []
    if d.editing_id:
        top.append(w.banner(f"✏️ تعديل فاتورة الشراء {d.editing_number} — لن تُحفظ التعديلات حتى تضغط «تم».", "amber"))
    btns = [w.btn("✔ تم", done, "success", expand=True)]
    if d.editing_id:
        btns.append(w.btn("إلغاء التعديل", cancel_edit, "ghost"))
    if has_lines or d.editing_id:
        btns.append(w.btn("🗑 حذف", clear, "danger"))

    return w.with_scroll_nav(ft.Column([
        *top, pending_panel(),
        w.card(ft.Column([w.title("بيانات فاتورة الشراء", 15),
                          w.field("اسم الشركة / المورد (مطلوب)", d.supplier, on_change=sup_changed),
                          ft.Row([ft.TextButton(s.name, on_click=lambda e, n=s.name: _set_supplier(app, n)) for s in led.suppliers[:6]], wrap=True),
                          w.field("رقم فاتورة المورد (اختياري)", d.supplier_invoice_number, on_change=supinv_changed)], spacing=6)),
        w.title("بنود فاتورة الشراء", 15),
        ft.Row([w.btn("📂 استيراد من ملف (Excel / CSV / JSON)", import_file, "ghost"),
                w.btn("📷 مسح فاتورة بالكاميرا", lambda _: app.page.run_task(app.go, "scan"), "ghost")], wrap=True),
        w.search_bar(app, on_search, "اكتب اسم أو كود الصنف…"), picks, more_btn,
        w.btn("+ صنف جديد (غير موجود بالمخزون)", new_item, "ghost"),
        w.title("فاتورة الشراء الحالية", 15), cart_box,
        w.field("المبلغ المدفوع الآن (فارغ = كامل)", "" if d.paid is None else fmt_num(d.paid), kind="num", on_change=paid_changed),
        w.field("ملاحظات", d.notes, on_change=notes_changed), w.card(totals_box), ft.Row(btns, spacing=8),
        w.title("سجل المشتريات", 16), w.search_bar(app, on_hsearch, "ابحث برقم الفاتورة أو اسم المورد…", barcode=False), hist_box,
    ], scroll=w.smooth_scroll(), expand=True, spacing=10))


def _set_supplier(app, name: str) -> None:
    app.purchase_draft.supplier = name
    app.page.run_task(app.after_change)


def _history_card(app, p) -> ft.Control:
    led = app.ledger

    async def view(_):
        await app.show_document(purchase_doc(p, led.settings, app.line_images(p)), p.number, back=("purchases", {}))

    async def edit(_):
        d = app.purchase_draft
        if any(q > 0 for q in d.cart.values()):
            w.toast(app.page, "في فاتورة شراء قيد العمل — أنهِها أو احذفها أولاً", error=True)
            return
        try:
            draft, warnings = await led.begin_edit_purchase(p.id)
        except LedgerError as e:
            w.toast(app.page, str(e), error=True)
            return
        app.purchase_draft = draft
        for m in warnings:
            w.toast(app.page, "⚠ " + m)
        await app.after_change()

    async def pricing(_):
        await app.go("purchases", pricing=p.id)

    def delete(_):
        w.confirm_dialog(app, "حذف فاتورة الشراء", "حذف فاتورة الشراء هذه نهائياً؟ ستنقص كمياتها من المخزون.",
                         lambda: led.delete_purchase(p.id), "حذف", True)
    return w.card(ft.Column([
        ft.Row([ft.Text(p.number, weight=ft.FontWeight.BOLD, expand=True), ft.Text(fmt_money(p.subtotal), weight=ft.FontWeight.BOLD)]),
        w.muted(f"{p.supplier} · {arabic_date(p.date)} · {len(p.items)} صنف" + (f" · متبقي {fmt_money(p.remaining)}" if p.remaining > 0 else "")),
        ft.Row([w.btn("👁 عرض / طباعة", view), w.btn("✎ تعديل", edit, "ghost"), w.btn("تعديل الأسعار", pricing, "ghost"),
                w.btn("حذف", delete, "danger")], wrap=True, spacing=6)], spacing=6))


def _pricing_view(app, purchase_id: str) -> ft.Control:
    """تعديل أسعار البيع لأصناف فاتورة شراء بعد إنشائها (renderPurchasePricingEditDoc)."""
    led = app.ledger
    p = next((x for x in led.purchases if x.id == purchase_id), None)

    async def back(_):
        await app.go("purchases")
    if not p:
        return ft.Column([w.btn("رجوع", back, "ghost"), w.empty("الفاتورة غير موجودة")])
    rows: dict[str, dict] = {}
    cards = []
    for ln in p.items:
        it = led._item(ln.id)
        if not it:
            cards.append(w.card(w.muted(f"«{ln.name}» لم يعد موجوداً بالمخزون")))
            continue
        rows[ln.id] = {"retail": it.price, "wholesale": it.price_wholesale or it.price, "dist": it.price_distribution or it.price}

        def ch(key, iid=ln.id):
            def h(e):
                rows[iid][key] = e.control.value
            return h
        cards.append(w.card(ft.Column([
            ft.Text(ln.name, weight=ft.FontWeight.BOLD), w.muted(f"تكلفة الشراء {fmt_money(ln.cost)}"),
            ft.Row([w.field("مفرق *", fmt_num(it.price), kind="num", on_change=ch("retail"), expand=True),
                    w.field("جملة", fmt_num(it.price_wholesale or it.price), kind="num", on_change=ch("wholesale"), expand=True),
                    w.field("توزيع", fmt_num(it.price_distribution or it.price), kind="num", on_change=ch("dist"), expand=True)])],
            spacing=6)))

    async def save(_):
        try:
            up, miss = await led.update_purchase_pricing(p.id, {k: (v["retail"], v["wholesale"], v["dist"]) for k, v in rows.items()})
        except LedgerError as e:
            w.toast(app.page, str(e), error=True)
            return
        w.toast(app.page, f"تم تحديث أسعار {up} صنف" + (f" ({miss} لم يعد موجوداً)" if miss else ""))
        await app.go("purchases")
    return ft.Column([ft.Row([w.btn("رجوع", back, "ghost"), w.title(f"تعديل أسعار البيع — {p.number}", 16)]), *cards,
                      w.btn("حفظ الأسعار", save, "success")], scroll=w.smooth_scroll(), expand=True, spacing=10)
