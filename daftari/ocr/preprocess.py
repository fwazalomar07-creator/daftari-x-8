"""معالجة صورة الفاتورة بـ OpenCV قبل التعرف على النص.

الخطوات (كل خطوة تحسّن دقة OCR فعلياً):
  1. فك الترميز + تصغير/تكبير إلى دقة مناسبة (الصور الضخمة بطيئة، الصغيرة غير مقروءة).
  2. اكتشاف حدود الورقة وتصحيح المنظور (صورة مائلة من الهاتف -> صفحة مستقيمة).
  3. إزالة الظلال (قسمة الصورة على خلفيتها المقدَّرة) + CLAHE لتحسين التباين.
  4. تصحيح ميلان السطور (deskew).
  5. تقليل الضجيج ثم نسختان: رمادية محسّنة + ثنائية (adaptive threshold).
  6. تقرير جودة (تشويش/إضاءة/ميلان) يُعرض للمستخدم بنصائح بالعربية.
"""
from __future__ import annotations

from dataclasses import dataclass, field

try:
    import cv2
except ImportError:   # أندرويد/بيئة بلا OpenCV: يعمل التطبيق وتتعطل قراءة الصور فقط
    cv2 = None
try:
    import numpy as np
except ImportError:   # أندرويد: numpy غير مطلوبة (التحليل عبر Gemini)
    np = None

MAX_SIDE = 2400
MIN_SIDE = 1400


class ImageError(ValueError):
    pass


@dataclass
class Quality:
    blur_score: float = 0.0       # تباين لابلاس: أقل من ~80 = مهزوزة
    brightness: float = 0.0       # متوسط اللمعان 0-255
    contrast: float = 0.0         # الانحراف المعياري للرمادي
    skew_degrees: float = 0.0
    document_found: bool = False
    hints: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hints


@dataclass
class Preprocessed:
    color: np.ndarray
    gray: np.ndarray      # محسّنة (للـ OCR الأساسي)
    binary: np.ndarray    # ثنائية (للمقارنة/الاحتياط)
    quality: Quality


def decode_image(data: bytes) -> np.ndarray:
    if not data:
        raise ImageError("الصورة فارغة")
    if cv2 is None:
        raise ImageError("OpenCV غير مثبّت على هذا الجهاز — قراءة الفواتير بالصور تعمل على نسخة ويندوز")
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # يطبّق اتجاه EXIF تلقائياً
    if img is None:
        raise ImageError("تعذّر فتح الصورة — تأكد أنها JPG أو PNG")
    return img


def _resize(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    side = max(h, w)
    if side > MAX_SIDE:
        scale = MAX_SIDE / side
        return cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    if side < MIN_SIDE:
        scale = MIN_SIDE / side
        return cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return img


def _order_points(pts: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype="float32")
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    rect[0], rect[2] = pts[np.argmin(s)], pts[np.argmax(s)]
    rect[1], rect[3] = pts[np.argmin(d)], pts[np.argmax(d)]
    return rect


def find_document_quad(img: np.ndarray) -> np.ndarray | None:
    """أكبر شكل رباعي يغطي ≥ 25% من الصورة = الورقة. None إن لم نجد."""
    h, w = img.shape[:2]
    scale = 800 / max(h, w)
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)), iterations=2)
    cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    area_min = 0.25 * small.shape[0] * small.shape[1]
    for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:8]:
        if cv2.contourArea(c) < area_min:
            break
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return (approx.reshape(4, 2).astype("float32") / scale)
    return None


def four_point_warp(img: np.ndarray, quad: np.ndarray) -> np.ndarray:
    tl, tr, br, bl = _order_points(quad)
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if width < 200 or height < 200:
        return img
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype="float32")
    m = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], dtype="float32"), dst)
    return cv2.warpPerspective(img, m, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def remove_shadows(gray: np.ndarray) -> np.ndarray:
    k = max(15, (min(gray.shape) // 30) | 1)
    bg = cv2.medianBlur(cv2.dilate(gray, np.ones((7, 7), np.uint8)), k if k % 2 else k + 1)
    norm = cv2.divide(gray, bg, scale=255)
    return cv2.normalize(norm, None, 0, 255, cv2.NORM_MINMAX)


def estimate_skew(gray: np.ndarray) -> float:
    """زاوية ميلان السطور بالدرجات (موجبة = مائلة باتجاه عقارب الساعة). محصورة بين ±15°."""
    inv = cv2.bitwise_not(gray)
    _, bw = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3)))
    lines = cv2.HoughLinesP(bw, 1, np.pi / 360, threshold=200,
                            minLineLength=gray.shape[1] // 4, maxLineGap=20)
    if lines is None:
        return 0.0
    angles = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(a) <= 15:
            angles.append(a)
    return float(np.median(angles)) if len(angles) >= 3 else 0.0


def rotate(img: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.3:
        return img
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    border = 255 if img.ndim == 2 else (255, 255, 255)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=border)


def measure_quality(gray: np.ndarray, doc_found: bool, skew: float) -> Quality:
    q = Quality(
        blur_score=float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        brightness=float(gray.mean()),
        contrast=float(gray.std()),
        skew_degrees=skew,
        document_found=doc_found,
    )
    if q.blur_score < 60:
        q.hints.append("الصورة مهزوزة — ثبّت الهاتف وأعد التصوير")
    if q.brightness < 70:
        q.hints.append("الإضاءة ضعيفة — صوّر قرب نافذة أو شغّل الإضاءة")
    elif q.brightness > 232 and float((gray < 110).mean()) < 0.003:
        # الورقة البيضاء النظيفة لمعانها عالٍ لكن فيها حبر داكن؛ الانعكاس يمحو الحبر نفسه
        q.hints.append("الإضاءة قوية جداً (انعكاس) — غيّر زاوية التصوير")
    if q.contrast < 28:
        q.hints.append("التباين منخفض — الحبر باهت أو الورقة داكنة")
    if abs(skew) > 8:
        q.hints.append("الفاتورة مائلة كثيراً (تم تصحيحها، لكن الدقة قد تقل)")
    return q


def preprocess(data: bytes | np.ndarray) -> Preprocessed:
    img = decode_image(data) if isinstance(data, (bytes, bytearray)) else data
    img = _resize(img)

    quad = find_document_quad(img)
    warped = four_point_warp(img, quad) if quad is not None else img

    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    skew = estimate_skew(gray)
    warped, gray = rotate(warped, skew), rotate(gray, skew)

    quality = measure_quality(gray, quad is not None, skew)

    flat = remove_shadows(gray)
    flat = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(flat)
    flat = cv2.fastNlMeansDenoising(flat, None, h=8, templateWindowSize=7, searchWindowSize=21)

    block = max(25, (min(flat.shape) // 25) | 1)
    binary = cv2.adaptiveThreshold(flat, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,
                                   block if block % 2 else block + 1, 12)
    return Preprocessed(color=warped, gray=flat, binary=binary, quality=quality)


def encode_png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ImageError("تعذّر ترميز الصورة")
    return buf.tobytes()
