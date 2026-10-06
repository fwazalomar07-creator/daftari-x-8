"""نقطة دخول البناء (flet build → apk / exe / web) وتشغيل التطوير.

تبقى هذه الشاشة في جذر المشروع بجانب المجلد daftari/ . أدوات البناء تشغّلها كسكربت مستقل
(لا حزمة أب)، لذلك: لا استيراد نسبي هنا أبداً، ونضيف مجلد الملف إلى sys.path بأنفسنا.

إن فشل الاستيراد لأي سبب (مكتبة ناقصة مثلاً) لا نترك «بالون الخطأ الأحمر» الغامض؛ نعرض شاشة
تشخيص واضحة فيها نص الخطأ الكامل وزر نسخ، فيُعرف السبب من أول مرة.
"""
import sys
import traceback
from pathlib import Path

_HERE = Path(globals().get("__file__") or sys.argv[0] or ".").resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _diagnostic_app(details: str):
    import flet as ft

    def _main(page: "ft.Page"):
        page.title = "Daftari — خطأ في التشغيل"
        page.scroll = "auto"
        try:
            page.rtl = True
        except Exception:  # noqa: BLE001
            pass

        clip = None
        try:
            clip = ft.Clipboard()                       # Flet >= 0.80: خدمة تُضاف للصفحة
            page.services.append(clip)
        except Exception:  # noqa: BLE001
            clip = None

        async def _copy(_):
            try:
                if clip is not None:
                    await clip.set(details)
                else:
                    page.set_clipboard(details)         # إصدارات أقدم
            except Exception:  # noqa: BLE001
                pass

        page.add(ft.SafeArea(ft.Column([
            ft.Text("تعذّر تشغيل البرنامج", size=22, weight=ft.FontWeight.BOLD),
            ft.Text("أرسل هذا النص كما هو ليُصلَح السبب:", size=14),
            ft.Button("نسخ الخطأ", on_click=_copy),
            ft.Text(details, selectable=True, size=12),
        ], spacing=12, scroll=ft.ScrollMode.AUTO), expand=True))

    try:
        ft.run(_main)
    except AttributeError:      # flet < 0.80
        ft.app(target=_main)


def _start() -> None:
    try:
        from daftari.main import run
    except Exception:  # noqa: BLE001
        _diagnostic_app(traceback.format_exc())
        return
    run()


if __name__ == "__main__":
    _start()
