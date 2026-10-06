"""وسائط المساعد: تجهيز الصور للرؤية، وتغليف الصوت ليُرسل إلى Gemini للنسخ."""
from __future__ import annotations

import io
import struct

from ..data.images import ImageError

VISION_SIDE = 1280
VISION_QUALITY = 84


def mime_of(name: str, data: bytes = b"") -> str:
    ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    by_ext = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
        "gif": "image/gif", "bmp": "image/bmp", "wav": "audio/wav", "mp3": "audio/mpeg",
        "m4a": "audio/mp4", "ogg": "audio/ogg", "webm": "audio/webm", "aac": "audio/aac",
    }
    if ext in by_ext:
        return by_ext[ext]
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    return "application/octet-stream"


def sniff_audio_mime(data: bytes, fallback: str = "audio/wav") -> str:
    """نوع الصوت من محتوى الملف نفسه (وليس من امتداده) — مهم لأن مسجّلات المنصات قد تكتب AAC داخل ملف .wav."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"fLaC":
        return "audio/flac"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "audio/webm"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return "audio/aac"
    return fallback


def is_image_mime(mime: str) -> bool:
    return mime.startswith("image/")


def compress_for_vision(data: bytes, max_side: int = VISION_SIDE, quality: int = VISION_QUALITY) -> bytes:
    """JPEG واضح كفاية للفواتير وقطع الغيار (أكبر من ضغط بطاقة المخزون)."""
    try:
        from PIL import Image, ImageOps
    except ImportError as e:  # pragma: no cover
        raise ImageError("مكتبة Pillow غير مثبتة") from e
    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im)
        im.load()
    except Exception as e:  # noqa: BLE001
        raise ImageError("تعذّرت قراءة الصورة — اختر ملف صورة صالحاً (JPG/PNG/WEBP)") from e
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def inline_image_part(data: bytes, mime: str = "image/jpeg") -> dict:
    import base64
    return {"inlineData": {"mimeType": mime or "image/jpeg", "data": base64.b64encode(data).decode("ascii")}}


def inline_audio_part(data: bytes, mime: str = "audio/wav") -> dict:
    import base64
    return {"inlineData": {"mimeType": mime or "audio/wav", "data": base64.b64encode(data).decode("ascii")}}


def pcm16_to_wav(pcm: bytes, sample_rate: int = 44100, channels: int = 1) -> bytes:
    """يغلف عيّنات PCM16 ذات endian صغير في حاوية WAV."""
    n = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + n, b"WAVE", b"fmt ", 16,
        1, channels, sample_rate, sample_rate * channels * 2,
        channels * 2, 16, b"data", n,
    )
    return header + pcm
