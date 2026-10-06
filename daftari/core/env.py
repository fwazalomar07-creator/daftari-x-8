"""تحميل ملف .env بدون أي مكتبة خارجية.

يبحث بالترتيب في: مجلد التطبيق، المجلد الأب، المجلد الحالي، ثم DAFTARI_HOME.
متغيرات البيئة الموجودة فعلاً لها الأولوية (لا يكتب فوقها).
"""
from __future__ import annotations

import os
from pathlib import Path


def parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        if key:
            out[key] = val
    return out


def candidate_paths(extra: list[Path] | None = None) -> list[Path]:
    here = Path(__file__).resolve().parent.parent
    paths = [here / ".env", here.parent / ".env", Path.cwd() / ".env"]
    home = os.getenv("DAFTARI_HOME")
    paths.append((Path(home) if home else Path.home() / ".daftari") / ".env")
    paths += list(extra or [])
    seen, uniq = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p); uniq.append(p)
    return uniq


def load_env(extra: list[Path] | None = None) -> list[Path]:
    """يحمّل أول ملفات .env موجودة. يرجع الملفات التي قُرئت (للتشخيص)."""
    loaded: list[Path] = []
    for p in candidate_paths(extra):
        try:
            if p.is_file():
                for k, v in parse_env_text(p.read_text(encoding="utf-8")).items():
                    if v != "" and not os.environ.get(k):
                        os.environ[k] = v
                loaded.append(p)
        except OSError:
            continue
    return loaded
