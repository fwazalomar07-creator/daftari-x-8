"""قراءة الباركود من صورة بـ OpenCV (cv2.barcode — يدعم EAN-13/EAN-8/UPC وغيرها).

بديل «📷 مسح باركود» في الأصل (كان يستخدم BarcodeDetector في المتصفح مع الكاميرا الحية).
هنا يلتقط المستخدم صورة للباركود (أو يختارها من المعرض) فنقرأ الرقم ونضعه في خانة البحث.
نجرّب عدة تحسينات للصورة (تكبير/رمادي/تباين) لأن صور الهاتف غالباً مائلة أو صغيرة.
"""
from __future__ import annotations

import asyncio

try:
    import numpy as np
except ImportError:
    np = None

from .preprocess import cv2, decode_image


def _pad(img: np.ndarray, frac: float = 0.35) -> np.ndarray:
    """هامش أبيض حول الصورة: الكاشف يحتاج مساحة هادئة حول الباركود، والصور المقصوصة بإحكام تفشل بدونها."""
    h, w = img.shape[:2]
    py, px = int(h * frac), int(w * frac)
    return cv2.copyMakeBorder(img, py, py, px, px, cv2.BORDER_CONSTANT, value=255 if img.ndim == 2 else (255, 255, 255))


def _variants(img: np.ndarray):
    yield img
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    yield gray
    yield _pad(gray)
    yield cv2.resize(_pad(gray), None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    # الكاشف حساس لعرض الشريط: الأشرطة العريضة جداً تفشل، فنجرّب عدة تصغيرات
    for f in (0.75, 0.5, 0.35, 0.25, 0.18, 0.12, 3.0, 4.0):
        yield cv2.resize(_pad(gray), None, fx=f, fy=f, interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC)
    yield cv2.createCLAHE(2.0, (8, 8)).apply(gray)
    h, w = gray.shape
    if max(h, w) < 1400:
        yield cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    if max(h, w) > 2000:
        s = 1600 / max(h, w)
        yield cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    yield cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]


def read_barcodes(data: bytes | np.ndarray) -> list[str]:
    """كل الباركودات المقروءة في الصورة (بدون تكرار). قائمة فارغة إن لم يُعثر على شيء."""
    if cv2 is None or not hasattr(cv2, "barcode"):
        raise RuntimeError("نسخة OpenCV لا تدعم الباركود — ثبّت opencv-python-headless>=4.8")
    img = decode_image(data) if isinstance(data, (bytes, bytearray)) else data
    det = cv2.barcode.BarcodeDetector()
    found: list[str] = []
    for v in _variants(img):
        try:
            ok, infos, _types, _pts = det.detectAndDecodeWithType(v)
        except cv2.error:
            continue
        if ok:
            for code in infos:
                if code and code not in found:
                    found.append(code)
        if found:
            break
    return found


async def read_barcodes_async(data: bytes) -> list[str]:
    return await asyncio.to_thread(read_barcodes, data)
