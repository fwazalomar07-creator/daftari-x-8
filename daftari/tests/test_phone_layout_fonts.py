"""مشاكل ظهرت على الجهاز الحقيقي (أندرويد): 1) ترويسة المساعد العمودية تتحول لنص حرف-تحت-حرف وتدفع شريط الإرسال خارج الشاشة،
2) الفواتير/طباعة الذكاء تُظهر مربعات □ مكان الحروف اللاتينية و- و$ لأن خط أندرويد العربي لا يحويها."""
import shutil, tempfile, unittest
from pathlib import Path

from daftari.tests import fake_flet
fake_flet.install()

from daftari.printing import render as R


def walk(c, depth=0):
    """يمر على شجرة عناصر fake_flet (controls/content)."""
    if c is None or depth > 40:
        return
    yield c
    for ch in (getattr(c, "controls", None) or []):
        yield from walk(ch, depth + 1)
    inner = getattr(c, "content", None)
    if inner is not None and not isinstance(inner, str):
        yield from walk(inner, depth + 1)


class Fonts(unittest.TestCase):
    def test_default_fonts_cover_latin_arabic_punctuation(self):
        R._FONT_PATH_CACHE.clear()
        for bold in (False, True):
            path = R._font_path(bold)
            self.assertTrue(path and R._font_covers(path), (bold, path))

    def test_arabic_only_system_font_is_skipped(self):
        """يعيد مشكلة الجهاز: NotoNaskhArabic في /system/fonts يوجد قبل DejaVu المرفق ولا يحوي اللاتينية."""
        from fontTools import subset
        from fontTools.ttLib import TTFont
        src = Path(R.ASSETS) / "fonts" / "DejaVuSans.ttf"
        tmp = Path(tempfile.mkdtemp()) / "fonts"
        tmp.mkdir()
        f = TTFont(str(src))
        sub = subset.Subsetter(subset.Options())
        sub.populate(unicodes=list(range(0x0600, 0x0700)) + list(range(0xFB50, 0xFE00)) + list(range(0x30, 0x3A)) + [0x20])
        sub.subset(f)
        f.save(tmp / "NotoNaskhArabic-Regular.ttf")
        f.save(tmp / "NotoNaskhArabic-Bold.ttf")
        self.assertFalse(R._font_covers(str(tmp / "NotoNaskhArabic-Regular.ttf")))
        shutil.copy(src, tmp / "DejaVuSans.ttf")
        shutil.copy(Path(R.ASSETS) / "fonts" / "DejaVuSans-Bold.ttf", tmp / "DejaVuSans-Bold.ttf")
        old_assets = R.ASSETS
        try:
            R.ASSETS = tmp.parent
            R._FONT_PATH_CACHE.clear()
            self.assertEqual(Path(R._font_path(False)).name, "DejaVuSans.ttf")
            self.assertEqual(Path(R._font_path(True)).name, "DejaVuSans-Bold.ttf")
        finally:
            R.ASSETS = old_assets
            R._FONT_PATH_CACHE.clear()

    def test_codes_prices_and_numbers_draw_real_glyphs_not_tofu(self):
        from PIL import Image, ImageDraw
        R._FONT_PATH_CACHE.clear()
        f = R._font(40, False)

        def ink(ch):
            im = Image.new("L", (80, 80), 0)
            ImageDraw.Draw(im).text((10, 5), ch, font=f, fill=255)
            return im.tobytes()
        tofu = ink("\uFFFF")
        for ch in "26300-42040 $35 INV-1107 15W-40 ×":
            if ch != " ":
                self.assertNotEqual(ink(ch), tofu, ch)


class AssistantHeaderOnPhone(unittest.TestCase):
    def _build(self, width):
        import asyncio, tempfile
        from daftari.data.drafts import DraftStore
        from daftari.data.repository import Repository
        from daftari.data.store import MemoryStore
        from daftari.ledger import Ledger
        from daftari.ui.app import App
        home = Path(tempfile.mkdtemp())
        led = Ledger(Repository(MemoryStore(), cache_dir=home / "c"))
        asyncio.run(led.load())
        page = fake_flet.Page()
        page.width = width
        app = App(page, led, DraftStore(home / "d"), home / "x")
        app.unlocked = True
        from daftari.ui.tabs import assistant
        return assistant.build(app)

    def test_model_line_never_shares_a_row_with_the_toolbar(self):
        for width in (320, 390, 768, 1366):
            root = self._build(width)
            nodes = list(walk(root))
            texts = [n for n in nodes if isinstance(getattr(n, "args", None), tuple) and n.args
                     and isinstance(n.args[0], str) and n.args[0].startswith("النموذج:")]
            self.assertTrue(texts, f"سطر النموذج غير موجود @{width}")
            for row in nodes:
                kids = getattr(row, "controls", None) or []
                if texts[0] in kids:
                    # مسموح فقط داخل حاوية/عمود، وليس صفاً فيه أزرار (هذا ما عصره إلى عرض 10px)
                    self.assertFalse(any(getattr(k, "on_change", None) or getattr(k, "on_click", None) for k in kids), width)

    def test_toolbar_row_wraps_and_send_is_compact_on_phone(self):
        root = self._build(390)
        nodes = list(walk(root))
        switch_rows = [n for n in nodes if any(getattr(k, "label", None) == "تنفيذ مباشر بدون سؤال" for k in (getattr(n, "controls", None) or []))]
        self.assertTrue(switch_rows)
        self.assertTrue(all(getattr(r, "wrap", False) for r in switch_rows))      # يلتف بدل أن يضيق
        labels = [getattr(n, "args", ("",))[0] for n in nodes if isinstance(getattr(n, "args", None), tuple) and n.args]
        self.assertNotIn("إرسال", labels)                                         # على الهاتف: زر دائري بأيقونة فقط
        unknown = list(walk(self._build(None)))                                  # قبل أول قياس للصفحة: لا نقرر «هاتف» خطأً
        ulabels = [getattr(n, "args", ("",))[0] for n in unknown if isinstance(getattr(n, "args", None), tuple) and n.args]
        self.assertIn("إرسال", ulabels)
        wide = list(walk(self._build(1366)))
        wlabels = [getattr(n, "args", ("",))[0] for n in wide if isinstance(getattr(n, "args", None), tuple) and n.args]
        self.assertIn("إرسال", wlabels)                                           # على الشاشات العريضة يبقى النص


if __name__ == "__main__":
    unittest.main()
