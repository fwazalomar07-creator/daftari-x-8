"""صور الأصناف: ضغط تلقائي إلى ~10 كيلوبايت + تخزين محلي (RAM + قرص) + رفع/تنزيل من Supabase.

التصميم (لماذا هكذا؟):
- الصورة لا تُخزَّن داخل سجل الصنف نفسه. سجل `inventory` يُقرأ ويُكتب كاملاً في كل عملية، فلو وضعنا فيه
  ١٠ كيلوبايت × ١٠٠٠ صنف لصار ١٠ ميغابايت يُنزَّل في كل تحديث ويبطّئ البرنامج جداً.
- بدلاً من ذلك: الصنف يحمل فقط `img` (بصمة صغيرة)، والصورة تُحفظ في سجل مستقل بجدول app_data باسم `img_<id>`
  وتُحمَّل عند الحاجة فقط ثم تبقى في كاش محلي على القرص وفي الذاكرة، فلا تُنزَّل مرتين.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from pathlib import Path
from typing import Any

TARGET_BYTES = 10 * 1024          # الهدف: ١٠ كيلوبايت
MAX_SIDE_START = 320              # نبدأ بهذا الحجم ثم نصغّر حتى نصل للهدف
MIN_SIDE = 64
IMG_PREFIX = "img_"


class ImageError(Exception):
    pass


ORIGINAL_MAX_SIDE = 1600          # النسخة الأصلية المحلية (تبقى على الجهاز الذي رُفعت منه)


def _open_rgb(data: bytes):
    from PIL import Image, ImageOps
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
        return bg
    return im.convert("RGB")


APP_WHITE = (255, 255, 255)       # أبيض البرنامج (PANEL = #FFFFFF) — نفس خلفية البطاقات والفاتورة


def whiten_background(im, tolerance: int = 38):
    """يستبدل الخلفية الموحّدة اللون (جدار/طاولة/ورقة) بأبيض البرنامج الناصع.

    تعبئة فيضية من حواف الصورة بلون الخلفية المقدَّر من الزوايا، فلا تلمس ألوان المنتج. تنجح مع الخلفيات البسيطة؛
    والخلفيات المزدحمة تُترك كما هي (لا نخرّب الصورة).
    """
    from PIL import Image, ImageDraw, ImageFilter
    w, h = im.size
    small = im.copy()
    small.thumbnail((400, 400))
    sw, sh = small.size
    px = small.load()
    corners = [px[0, 0], px[sw - 1, 0], px[0, sh - 1], px[sw - 1, sh - 1]]

    def close(a, b):
        return max(abs(a[i] - b[i]) for i in range(3)) <= tolerance
    if sum(1 for c in corners if close(c, corners[0])) < 3:
        return im
    mark = (255, 0, 255)
    work = small.copy()
    for seed in ((0, 0), (sw - 1, 0), (0, sh - 1), (sw - 1, sh - 1)):
        if close(work.getpixel(seed), corners[0]):
            ImageDraw.floodfill(work, seed, mark, thresh=tolerance)
    mask = Image.new("L", (sw, sh), 0)
    mp, wp = mask.load(), work.load()
    bg = 0
    for y in range(sh):
        for x in range(sw):
            if wp[x, y] == mark:
                mp[x, y] = 255
                bg += 1
    frac = bg / float(sw * sh)
    if frac < 0.08 or frac > 0.95:
        return im
    mask = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(1.2)).resize((w, h), Image.LANCZOS)
    return Image.composite(Image.new("RGB", (w, h), APP_WHITE), im, mask)


def clean_product_photo(data: bytes, side: int = 900) -> bytes:
    """يُصلح صورة صنف: خلفية بيضاء ناصعة بلون البرنامج + الصنف كاملاً في وسط مربع (بلا قص). يرجع JPEG عالي الجودة."""
    from PIL import Image
    im = _open_rgb(data)
    try:
        im = whiten_background(im)
    except Exception as e:  # noqa: BLE001
        print("bg-remove:", e)
    inner = int(side * 0.88)
    im.thumbnail((inner, inner), Image.LANCZOS)
    canvas = Image.new("RGB", (side, side), APP_WHITE)
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


def prepare_product_image(data: bytes) -> tuple[bytes, bytes]:
    """صورة المنتج عند الرفع → (نسخة صغيرة ~10KB للسحابة، نسخة كبيرة بالجودة الجيدة للعرض على هذا الجهاز).

    لا تغيير في الخلفية ولا في الشكل: الصورة تبقى كما نسّقها المستخدم (تُصغَّر فقط).
    """
    from PIL import Image
    im = _open_rgb(data)
    big = im.copy()
    big.thumbnail((ORIGINAL_MAX_SIDE, ORIGINAL_MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    big.save(buf, "JPEG", quality=88, optimize=True)
    return compress_image(data), buf.getvalue()


def compress_image(data: bytes, target: int = TARGET_BYTES) -> bytes:
    """يحوّل أي صورة (PNG/JPG/WEBP…) إلى JPEG مربّع صغير لا يتجاوز `target` بايت.

    نصغّر الأبعاد أولاً ثم نخفض الجودة تدريجياً. الخلفية الشفافة تصير بيضاء.
    """
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

    side = MAX_SIDE_START
    best: bytes | None = None
    while side >= MIN_SIDE:
        work = im.copy()
        work.thumbnail((side, side), Image.LANCZOS)
        for q in (82, 72, 62, 52, 42, 34, 26):
            buf = io.BytesIO()
            work.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
            out = buf.getvalue()
            best = out if best is None or len(out) < len(best) else best
            if len(out) <= target:
                return out
        side = int(side * 0.82)
    if best is None:
        raise ImageError("تعذّر ضغط الصورة")
    return best  # أصغر ما أمكن حتى لو تجاوز الهدف قليلاً


def fingerprint(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()[:10]


class ImageStore:
    """كاش صور مكوَّن من: ذاكرة (dict) ← ملفات على القرص ← Supabase (عند الحاجة)."""

    def __init__(self, store: Any | None, cache_dir: Path):
        self.store = store
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._ram: dict[str, bytes] = {}
        self._doc_ram: dict[str, bytes] = {}
        self._fetching: set[str] = set()
        self._failed: set[str] = set()

    def _path(self, item_id: str) -> Path:
        return self.dir / f"{item_id}.jpg"

    def _orig_path(self, item_id: str) -> Path:
        return self.dir / f"{item_id}.full.jpg"

    def preload(self) -> int:
        """يحمّل كل صور الكاش المحلي (نحو 10KB لكل صنف) إلى الذاكرة بخيط خلفي: فتح المخزون/الفاتورة لا يقرأ القرص بعدها."""
        n = 0
        try:
            for p in self.dir.glob("*.jpg"):
                if p.name.endswith(".full.jpg"):
                    continue
                key = p.stem
                if key not in self._ram:
                    try:
                        self._ram[key] = p.read_bytes()
                        n += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return n

    def get_doc_image(self, item_id: str) -> bytes | None:
        """صورة للفواتير: الأفضل جودةً لكن بحد 500 بكسل ومخبّأة بالذاكرة، فلا نفكّ ملفاً كبيراً مع كل فاتورة."""
        hit = self._doc_ram.get(item_id)
        if hit is not None:
            return hit
        raw = self.get_best_local(item_id)
        if not raw:
            return None
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(raw))
            if max(im.size) > 500:
                im.draft("RGB", (1000, 1000))
                im = im.convert("RGB")
                im.thumbnail((500, 500), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=88)
                raw = buf.getvalue()
        except Exception:  # noqa: BLE001
            pass
        self._doc_ram[item_id] = raw
        return raw

    def get_best_local(self, item_id: str) -> bytes | None:
        """الأكبر جودة المتاح محلياً (الأصلية إن وُجدت على هذا الجهاز، وإلا نسخة الـ10KB)."""
        p = self._orig_path(item_id)
        if p.exists():
            try:
                return p.read_bytes()
            except OSError:
                pass
        return self.get_local(item_id)

    async def put_original(self, item_id: str, full_jpeg: bytes) -> None:
        await asyncio.to_thread(self._orig_path(item_id).write_bytes, full_jpeg)

    def get_local(self, item_id: str) -> bytes | None:
        """فوري بلا شبكة: من الذاكرة ثم القرص."""
        if item_id in self._ram:
            return self._ram[item_id]
        p = self._path(item_id)
        if p.exists():
            try:
                b = p.read_bytes()
            except OSError:
                return None
            self._ram[item_id] = b
            return b
        return None

    async def put(self, item_id: str, jpeg: bytes, full: bytes | None = None) -> None:
        """يحفظ محلياً فوراً ثم يرفع للسحابة (إن فشل الرفع تبقى الصورة محلية)."""
        self._ram[item_id] = jpeg
        self._doc_ram.pop(item_id, None)
        self._doc_ram.pop(item_id, None)
        self._failed.discard(item_id)
        await asyncio.to_thread(self._path(item_id).write_bytes, jpeg)
        if full:
            await self.put_original(item_id, full)     # الأصلية تبقى محلية فقط؛ السحابة تأخذ نسخة الـ10KB
        if self.store is not None:
            try:
                _, ts = await self.store.get(IMG_PREFIX + item_id)
                payload = {"d": base64.b64encode(jpeg).decode("ascii")}
                await self.store.put_if_unchanged(IMG_PREFIX + item_id, payload, ts)
            except Exception as e:  # noqa: BLE001
                print("image upload:", e)

    async def delete(self, item_id: str) -> None:
        self._ram.pop(item_id, None)
        self._doc_ram.pop(item_id, None)
        for p in (self._path(item_id), self._orig_path(item_id)):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    async def fetch_missing(self, item_ids: list[str], on_progress=None, batch: int = 20) -> int:
        """يجلب من السحابة الصور غير الموجودة محلياً — على دفعات صغيرة وفي الخلفية (لا يجمّد الواجهة)."""
        if self.store is None or not hasattr(self.store, "get_many"):
            return 0
        todo = [i for i in item_ids if i not in self._ram and not self._path(i).exists()
                and i not in self._fetching and i not in self._failed]
        got = 0
        for k in range(0, len(todo), batch):
            chunk = todo[k:k + batch]
            self._fetching.update(chunk)
            try:
                rows = await self.store.get_many([IMG_PREFIX + i for i in chunk])
            except Exception as e:  # noqa: BLE001
                print("image fetch:", e)
                self._failed.update(chunk)
                rows = {}
            finally:
                self._fetching.difference_update(chunk)
            for i in chunk:
                val = (rows.get(IMG_PREFIX + i) or (None, None))[0]
                b64 = val.get("d") if isinstance(val, dict) else None
                if not b64:
                    self._failed.add(i)  # لا توجد صورة لهذا الصنف في السحابة — لا نكرر السؤال
                    continue
                try:
                    jpeg = base64.b64decode(b64)
                except Exception:  # noqa: BLE001
                    self._failed.add(i)
                    continue
                self._ram[i] = jpeg
                self._doc_ram.pop(i, None)
                await asyncio.to_thread(self._path(i).write_bytes, jpeg)
                got += 1
            if on_progress and got:
                on_progress(got)
            await asyncio.sleep(0)
        return got
