"""نقطة التشغيل.   من المجلد الأب:  flet run daftari/main.py    أو    python -m daftari.main

الإعدادات تُقرأ من ملف .env (بجانب هذا الملف) أو من متغيرات البيئة — لا تضعها داخل الكود:
    SUPABASE_URL, SUPABASE_KEY   — بدونهما يعمل التطبيق محلياً فقط (كاش ملفات)
    GEMINI_API_KEY, GEMINI_MODEL — لتشغيل «مساعد شركة العمر»
    DAFTARI_HOME                 — مجلد البيانات المحلية (الافتراضي ~/.daftari)
    DAFTARI_FONT                 — مسار خط عربي لطباعة المستندات (اختياري)
"""
import os
import sys
from pathlib import Path

# ── يعمل بأي طريقة تشغيل ──────────────────────────────────────────────────────────
# السبب القديم لخطأ «attempted relative import with no known parent package» على الجوال:
# أدوات البناء (flet build / serious_python) تشغّل main.py كسكربت مستقل (__package__ فارغ)، وأحياناً
# تضع محتويات مجلد daftari مسطّحةً في جذر التطبيق، فلا توجد حزمة اسمها daftari أصلاً.
# الحل هنا لا يعتمد على __init__.py ولا على مكان الملف: نسجّل «حزمة» daftari يدوياً (ModuleType)
# مساراتها مجلد هذا الملف، فتعمل كل الاستيرادات النسبية في بقية الوحدات في أي تخطيط.
if not __package__:
    import types as _types
    _here = Path(globals().get("__file__") or sys.argv[0] or ".").resolve().parent
    if "daftari" not in sys.modules:
        _pkg = _types.ModuleType("daftari")
        _pkg.__path__ = [str(_here)]          # type: ignore[attr-defined]
        _pkg.__file__ = str(_here / "__init__.py")
        _pkg.__package__ = "daftari"
        sys.modules["daftari"] = _pkg
    else:
        _pkg = sys.modules["daftari"]
        if str(_here) not in list(getattr(_pkg, "__path__", [])):
            _pkg.__path__ = [*getattr(_pkg, "__path__", []), str(_here)]   # type: ignore[attr-defined]
    __package__ = "daftari"

import flet as ft

from daftari.core.env import load_env
from daftari.data.drafts import DraftStore
from daftari.data.repository import Repository
from daftari.data.store import SupabaseStore
from daftari.ledger import Ledger
from daftari.ui.app import App


def _resolve_home(page: ft.Page) -> Path:
    env = os.getenv("DAFTARI_HOME")
    if env:
        return Path(env)
    plat = str(getattr(page, "platform", "")).lower()
    storage = os.getenv("FLET_APP_STORAGE_DATA")
    if storage and ("android" in plat or "ios" in plat):   # الجوال: مجلد المستخدم غير قابل للكتابة
        home = Path(storage)
        os.environ["DAFTARI_HOME"] = str(home)             # ليلتقطه core/env.py وبقية الوحدات
        return home
    return Path.home() / ".daftari"


async def main(page: ft.Page):
    try:
        from daftari.ui.safe import install as _install_safe_update
        _install_safe_update()          # شبكة أمان: لا انهيار بسبب update() على عنصر خارج الصفحة
    except Exception as e:  # noqa: BLE001
        print("safe-update:", e)
    home = _resolve_home(page)
    load_env()
    # شاشة الدخول: تظهر فوراً وتعمل بالتوازي مع التحميل (تُتجاوز بصمت عند أي مشكلة أو إن عُطّلت)
    intro = None
    try:
        from daftari.ui.intro import Intro, enabled as intro_enabled
        if intro_enabled():
            intro = Intro(page)
            await intro.show()
    except Exception as e:  # noqa: BLE001
        print("intro:", e)
        intro = None
    store, notice = None, None
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if url and key:
        try:
            store = await SupabaseStore.create(url, key)
        except Exception as e:  # noqa: BLE001
            notice = f"تعذّر الاتصال بـ Supabase — وضع محلي فقط ({e})"

    repo = Repository(store, cache_dir=home / "cache")
    ledger = Ledger(repo)
    # فتح سريع: إن وُجد كاش محلي نعرضه فوراً (بلا انتظار الإنترنت) ثم نحدّث من السحابة بالخلفية
    fast = store is not None and repo.has_cache("inventory")
    await ledger.load(cloud=not fast)
    if store is None and notice is None:
        notice = "لم يُعثر على SUPABASE_URL / SUPABASE_KEY (ملف .env) — وضع محلي فقط، لن تظهر أصناف السحابة."
    notice = notice or (None if fast else ledger.health_notice())
    app = App(page, ledger, DraftStore(home / "drafts"), work_dir=home / "exports", home=home, store=store)
    await app.start()
    if intro is not None:
        page.run_task(intro.hide)          # يتلاشى بعد أقل مدة عرض دون تعطيل بقية الإقلاع
    if fast:
        page.run_task(app.cloud_boot)
    else:
        app.queue_images()
    if notice:
        from daftari.ui.widgets import toast
        toast(page, notice, error=True)


def run() -> None:
    from daftari.printing.render import ASSETS
    packaged = bool(os.getenv("FLET_APP_STORAGE_DATA"))   # تطبيق مبني بـ flet build: لا تغيّر العرض/المنفذ
    web = not packaged and (os.getenv("FLET_WEB") or "").strip().lower() in ("1", "true", "yes", "web")
    port = 0 if packaged else int(os.getenv("PORT") or os.getenv("FLET_PORT") or ("8080" if web else "0") or 0)
    host = None if packaged else (os.getenv("FLET_HOST") or ("0.0.0.0" if web else None))
    kw = {"assets_dir": str(ASSETS)}
    if web or port:
        view = getattr(getattr(ft, "AppView", None), "WEB_BROWSER", None)
        if view is not None:
            kw["view"] = view
        kw["port"] = port or 8080
        if host:
            kw["host"] = host
    try:
        ft.run(main, **kw)
    except TypeError:
        ft.run(main, assets_dir=str(ASSETS))


if __name__ == "__main__":
    run()
