"""شاشة الدخول (Intro): شعار العمر يظهر بحركة تكبير + تلاشٍ أثناء تحميل البيانات، ثم يتلاشى.

مبادئ التصميم:
  • لا تؤخّر الإقلاع: تُعرض فوراً وتعمل بالتوازي مع تحميل المخزون/Supabase، وأقل مدة عرض 1.6 ثانية فقط
    (إن كان التحميل أبطأ تبقى حتى ينتهي).
  • لا تكسر التطبيق أبداً: أي خطأ في رسمها أو إصدار Flet لا يعرف خاصية ما → نتجاوزها بصمت ويكمل البرنامج.
  • للإيقاف: متغير البيئة DAFTARI_INTRO=0 أو مفتاح الإعدادات introScreen=false.
  • الصورة assets/intro_logo.png (PNG شفاف) — استبدلها بنفس الاسم لتغيير الشعار.
"""
from __future__ import annotations

import asyncio
import os
import time

import flet as ft

MIN_SHOW_SECONDS = 1.6
BG = "#FFFFFF"
INK = "#14375E"          # أزرق شعار العمر


def enabled(settings: dict | None = None) -> bool:
    if (os.getenv("DAFTARI_INTRO") or "").strip().lower() in ("0", "false", "no", "off"):
        return False
    v = (settings or {}).get("introScreen")
    return v is not False and str(v).lower() not in ("false", "0", "off")


def _anim(ms: int):
    try:
        return ft.Animation(ms, ft.AnimationCurve.EASE_OUT)
    except Exception:  # noqa: BLE001
        return None


class Intro:
    def __init__(self, page: ft.Page):
        self.page = page
        self.t0 = time.monotonic()
        self._root = None
        self._logo = None
        self._shown = False

    def _logo_width(self) -> int:
        try:
            w = float(self.page.width or 0)
        except Exception:  # noqa: BLE001
            w = 0.0
        return int(max(180, min(w * 0.72 if w else 320, 380)))

    async def show(self) -> None:
        try:
            self._logo = ft.Container(
                content=ft.Image(src="intro_logo.png", width=self._logo_width(), fit=ft.BoxFit.CONTAIN),
                opacity=0, scale=0.82, animate_opacity=_anim(700), animate_scale=_anim(900),
            )
            bar = ft.ProgressBar(width=120, color=INK, bgcolor="#E3EAF2", bar_height=3)
            self._root = ft.Container(
                content=ft.Column(
                    [self._logo, ft.Container(bar, margin=ft.Margin(0, 18, 0, 0))],
                    alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=0,
                ),
                bgcolor=BG, expand=True, alignment=ft.Alignment(0, 0),
                opacity=1, animate_opacity=_anim(450),
            )
            self.page.bgcolor = BG
            self.page.overlay.append(self._root)
            self.page.update()
            self._shown = True
            await asyncio.sleep(0.05)
            self._logo.opacity, self._logo.scale = 1, 1          # تبدأ حركة الدخول
            self.page.update()
        except Exception as e:  # noqa: BLE001 — الحركة تجميلية: لا تُسقط التطبيق
            print("intro:", e)
            self._shown = False

    async def hide(self) -> None:
        """يُستدعى بعد جاهزية الواجهة: ينتظر بقية أقل مدة عرض ثم يتلاشى ويُزال."""
        if not self._shown:
            return
        try:
            left = MIN_SHOW_SECONDS - (time.monotonic() - self.t0)
            if left > 0:
                await asyncio.sleep(left)
            self._root.opacity = 0
            self.page.update()
            await asyncio.sleep(0.5)
        except Exception as e:  # noqa: BLE001
            print("intro hide:", e)
        finally:
            try:
                self.page.overlay.remove(self._root)
                self.page.update()
            except Exception:  # noqa: BLE001
                pass
            self._shown = False
