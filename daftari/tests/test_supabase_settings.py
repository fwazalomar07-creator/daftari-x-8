"""بطاقة «اتصال Supabase» في الإعدادات: التحقق من المدخلات، حفظ .env، تبديل المتجر، الفصل."""
import os, tempfile, unittest
from pathlib import Path
from unittest import mock

from daftari.tests import fake_flet
fake_flet.install()

from daftari.data.drafts import DraftStore
from daftari.data.repository import Repository
from daftari.data.store import MemoryStore, SupabaseStore
from daftari.ledger import Ledger
from daftari.tests.test_ui_smoke import walk
from daftari.ui.app import App
from daftari.ui.tabs import settings


class SupabaseCard(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._env = {k: os.environ.pop(k, None) for k in ("SUPABASE_URL", "SUPABASE_KEY")}
        self.led = Ledger(Repository(None, cache_dir=self.home / "c"))
        await self.led.load(cloud=False)
        self.app = App(fake_flet.Page(), self.led, DraftStore(self.home / "d"), self.home / "x", home=self.home)
        self.app.unlocked = True

    async def asyncTearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def _controls(self):
        tree = settings.build(self.app)
        fields = {c.kw.get("label"): c for c in walk(tree) if c.kw.get("label")}
        btns = {getattr(c, "args", ("",))[0]: c for c in walk(tree) if c.kw.get("on_click") is not None and c.args}
        return fields, btns

    async def test_rejects_bad_url_and_short_key(self):
        fields, _ = self._controls()
        url = fields["رابط المشروع (Project URL)"]; key = fields["المفتاح (anon أو service_role)"]
        btn = [b for b in self._all_buttons() if b.args and b.args[0] == "حفظ واتصال"][0]
        url.value, key.value = "not-a-url", "x" * 40
        await btn.on_click(None)
        self.assertIsNone(self.app.store)
        self.assertFalse((self.home / ".env").exists())
        url.value, key.value = "https://abc.supabase.co", "short"
        await btn.on_click(None)
        self.assertIsNone(self.app.store)

    def _all_buttons(self):
        return [c for c in walk(settings.build(self.app)) if c.kw.get("on_click") is not None]

    async def test_connect_saves_env_and_swaps_store(self):
        tree = settings.build(self.app)
        nodes = list(walk(tree))
        url = next(c for c in nodes if c.kw.get("label") == "رابط المشروع (Project URL)")
        key = next(c for c in nodes if c.kw.get("label") == "المفتاح (anon أو service_role)")
        btn = next(c for c in nodes if c.args and c.args[0] == "حفظ واتصال")
        url.value, key.value = "https://abc.supabase.co/", "k" * 40

        async def fake_create(u, k):
            self.assertEqual(u, "https://abc.supabase.co")     # أُزيلت الشرطة الأخيرة
            return MemoryStore()
        with mock.patch.object(SupabaseStore, "create", staticmethod(fake_create)), \
             mock.patch.object(MemoryStore, "keys", create=True, new=mock.AsyncMock(return_value=[])), \
             mock.patch.object(App, "refresh_cloud", new=mock.AsyncMock()), \
             mock.patch.object(App, "render", new=mock.AsyncMock()):
            await btn.on_click(None)
        self.assertIsNotNone(self.app.store)
        self.assertIs(self.led.repo.store, self.app.store)
        self.assertIs(self.app.images.store, self.app.store)
        env = (self.home / ".env").read_text(encoding="utf-8")
        self.assertIn("SUPABASE_URL=https://abc.supabase.co", env)
        self.assertIn("SUPABASE_KEY=" + "k" * 40, env)

    async def test_failed_connect_saves_nothing(self):
        tree = settings.build(self.app)
        nodes = list(walk(tree))
        url = next(c for c in nodes if c.kw.get("label") == "رابط المشروع (Project URL)")
        key = next(c for c in nodes if c.kw.get("label") == "المفتاح (anon أو service_role)")
        btn = next(c for c in nodes if c.args and c.args[0] == "حفظ واتصال")
        url.value, key.value = "https://abc.supabase.co", "k" * 40

        async def boom(u, k):
            raise RuntimeError("Invalid API key")
        with mock.patch.object(SupabaseStore, "create", staticmethod(boom)):
            await btn.on_click(None)
        self.assertIsNone(self.app.store)
        self.assertFalse((self.home / ".env").exists())
        self.assertNotIn("SUPABASE_KEY", os.environ)


if __name__ == "__main__":
    unittest.main()
