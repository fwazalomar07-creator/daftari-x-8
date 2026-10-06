"""حارس دورة حياة العناصر: يمنع انهيار «RuntimeError: Text(…) Control must be added to the page first».

السبب: معالجات الأحداث غير المتزامنة (حفظ مزوّد، اختبار اتصال، تحميل…) تنتظر الشبكة ثم تستدعي control.update(). وفي أثناء الانتظار
قد يُعاد رسم الشاشة فيُفصل العنصر القديم عن الصفحة، فيرفع Flet الخطأ ويظهر البالون الأحمر ويتوقف المعالج.

الحل على مستويين:
  1) safe_update(*controls): يحدّث العنصر فقط إن كان ما زال في الصفحة ويتجاهل حالة «غير مضاف» بصمت.
  2) install(): شبكة أمان عامة تلفّ BaseControl.update فلا يُسقط أي استدعاء قديم/مستقبلي التطبيق. أي RuntimeError آخر يمرّ كما هو.
"""
from __future__ import annotations

_MARK = "must be added to the page"
_installed = False


def is_detached_error(e: BaseException) -> bool:
    return isinstance(e, RuntimeError) and _MARK in str(e).lower()


def safe_update(*controls) -> None:
    for c in controls:
        if c is None:
            continue
        try:
            c.update()
        except RuntimeError as e:
            if not is_detached_error(e):
                raise


def install() -> bool:
    """يلفّ update() لأصل كل العناصر في Flet. آمن للاستدعاء أكثر من مرة. يرجع True إن طُبّق."""
    global _installed
    if _installed:
        return True
    try:
        import flet as ft
    except Exception:  # noqa: BLE001
        return False
    base = None
    try:
        from flet.controls.base_control import BaseControl as base          # Flet >= 0.80
    except Exception:  # noqa: BLE001
        base = getattr(ft, "BaseControl", None) or getattr(ft, "Control", None)
    orig = getattr(base, "update", None) if base is not None else None
    if orig is None or getattr(orig, "_daftari_safe", False):
        return bool(orig)

    def update(self, *a, **k):
        try:
            return orig(self, *a, **k)
        except RuntimeError as e:
            if is_detached_error(e):
                return None
            raise
    update._daftari_safe = True          # type: ignore[attr-defined]
    update.__wrapped__ = orig            # type: ignore[attr-defined]
    try:
        base.update = update
    except Exception:  # noqa: BLE001
        return False
    _installed = True
    return True
