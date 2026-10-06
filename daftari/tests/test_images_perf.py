"""اختبارات: ضغط الصور ~10KB، كاش الصور، التحميل بطلب واحد، فتح سريع من الكاش، العثور على خط عربي."""
import asyncio, io, os, shutil, tempfile, unittest
from pathlib import Path

from PIL import Image

from daftari.data.images import TARGET_BYTES, ImageError, ImageStore, compress_image, fingerprint
from daftari.data.repository import Repository
from daftari.data.store import MemoryStore
from daftari.ledger import Ledger


def _big_png() -> bytes:
    im = Image.effect_noise((1600, 1200), 70).convert("RGB")
    b = io.BytesIO(); im.save(b, "PNG")
    return b.getvalue()


class Compress(unittest.TestCase):
    def test_under_target(self):
        raw = _big_png()
        out = compress_image(raw)
        self.assertLessEqual(len(out), TARGET_BYTES)
        self.assertLess(len(out), len(raw) / 20)
        self.assertEqual(Image.open(io.BytesIO(out)).format, "JPEG")

    def test_transparent_png_ok(self):
        im = Image.new("RGBA", (500, 500), (255, 0, 0, 0)); b = io.BytesIO(); im.save(b, "PNG")
        self.assertLessEqual(len(compress_image(b.getvalue())), TARGET_BYTES)

    def test_bad_file(self):
        with self.assertRaises(ImageError):
            compress_image(b"not an image")


class Store(unittest.IsolatedAsyncioTestCase):
    async def test_image_roundtrip_through_cloud_and_local_cache(self):
        store = MemoryStore()
        d1, d2 = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        a, b = ImageStore(store, d1), ImageStore(store, d2)      # جهازان مختلفان
        jpeg = compress_image(_big_png())
        await a.put("item1", jpeg)
        self.assertEqual(a.get_local("item1"), jpeg)
        self.assertIsNone(b.get_local("item1"))
        self.assertEqual(await b.fetch_missing(["item1", "ghost"]), 1)
        self.assertEqual(b.get_local("item1"), jpeg)
        self.assertEqual(await b.fetch_missing(["item1", "ghost"]), 0)    # لا إعادة تنزيل ولا إعادة سؤال عن المفقود
        shutil.rmtree(d1); shutil.rmtree(d2)

    async def test_get_many_single_request_and_fast_cache_start(self):
        store = MemoryStore()
        home = Path(tempfile.mkdtemp())
        led = Ledger(Repository(store, cache_dir=home))
        await led.load()
        it = await led.add_item("زيت", "A1", 5, 8, 10, img=fingerprint(b"x"))
        self.assertEqual((await store.get_many(["inventory", "nope"]))["inventory"][0][0]["img"], fingerprint(b"x"))
        # فتح من الكاش المحلي فقط (بدون شبكة) يُظهر نفس الصنف
        store.fail = True
        led2 = Ledger(Repository(store, cache_dir=home))
        await led2.load(cloud=False)
        self.assertEqual([i.name for i in led2.inventory], ["زيت"])
        self.assertEqual(led2.inventory[0].img, fingerprint(b"x"))
        shutil.rmtree(home)


class Fonts(unittest.TestCase):
    def test_finds_windows_system_font(self):
        from daftari.printing import render
        src = next((p for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"] if Path(p).exists()), None)
        if not src:
            self.skipTest("no sample font")
        win = Path(tempfile.mkdtemp())
        (win / "Fonts").mkdir()
        shutil.copy(src, win / "Fonts" / "tahoma.ttf")
        old = os.environ.get("WINDIR"); os.environ["WINDIR"] = str(win)
        try:
            names = [n for n in render._REGULAR if n not in ("DejaVuSans.ttf",) and not n.startswith("Cairo")]
            self.assertTrue(render._find_font(names).endswith("tahoma.ttf"))
        finally:
            os.environ.pop("WINDIR") if old is None else os.environ.__setitem__("WINDIR", old)
            shutil.rmtree(win)


if __name__ == "__main__":
    unittest.main()


class ArabicShaping(unittest.TestCase):
    """المشكِّل المدمج: وصل الحروف + الاتجاه الصحيح، بدون أي مكتبة خارجية."""

    def test_forms_and_order(self):
        from daftari.printing.arabic import shape, visual
        iso = lambda c: chr(c)
        # «لا» تصير رمز لام-ألف واحداً، و«العمر» تتشكل: ا منفصلة، ل بداية، ع وسط، م وسط، ر نهاية
        self.assertEqual(shape("لا"), "\uFEFB")
        self.assertEqual(shape("العمر"), "\uFE8D\uFEDF\uFEEC"[:0] + "\uFE8D\uFEDF\uFECC\uFEE4\uFEAE")
        # الكلمات اللاتينية والأرقام تبقى بترتيبها، والعربية تُعكس للرسم من اليسار لليمين
        v = visual("زيت DONG 6 L")
        self.assertIn("DONG 6 L", v)
        self.assertTrue(v.startswith("DONG 6 L"))
        self.assertEqual(visual("خصم -5"), "-5" + " " + shape("خصم")[::-1])

    def test_render_without_raqm_has_no_missing_glyphs(self):
        from daftari.printing import render
        old = render.HAS_RAQM
        render.HAS_RAQM = False
        try:
            self.assertNotEqual(render._shape("فاتورة"), "فاتورة")
        finally:
            render.HAS_RAQM = old


class DollarBidi(unittest.TestCase):
    def test_dollar_stays_attached_to_number_in_arabic_text(self):
        from daftari.printing.arabic import visual
        v = visual("المجموع: $70")
        self.assertTrue(v.startswith("$70"))          # الرمز ملتصق بالرقم وعلى يسار الجملة العربية
        self.assertEqual(visual("خصم -$5").split(" ")[0], "-$5")


class ProductImagePrep(unittest.TestCase):
    def test_small_for_upload_big_for_display(self):
        from PIL import Image
        from daftari.data.images import prepare_product_image
        im = Image.new("RGB", (1200, 800), (200, 120, 40))
        b = io.BytesIO()
        im.save(b, "JPEG")
        small, full = prepare_product_image(b.getvalue())
        self.assertLessEqual(len(small), TARGET_BYTES)
        self.assertGreater(Image.open(io.BytesIO(full)).width, Image.open(io.BytesIO(small)).width)
