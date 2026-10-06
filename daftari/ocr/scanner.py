"""منسّق مسح الفواتير والباركود.

الافتراضي (وهو الوحيد على أندرويد/iOS): نموذج الذكاء الاصطناعي «مساعد شركة العمر» يقرأ الصورة مباشرة
(ocr/vision.py) — لا OpenCV ولا Tesseract ولا numpy.

المسار المحلي القديم (OpenCV + Tesseract، نسختان: رمادية وثنائية) ما زال موجوداً لكنه **لا يُستخدم إلا إذا مُرِّر
`engine` صراحةً** (اختبارات / ويندوز بحزم اختيارية). استيراده كسول فلا يكسر إقلاع الجوال.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from .parser import ScannedInvoice


@dataclass
class ScanResult:
    invoice: ScannedInvoice
    quality: Any
    processed_png: bytes   # للمعاينة في الواجهة ("هكذا رأى البرنامج فاتورتك")
    engine: str
    variant: str           # vision | gray | binary


class InvoiceScanner:
    def __init__(self, engine: Any | None = None, client_factory: Callable[[], Any] | None = None):
        self._engine = engine
        self._vision = None
        if engine is None:
            from .vision import VisionInvoiceScanner, default_client_factory
            self._vision = VisionInvoiceScanner(client_factory or default_client_factory())

    @property
    def uses_ai(self) -> bool:
        return self._vision is not None

    @property
    def engine(self) -> Any:
        if self._engine is None:  # إنشاء متأخر لمحرك OCR المحلي
            from .engine import TesseractEngine
            self._engine = TesseractEngine()
        return self._engine

    async def preview(self, image: bytes):
        """معاينة سريعة (بدون ذكاء/OCR): الصورة بعد الضغط + نصائح الجودة. تُعرض قبل المسح."""
        if self._vision is not None:
            return await self._vision.preview(image)
        from .preprocess import encode_png, preprocess
        pre = await asyncio.to_thread(preprocess, image)
        return await asyncio.to_thread(encode_png, pre.gray), pre.quality

    async def scan(self, image: bytes) -> ScanResult:
        if self._vision is not None:
            return await self._vision.scan(image)
        return await self._scan_local(image)

    async def read_barcodes(self, image: bytes) -> list[str]:
        if self._vision is not None:
            return await self._vision.read_barcodes(image)
        from .barcode import read_barcodes_async
        return await read_barcodes_async(image)

    # ---- المسار المحلي القديم (اختياري) ----------------------------------------------------------
    async def _scan_local(self, image: bytes) -> ScanResult:
        from .engine import read_async
        from .parser import parse_invoice
        from .preprocess import Preprocessed, encode_png, preprocess
        pre: Preprocessed = await asyncio.to_thread(preprocess, image)
        variants = {"gray": pre.gray, "binary": pre.binary}
        words = await asyncio.gather(*(read_async(self.engine, img) for img in variants.values()))

        width = pre.gray.shape[1]
        best_name, best_inv, best_score = "gray", ScannedInvoice(), -1.0
        for (name, _), w in zip(variants.items(), words):
            inv = parse_invoice(w, page_width=width)
            score = sum(r.confidence for r in inv.rows)
            if score > best_score:
                best_name, best_inv, best_score = name, inv, score

        for hint in pre.quality.hints:
            best_inv.warnings.append(hint)
        png = await asyncio.to_thread(encode_png, pre.gray)
        return ScanResult(best_inv, pre.quality, png, self.engine.name, best_name)
