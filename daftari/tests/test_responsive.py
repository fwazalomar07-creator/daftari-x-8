"""التجاوب: جوال (عمودي/أفقي) · آيباد · لابتوب — كل شاشة تُبنى في كل مقاس، وتدوير الجهاز يعيد الرسم."""
import tempfile, unittest
from pathlib import Path

from daftari.tests import fake_flet
fake_flet.install()

from daftari.data.drafts import DraftStore
from daftari.data.repository import Repository
from daftari.data.store import MemoryStore
from daftari.ledger import Ledger
from daftari.ui.app import App, BOTTOM_TABS, MAIN_NAV, MORE_NAV

SIZES = {"phone-portrait": 390, "phone-landscape": 844, "ipad-portrait": 768, "ipad-landscape": 1180,
         "laptop": 1366, "desktop": 1920, "small-phone": 320}


class Responsive(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        led = Ledger(Repository(MemoryStore(), cache_dir=home / "c"))
        await led.load()
        await led.add_item("فلتر زيت تويوتا", "TY-100", 6, 10, 20, 9, 9.5)
        self.page = fake_flet.Page()
        self.app = App(self.page, led, DraftStore(home / "d"), home / "x")
        self.app.unlocked = True

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_every_screen_in_every_size(self):
        keys = [k for k, _, _ in BOTTOM_TABS] + [k for k, _, _ in MAIN_NAV + MORE_NAV if k != "scan"]
        for name, width in SIZES.items():
            self.page.width = width
            self.app._wide = self.app._is_wide()
            for key in keys:
                self.app.tab, self.app.sub = key, {}
                try:
                    await self.app.render()
                except Exception as e:  # noqa: BLE001
                    self.fail(f"{key} @ {name}({width}): {e!r}")

    async def test_layout_tiers(self):
        for width, wide, split in [(320, False, False), (390, False, False), (768, False, False),
                                   (820, True, False), (1180, True, True), (1920, True, True)]:
            self.page.width = width
            self.assertEqual((self.app._is_wide(), self.app.pos_split()), (wide, split), width)

    async def test_rotation_triggers_rerender_only_on_tier_change(self):
        self.page.width = 390
        self.app._layout = self.app._layout_key()
        self.page.tasks.clear()
        self.page.width = 400                      # نفس الفئة (جوال) → لا إعادة رسم
        self.app._on_resize(None)
        self.assertEqual(len(self.page.tasks), 0)
        self.page.width = 1180                     # آيباد أفقي → شريط جانبي + عمودان
        self.app._on_resize(None)
        self.assertEqual(len(self.page.tasks), 1)
        self.assertTrue(self.app._wide)
        self.page.width = 390                      # رجوع لوضع الجوال
        self.app._on_resize(None)
        self.assertEqual(len(self.page.tasks), 2)
        self.assertFalse(self.app._wide)


if __name__ == "__main__":
    unittest.main()
