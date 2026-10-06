"""يفشل البناء مبكراً إن دخلت مكتبة لا تعمل على أندرويد إلى pyproject.toml أو استُوردت أعلى وحدة في التطبيق."""
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = {"opencv-python", "opencv-python-headless", "opencv-contrib-python", "pytesseract", "sounddevice",
             "easyocr", "numpy", "torch", "tensorflow", "scipy", "pandas", "playwright", "python-bidi-rs"}
bad = []
deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
for d in deps:
    name = re.split(r"[<>=!~\[; ]", d.strip(), 1)[0].lower().replace("_", "-")
    if name in FORBIDDEN:
        bad.append(f"pyproject.toml: «{d}» لا تعمل على أندرويد")
    if name == "python-bidi" and "==0.4" not in d.replace(" ", ""):
        bad.append("pyproject.toml: python-bidi يجب تثبيته ==0.4.2 (الإصدارات الأحدث Rust ولا تُبنى على أندرويد)")

# استيراد ثقيل على مستوى الوحدة (يكسر الإقلاع على الجوال). الاستيراد داخل الدوال/try مسموح.
TOP = re.compile(r"^(?:import|from)\s+(cv2|numpy|pytesseract|sounddevice|easyocr)\b", re.M)
for f in (ROOT / "daftari").rglob("*.py"):
    if "tests" in f.parts:
        continue
    for m in TOP.finditer(f.read_text(encoding="utf-8")):
        bad.append(f"{f.relative_to(ROOT)}: استيراد «{m.group(1)}» على مستوى الوحدة (يجب أن يكون داخل try/دالة)")
if bad:
    print("\n".join(bad))
    sys.exit(1)
print("OK: لا توجد مكتبات غير متوافقة مع أندرويد")
