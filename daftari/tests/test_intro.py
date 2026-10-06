import asyncio, os, unittest
from daftari.tests import fake_flet
fake_flet.install()
from daftari.ui import intro as I


class P(fake_flet.Page):
    def __init__(self, width=390):
        super().__init__()
        self.overlay, self.width, self.updates = [], width, 0

    def update(self):
        self.updates += 1


class IntroTests(unittest.IsolatedAsyncioTestCase):
    async def test_show_then_hide_removes_overlay(self):
        I.MIN_SHOW_SECONDS = 0.05
        page = P()
        it = I.Intro(page)
        await it.show()
        self.assertEqual(len(page.overlay), 1)
        self.assertEqual((it._logo.opacity, it._logo.scale), (1, 1))      # بدأت حركة الدخول
        await it.hide()
        self.assertEqual(page.overlay, [])

    async def test_broken_page_never_raises(self):
        page = fake_flet.Page()          # بلا overlay حقيقي (None)
        it = I.Intro(page)
        await it.show()                  # يبتلع الخطأ
        await it.hide()                  # لا شيء ليخفيه
        self.assertFalse(it._shown)

    async def test_hide_waits_for_minimum_time(self):
        I.MIN_SHOW_SECONDS = 0.3
        page = P(); it = I.Intro(page); await it.show()
        t = asyncio.get_event_loop().time(); await it.hide()
        self.assertGreaterEqual(asyncio.get_event_loop().time() - t, 0.2)

    def test_logo_width_responsive(self):
        self.assertEqual(I.Intro(P(320))._logo_width(), 230)
        self.assertEqual(I.Intro(P(1920))._logo_width(), 380)

    def test_can_disable(self):
        self.assertTrue(I.enabled({}))
        self.assertFalse(I.enabled({"introScreen": False}))
        os.environ["DAFTARI_INTRO"] = "0"
        try:
            self.assertFalse(I.enabled({}))
        finally:
            del os.environ["DAFTARI_INTRO"]

    def test_assets_exist_for_build_and_runtime(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        for rel in ("assets/icon.png", "assets/splash.png", "assets/intro_logo.png", "daftari/assets/intro_logo.png"):
            self.assertTrue((root / rel).exists(), rel)


if __name__ == "__main__":
    unittest.main()
