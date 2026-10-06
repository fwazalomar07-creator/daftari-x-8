"""ينظّف المشروع قبل الرفع إلى GitHub: يحذف __pycache__ و*.pyc و*.pyo ومجلدات البناء القديمة (build/ dist/ .flet/ …).
    python tools/clean.py            # حذف فعلي
    python tools/clean.py --dry-run  # عرض ما سيُحذف فقط
لا يلمس: الكود، assets/، .env، .github/، ولا بيانات التطبيق."""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRS = {"__pycache__", "build", "dist", ".flet", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".gradle", "*.egg-info"}
FILES = ("*.pyc", "*.pyo", "*.log", ".DS_Store", "Thumbs.db")


def main(dry: bool) -> int:
    freed, count = 0, 0
    targets: list[Path] = []
    for p in ROOT.rglob("*"):
        if ".git" in p.parts or ".github" in p.parts or ".venv" in p.parts:
            continue
        if p.is_dir() and (p.name in DIRS or p.match("*.egg-info")):
            targets.append(p)
        elif p.is_file() and any(p.match(g) for g in FILES):
            targets.append(p)
    for t in sorted(set(targets), key=lambda x: len(x.parts)):
        if not t.exists():
            continue
        size = sum(f.stat().st_size for f in t.rglob("*") if f.is_file()) if t.is_dir() else t.stat().st_size
        print(("سيُحذف: " if dry else "حُذف: ") + str(t.relative_to(ROOT)))
        freed, count = freed + size, count + 1
        if not dry:
            shutil.rmtree(t, ignore_errors=True) if t.is_dir() else t.unlink(missing_ok=True)
    print(f"{'(تجربة) ' if dry else ''}{count} عنصر، {freed / 1_048_576:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main("--dry-run" in sys.argv))
