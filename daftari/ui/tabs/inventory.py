"""تبويب المخزون (renderInventoryTab)."""
from __future__ import annotations

import flet as ft

from ...core.money import fmt_money, fmt_num, parse_num
from ...core.reports import item_movement, loss_items, low_stock_items
from ...core.search import search_inventory
from .. import widgets as w


PAGE_SIZE = 60


def build(app) -> ft.Control:
    led = app.ledger
    list_box = w.LazyList()          # ListView كسول: ترويسة المخزون + البطاقات أبناء مباشرون (تحميل عند الحاجة)
    state = {"q": "", "all": False}

    # ---- تنبيهات ----
    banners: list[ft.Control] = []
    low = low_stock_items(led.inventory)
    if low:
        names = "، ".join(f"{i.name} ({i.stock})" for i in low[:8]) + (" …" if len(low) > 8 else "")
        banners.append(w.banner(f"⚠ {len(low)} صنف مخزونه قارب على النفاد (3 أو أقل): {names}", "amber"))
    loss = loss_items(led.inventory)
    if loss:
        lines = "، ".join(f"{l.item.name} (رأس المال {fmt_money(l.cost)})" for l in loss[:5])
        banners.append(w.banner(f"🔴 {len(loss)} صنف سعر بيعه أقل من رأس ماله: {lines}", "red"))

    # ---- ملخّص ----
    def show_capital(_):
        w.info_dialog(app, "رأس المال المجمَّد في المخزون",
                      ft.Text(fmt_money(led.capital()), size=26, weight=ft.FontWeight.BOLD, color=w.TEAL))

    summary = ft.Row([w.kpi("عدد الأصناف", str(len(led.inventory)), w.TEAL, ft.Icons.INVENTORY_2_OUTLINED, w.TINT),
                      w.kpi("إجمالي القطع", str(sum(i.stock for i in led.inventory)), w.INK, ft.Icons.NUMBERS, w.SLATE_TINT),
                      w.kpi("منخفضة المخزون", str(len(low)), w.RED if low else w.INK, ft.Icons.WARNING_AMBER_OUTLINED,
                            w.RED_TINT if low else w.SLATE_TINT)], spacing=10)

    # ---- إعادة تعبئة بالكود ----
    code_tf = w.field("كود الصنف", "", expand=True)
    qty_tf = w.field("الكمية", "1", kind="int", width=90)

    async def scan_code(_):
        code = await app.scan_barcode()
        if code:
            code_tf.value = code
            app.page.update()

    async def restock(_):
        try:
            qty = int(parse_num(qty_tf.value) or 0)
            it = await led.restock_by_code(code_tf.value or "", qty)
        except Exception as e:  # noqa: BLE001
            w.toast(app.page, str(e), error=True)
            return
        w.toast(app.page, f"أُضيف {qty} إلى «{it.name}» — المخزون الآن {it.stock}")
        await app.after_change()

    restock_card = w.card(ft.Column([
        ft.Row([w.icon_tile(ft.Icons.ADD_BOX_OUTLINED, w.TEAL, w.TINT, 32),
                ft.Text("إضافة كمية لصنف موجود (بالكود)", weight=ft.FontWeight.W_700)], spacing=10),
        ft.Row([code_tf, ft.IconButton(icon=ft.Icons.QR_CODE_SCANNER, on_click=scan_code), qty_tf,
                w.btn("إضافة", restock)], vertical_alignment=ft.CrossAxisAlignment.CENTER)], spacing=6))

    # ---- إضافة صنف جديد ----
    def add_item(_):
        img_state: dict = {}

        async def submit(v):
            from ...data.images import fingerprint
            jpeg = img_state.get("jpeg")
            it = await led.add_item(v["name"], v["code"], v["cost"], v["price"], int(parse_num(v["stock"]) or 0),
                                    v["wholesale"], v["distribution"], img=fingerprint(jpeg) if jpeg else None)
            if jpeg:
                await app.images.put(it.id, jpeg, full=img_state.get("full"))
        w.form_dialog(app, "إضافة صنف جديد", [
            ("name", "اسم الصنف", "", "text"), ("code", "الكود (اختياري)", "", "text"),
            ("cost", "رأس المال (التكلفة)", "", "num"), ("price", "سعر المفرق", "", "num"),
            ("wholesale", "سعر الجملة (فارغ = المفرق)", "", "num"),
            ("distribution", "سعر التوزيع (فارغ = المفرق)", "", "num"), ("stock", "الكمية", "0", "int")], submit, "إضافة",
            extra=[w.image_picker(app, img_state)])

    # ---- قائمة الأصناف ----
    def render_list(animate: bool = False, force: bool = False, start: int = 0) -> None:
        items = search_inventory(led.inventory, state["q"])
        shown = items if state["all"] else items[:PAGE_SIZE]       # 60 بطاقة فقط في البداية = برنامج أسرع بكثير
        list_box.controls = [_item_card(app, it) for it in shown] or [w.empty("لا توجد أصناف مطابقة")]
        if len(items) > len(shown):
            def more(_):
                state["all"] = True
                render_list(animate=True, force=True, start=PAGE_SIZE); app.page.update()
            list_box.controls.append(ft.Container(w.btn(f"عرض الكل ({len(items)})", more, "ghost"), padding=8))
        if animate:
            w.reveal(app, list_box.controls, force=force, start=start)
        list_box.sync()

    def on_search(q: str) -> None:
        state["q"], state["all"] = q, False
        render_list(animate=True, force=True)
        app.page.update()

    render_list(animate=True)
    list_box.fixed.extend([
        w.page_header("المخزون", f"{len(led.inventory)} صنف",
                      w.btn("💰 رأس المال", show_capital, "ghost", small=True), w.btn("➕ صنف جديد", add_item)),
        *banners, summary, restock_card,
        w.search_bar(app, on_search, "ابحث بالاسم أو الكود…")])
    list_box.sync()
    return w.with_scroll_nav(list_box.view)


def _item_card(app, it) -> ft.Control:
    led = app.ledger
    low = it.stock <= 3
    loss = it.cost > min(it.price, it.price_wholesale or it.price, it.price_distribution or it.price)

    def edit(_):
        img_state: dict = {}

        async def submit(v):
            await led.edit_item(it.id, v["name"], v["code"], v["stock"])
            await w.save_picked_image(app, img_state, it.id)
        w.form_dialog(app, "تعديل الصنف", [("name", "الاسم", it.name, "text"), ("code", "الكود", it.code, "text"),
                                             ("stock", "الكمية بالمخزون", it.stock, "int")], submit,
                      extra=[w.image_picker(app, img_state, it)])

    def pricing(_):
        async def submit(v):
            await led.edit_item_pricing(it.id, v["cost"], v["wholesale"], v["distribution"], v["price"])
        w.form_dialog(app, "تسعير الصنف", [
            ("cost", "رأس المال", fmt_num(it.cost), "num"), ("wholesale", "سعر الجملة", fmt_num(it.price_wholesale or it.price), "num"),
            ("distribution", "سعر التوزيع", fmt_num(it.price_distribution or it.price), "num"),
            ("price", "سعر المفرق", fmt_num(it.price), "num")], submit, subtitle=it.name)

    def movement(_):
        rows = item_movement(it, led.invoices, led.purchases)
        from ...printing.render import arabic_date
        body = ft.Column([ft.Row([ft.Text(f"{arabic_date(m.date)} — {m.kind} ({m.ref})", expand=True, size=13),
                                  ft.Text(("+" if m.qty > 0 else "") + str(m.qty), color=w.GREEN if m.qty > 0 else w.RED,
                                          weight=ft.FontWeight.BOLD)]) for m in rows[:60]]
                         or [w.empty("لا توجد حركات")], scroll=w.smooth_scroll(), height=360, spacing=6)
        w.info_dialog(app, f"حركة «{it.name}»", body)

    def delete(_):
        w.confirm_dialog(app, "حذف الصنف", f"حذف «{it.name}» نهائياً من المخزون؟",
                         lambda: led.delete_item(it.id), "حذف", True)

    def price_cell(label: str, value, color: str = w.INK):
        return ft.Column([w.muted(label, 11), ft.Text(fmt_money(value), size=13, weight=ft.FontWeight.W_700, color=color)],
                         spacing=0, expand=True)

    stock_pill = w.pill("نفد" if it.stock <= 0 else f"{it.stock} قطعة", "red" if low else "green")
    return w.row_card(ft.Column([
        ft.Row([w.thumb(app, it, 96, zoom=True),
                ft.Column([ft.Text(it.name, weight=ft.FontWeight.W_700, size=14),
                           w.muted(f"الكود: {it.code or '—'}", 12)], spacing=1, expand=True),
                stock_pill], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12),
        ft.Row([price_cell("رأس المال", it.cost, w.RED if loss else w.INK), price_cell("مفرق", it.price),
                price_cell("جملة", it.price_wholesale or it.price), price_cell("توزيع", it.price_distribution or it.price)],
               spacing=8),
        *([w.banner("سعر البيع أقل من رأس المال", "red")] if loss else []),
        ft.Row([w.btn("تعديل", edit, "ghost", small=True), w.btn("تسعير", pricing, "ghost", small=True),
                w.btn("حركة", movement, "ghost", small=True), w.btn("حذف", delete, "danger", small=True)],
               wrap=True, spacing=6, run_spacing=6)], spacing=10))
