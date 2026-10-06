"""محركات التعرف على النص. الافتراضي Tesseract (خفيف ويعمل دون إنترنت).

لدقة أفضل على العربية: ثبّت حزمة اللغة `ara` (tesseract-ocr-ara). إن لم تتوفر يعمل
المحرك بالإنجليزية فقط (الأرقام والأكواد تُقرأ جيداً، لكن أسماء الأصناف العربية لا).
EasyOCR (اختياري) أدق أحياناً على الخط العربي لكنه ثقيل (~مئات الميغابايت) ولا يناسب
الهاتف مباشرة — الأنسب تشغيله على خادم خاص بك واستدعاؤه عبر شبكة.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

try:
    import numpy as np
except ImportError:   # أندرويد: محرك OCR المحلي غير مستخدم
    np = None


@dataclass
class Word:
    text: str
    conf: float  # 0-100
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


class OcrEngine(Protocol):
    name: str

    def read(self, image: np.ndarray) -> list[Word]: ...


class OcrUnavailable(RuntimeError):
    pass


class TesseractEngine:
    def __init__(self, lang: str = "ara+eng", psm: int = 6):
        try:
            import pytesseract
        except ImportError as e:  # pragma: no cover
            raise OcrUnavailable("ثبّت pytesseract و Tesseract على الجهاز") from e
        self._pt = pytesseract
        try:
            available = set(pytesseract.get_languages(config=""))
        except Exception as e:  # noqa: BLE001
            raise OcrUnavailable("Tesseract غير مثبّت أو غير موجود في PATH") from e
        wanted = [l for l in lang.split("+") if l in available]
        if not wanted:
            raise OcrUnavailable(f"لا توجد لغات مطلوبة ({lang}). المتاح: {sorted(available)}")
        self.lang = "+".join(wanted)
        self.has_arabic = "ara" in wanted
        self.psm = psm
        self.name = f"tesseract[{self.lang}]"

    def read(self, image: np.ndarray) -> list[Word]:
        cfg = f"--oem 1 --psm {self.psm} -c preserve_interword_spaces=1"
        d = self._pt.image_to_data(image, lang=self.lang, config=cfg, output_type=self._pt.Output.DICT)
        words: list[Word] = []
        for i, txt in enumerate(d["text"]):
            txt = (txt or "").strip()
            try:
                conf = float(d["conf"][i])
            except (TypeError, ValueError):
                conf = -1
            if not txt or conf < 0:
                continue
            words.append(Word(txt, conf, int(d["left"][i]), int(d["top"][i]), int(d["width"][i]), int(d["height"][i])))
        return words


class EasyOcrEngine:
    """اختياري: pip install easyocr (يحمّل النماذج عند أول تشغيل)."""

    def __init__(self, langs: tuple[str, ...] = ("ar", "en"), gpu: bool = False):
        try:
            import easyocr
        except ImportError as e:
            raise OcrUnavailable("easyocr غير مثبّت") from e
        self._reader = easyocr.Reader(list(langs), gpu=gpu)
        self.name = f"easyocr[{'+'.join(langs)}]"

    def read(self, image: np.ndarray) -> list[Word]:
        out: list[Word] = []
        for box, text, conf in self._reader.readtext(image):
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            x, y = int(min(xs)), int(min(ys))
            out.append(Word(text.strip(), float(conf) * 100, x, y, int(max(xs) - x), int(max(ys) - y)))
        return [w for w in out if w.text]


async def read_async(engine: OcrEngine, image: np.ndarray) -> list[Word]:
    """OCR ثقيل على المعالج — نُخرجه من حلقة الأحداث حتى لا تتجمّد واجهة Flet."""
    return await asyncio.to_thread(engine.read, image)
