"""يتأكد أن التطبيق بأكمله يُستورد ويُبنى بدون opencv / numpy / pytesseract / sounddevice (حال أندرويد)،
وأن الإقلاع من ملف main.py كسكربت مستقل يعمل، وأن فشل الاستيراد يظهر كشاشة تشخيص لا كخطأ أحمر."""
import subprocess, sys, textwrap, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PRELUDE = textwrap.dedent('''
    import sys, importlib.abc
    BLOCKED = {"cv2", "numpy", "pytesseract", "sounddevice", "easyocr", "playwright"}
    class Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError(f"{name} غير متوفرة على أندرويد (محاكاة)")
    sys.meta_path.insert(0, Blocker())
''')


def run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(code)], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)


class MobileSafe(unittest.TestCase):
    def test_whole_app_imports_without_native_libs(self):
        r = run('''
            from daftari.tests import fake_flet; fake_flet.install()
            import importlib, pkgutil, daftari
            n = 0
            for m in pkgutil.walk_packages(daftari.__path__, "daftari."):
                if ".tests" in m.name or m.name.endswith(".playwright"):
                    continue
                importlib.import_module(m.name); n += 1
            assert not (BLOCKED & set(k.split(".")[0] for k, v in sys.modules.items() if v is not None)), "مكتبة محظورة حُمّلت"
            print("imported", n)
        ''')
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])

    def test_scan_view_builds_without_native_libs(self):
        r = run('''
            from daftari.tests import fake_flet; fake_flet.install()
            from daftari.ocr.scanner import InvoiceScanner
            from daftari.ui.scan_view import ScanView
            from daftari.ledger import Ledger, PurchaseDraft
            from daftari.data.repository import Repository
            from daftari.data.store import MemoryStore
            import flet as ft, tempfile
            from pathlib import Path
            led = Ledger(Repository(MemoryStore(), cache_dir=Path(tempfile.mkdtemp())))
            sv = ScanView(ft.Page(), led, PurchaseDraft(), InvoiceScanner())
            sv.build(); print("ok")
        ''')
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])

    def test_import_failure_shows_diagnostic_screen(self):
        r = run('''
            from daftari.tests import fake_flet
            m = fake_flet.install(); seen = {}
            m.run = lambda fn, **kw: seen.setdefault("fn", fn)
            sys.modules["daftari.main"] = None          # يجعل «from daftari.main import run» يفشل
            import runpy; runpy.run_path("main.py", run_name="__main__")
            assert "fn" in seen, "لم تُعرض شاشة التشخيص"
            page = fake_flet.Page(); seen["fn"](page)
            assert page.added, "لا شيء أُضيف للصفحة"
            print("diag ok")
        ''')
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])


class LaunchLayouts(unittest.TestCase):
    def test_all_layouts_boot(self):
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "check_launch.py")], cwd=ROOT,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr[-1500:])

    def test_dependency_guard(self):
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "check_mobile_deps.py")], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
