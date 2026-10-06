"""ملفات البناء والأداء: requirements مطابق لـ pyproject، صلاحيات أندرويد، إعدادات ذاكرة CI، سكربت التنظيف، القوائم الكسولة."""
import re, subprocess, sys, tempfile, tomllib, unittest
from pathlib import Path

from daftari.tests import fake_flet
fake_flet.install()

ROOT = Path(__file__).resolve().parents[2]


def names(lines):
    out = set()
    for ln in lines:
        ln = ln.split("#")[0].strip()
        if ln:
            out.add(re.split(r"[<>=!~\[; ]", ln, 1)[0].lower().replace("_", "-"))
    return out


class BuildFiles(unittest.TestCase):
    def test_requirements_match_pyproject(self):
        deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
        for f in (ROOT / "requirements.txt", ROOT / "daftari" / "requirements.txt"):
            self.assertEqual(names(f.read_text(encoding="utf-8").splitlines()), names(deps), f.name)
        req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("python-bidi==0.4.2", req)
        self.assertNotRegex(req, r"(?im)^\s*(sounddevice|opencv|numpy|pytesseract)")

    def test_android_permissions_for_mic_and_storage(self):
        perms = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["flet"]["android"]["permission"]
        for p in ("RECORD_AUDIO", "INTERNET", "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE", "READ_MEDIA_IMAGES"):
            self.assertTrue(perms.get(f"android.permission.{p}"), p)

    def test_workflow_has_memory_guards(self):
        y = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        for needle in ("GRADLE_OPTS", "-Xmx3g", "fallocate -l 8G", "org.gradle.daemon=false", "org.gradle.workers.max=2", "continue-on-error: true"):
            self.assertIn(needle, y)
        self.assertIn("needs: [android, windows]", y)              # فشل المحاكي لا يمنع الإصدار

    def test_clean_script_removes_caches_only(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "tools").mkdir()
            (d / "tools" / "clean.py").write_text((ROOT / "tools" / "clean.py").read_text(encoding="utf-8"), encoding="utf-8")
            (d / "pkg" / "__pycache__").mkdir(parents=True)
            (d / "pkg" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"x")
            (d / "build" / "apk").mkdir(parents=True)
            (d / "build" / "apk" / "x.apk").write_bytes(b"x" * 10)
            (d / "pkg" / "keep.py").write_text("print(1)")
            (d / ".env").write_text("K=1")
            r = subprocess.run([sys.executable, str(d / "tools" / "clean.py")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((d / "build").exists() or (d / "pkg" / "__pycache__").exists())
            self.assertTrue((d / "pkg" / "keep.py").exists() and (d / ".env").exists())


class LazyLists(unittest.TestCase):
    def _app(self):
        import asyncio
        from daftari.data.drafts import DraftStore
        from daftari.data.repository import Repository
        from daftari.data.store import MemoryStore
        from daftari.ledger import Ledger
        from daftari.ui.app import App
        home = Path(tempfile.mkdtemp())
        led = Ledger(Repository(MemoryStore(), cache_dir=home / "c"))
        asyncio.run(led.load())
        for i in range(150):
            asyncio.run(led.add_item(f"صنف {i}", f"C{i}", 1, 2, 5))
        app = App(fake_flet.Page(), led, DraftStore(home / "d"), home / "x")
        app.unlocked = True
        return app, led

    def test_inventory_uses_listview_with_cards_as_direct_children(self):
        from daftari.ui.tabs import inventory
        app, led = self._app()
        root = inventory.build(app)                                 # Stack([ListView, أزرار التنقل])
        lv = root.controls[0]
        self.assertGreater(len(lv.controls), inventory.PAGE_SIZE)   # الترويسة + 60 بطاقة + زر «عرض الكل» كلها أبناء مباشرون لـ ListView
        self.assertTrue(lv.kw.get("expand"))

    def test_lazylist_sync(self):
        from daftari.ui import widgets as w
        lz = w.LazyList(fixed=["h1", "h2"])
        lz.controls = ["a", "b"]
        lz.sync()
        self.assertEqual(lz.view.controls, ["h1", "h2", "a", "b"])


if __name__ == "__main__":
    unittest.main()
