"""حارس دائم: ملفا الدخول لا يجوز أن يحويا استيراداً نسبياً أبداً (سبب «attempted relative import»)."""
import ast, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class EntryGuard(unittest.TestCase):
    def _relative_imports(self, path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return [(n.lineno, n.module) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level > 0]

    def test_root_main_has_no_relative_imports(self):
        self.assertEqual(self._relative_imports(ROOT / "main.py"), [])

    def test_inner_main_has_no_relative_imports(self):
        self.assertEqual(self._relative_imports(ROOT / "daftari" / "main.py"), [],
                         "استخدم from daftari.x import y في daftari/main.py")

    def test_pyproject_entry_is_root_main(self):
        import tomllib
        d = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(d["tool"]["flet"]["app"]["module"], "main")
        self.assertEqual(d["tool"]["flet"]["app"]["path"], ".")
        self.assertTrue((ROOT / "main.py").exists())


if __name__ == "__main__":
    unittest.main()
