"""يتأكد أن التطبيق يقلع بكل طرق التشغيل التي تستعملها أدوات البناء — بلا Flet الحقيقي.

يحاكي serious_python على أندرويد: ينسخ المشروع لمجلد مؤقت ويشغّل main.py بـ runpy.run_path (سكربت مستقل،
بلا حزمة أب) في أربعة تخطيطات، وفي كلٍّ يجب أن تُستدعى ft.run. فشل أي تخطيط = كان سيظهر «الخطأ الأحمر».
    python tools/check_launch.py
"""
import importlib.util
import os
import runpy
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IGNORE = shutil.ignore_patterns("tests", "__pycache__", ".git", ".github", "build", "tools")


def _fake_flet():
    spec = importlib.util.spec_from_file_location("fake_flet", ROOT / "daftari/tests/fake_flet.py")
    ff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ff)
    return ff.install()


def _clean_modules() -> None:
    for name in [n for n in sys.modules if n == "daftari" or n.startswith("daftari.")]:
        del sys.modules[name]


def run_layout(layout: str) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="serious_python_temp"))
    app = tmp / "app"
    if layout == "root":                       # main.py بجانب daftari/  (تخطيط المشروع الطبيعي)
        shutil.copytree(ROOT, app, ignore=IGNORE)
        entry = app / "main.py"
    elif layout == "root-inner":               # المدخل daftari/main.py داخل المشروع
        shutil.copytree(ROOT, app, ignore=IGNORE)
        entry = app / "daftari" / "main.py"
    else:                                      # flat / flat-no-init: محتويات daftari/ مسطّحة في جذر التطبيق
        shutil.copytree(ROOT / "daftari", app, ignore=IGNORE)
        if layout == "flat-no-init":
            (app / "__init__.py").unlink()
        entry = app / "main.py"
    _clean_modules()
    fake = _fake_flet()
    called: dict = {}
    fake.run = lambda fn, **kw: called.setdefault("main", fn)
    saved_path, saved_cwd = list(sys.path), os.getcwd()
    sys.path[:] = [p for p in sys.path if Path(p).resolve() != ROOT]
    sys.path.insert(0, str(app))
    os.chdir(app)
    os.environ["FLET_APP_STORAGE_DATA"] = str(tmp / "data")
    try:
        runpy.run_path(str(entry), run_name="__main__")
    finally:
        sys.path[:] = saved_path
        os.chdir(saved_cwd)
        shutil.rmtree(tmp, ignore_errors=True)
    assert "main" in called, f"[{layout}] ft.run لم تُستدعَ"
    print(f"OK  {layout}")


def main() -> int:
    failed = 0
    for layout in ("root", "root-inner", "flat", "flat-no-init"):
        try:
            run_layout(layout)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {layout}: {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
