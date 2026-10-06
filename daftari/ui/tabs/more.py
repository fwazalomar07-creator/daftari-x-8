"""صفحة «المزيد»: بوابة لباقي الأقسام (تظهر في وضع الهاتف)."""
from __future__ import annotations

import flet as ft

from ...core.money import fmt_money
from .. import widgets as w
from ..app import MORE_NAV

# أقسام لا تظهر بالشريط السفلي للهاتف فنضعها هنا
PHONE_EXTRA = [("history", "سجل الفواتير", ft.Icons.RECEIPT_LONG_OUTLINED),
               ("profit", "الأرباح", ft.Icons.TRENDING_UP)]


def build(app) -> ft.Control:
    led = app.ledger
    owed_by_customers = sum((c.balance for c in led.customers), 0)
    we_owe = sum((s.balance for s in led.suppliers if s.type == "we_owe"), 0)

    def make(key: str):
        async def click(_):
            await app.go(key)
        return click

    tiles = [
        w.card(ft.Row([ft.Container(ft.Icon(icon, color=w.TEAL, size=22), bgcolor=w.TINT,
                                    padding=10, border_radius=12),
                       ft.Text(label, size=15, weight=ft.FontWeight.W_600, color=w.INK,
                               expand=True),
                       ft.Icon(ft.Icons.CHEVRON_LEFT, color=w.SOFT, size=20)],
                      vertical_alignment=ft.CrossAxisAlignment.CENTER),
               on_click=make(key), padding=12)
        for key, label, icon in PHONE_EXTRA + MORE_NAV
    ]

    async def lock(_):
        app.unlocked = False
        await app.render()
    extra = [w.btn("🔒 قفل التطبيق", lock, "ghost")] if led.has_password else []
    return ft.Column([
        ft.Row([w.stat("ديون العملاء لنا", fmt_money(owed_by_customers), w.RED if owed_by_customers > 0 else w.INK),
                w.stat("ديوننا للموردين", fmt_money(we_owe), w.RED if we_owe > 0 else w.INK)], spacing=8),
        *tiles, *extra], scroll=w.smooth_scroll(), expand=True, spacing=8)
