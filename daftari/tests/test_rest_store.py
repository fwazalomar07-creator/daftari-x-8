"""RestStore (اتصال Supabase عبر PostgREST بدون مكتبة supabase) مقابل خادم محلي يقلّد سلوك PostgREST.
ملاحظة: هذا يختبر منطقنا (بناء الروابط/الترميز/الكتابة المشروطة)، لا خادم Supabase الحقيقي."""
import json, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from daftari.data.store import RestStore, StoreError, SupabaseStore

DB: dict[str, dict] = {}
GOOD_KEY = "good-key-" + "x" * 20


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _auth(self):
        if self.headers.get("apikey") != GOOD_KEY or self.headers.get("Authorization") != f"Bearer {GOOD_KEY}":
            self._send(401, {"message": "Invalid API key"})
            return False
        return True

    def _send(self, code, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def _q(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query, keep_blank_values=True).items()}

    def _match(self, q):
        rows = list(DB.values())
        if "key" in q:
            f = q["key"]
            if f.startswith("eq."):
                rows = [r for r in rows if r["key"] == f[3:]]
            elif f.startswith("in.(") and f.endswith(")"):
                wanted = json.loads("[" + f[4:-1] + "]")
                rows = [r for r in rows if r["key"] in wanted]
        if "updated_at" in q and q["updated_at"].startswith("eq."):
            rows = [r for r in rows if r["updated_at"] == q["updated_at"][3:]]
        return rows

    def do_GET(self):
        if not self._auth(): return
        if not urlparse(self.path).path.endswith("/rest/v1/app_data"):
            return self._send(404, {"message": "no table"})
        q = self._q(); rows = self._match(q)
        cols = q.get("select", "*").split(",")
        out = [{c: r[c] for c in cols if c in r} for r in rows][: int(q.get("limit", 1000))]
        self._send(200, out)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")

    def do_PATCH(self):
        if not self._auth(): return
        body = self._body(); rows = self._match(self._q())
        for r in rows: r.update(body)
        self._send(200, rows if "return=representation" in (self.headers.get("Prefer") or "") else None)

    def do_POST(self):
        if not self._auth(): return
        body = self._body()
        DB[body["key"]] = {**DB.get(body["key"], {}), **body}
        self._send(201)


class RestStoreTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/"        # الشرطة الأخيرة تُزال تلقائياً
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    async def asyncSetUp(self):
        DB.clear()
        self.s = await SupabaseStore.create(self.url, GOOD_KEY)

    async def test_roundtrip_conditional_write(self):
        self.assertEqual(await self.s.get("inventory"), (None, None))
        self.assertTrue(await self.s.put_if_unchanged("inventory", [{"id": "1", "name": "فلتر زيت"}], None))
        val, ts = await self.s.get("inventory")
        self.assertEqual(val, [{"id": "1", "name": "فلتر زيت"}])
        self.assertTrue(ts)
        # كتابة بطابع صحيح تنجح، وبطابع قديم تفشل (لا نكتب فوق عمل جهاز آخر)
        self.assertTrue(await self.s.put_if_unchanged("inventory", [], ts))
        self.assertFalse(await self.s.put_if_unchanged("inventory", [1], ts))

    async def test_timestamp_with_plus_offset_is_encoded(self):
        DB["x"] = {"key": "x", "value": {"a": 1}, "updated_at": "2026-10-03T04:19:29.340550+00:00"}
        self.assertTrue(await self.s.put_if_unchanged("x", {"a": 2}, "2026-10-03T04:19:29.340550+00:00"))
        self.assertEqual((await self.s.get("x"))[0], {"a": 2})

    async def test_get_many_and_keys_with_special_chars(self):
        for k in ("inventory", "img_a b", 'we"ird,key'):
            await self.s.put_if_unchanged(k, {"k": k}, None)
        got = await self.s.get_many(["inventory", "img_a b", 'we"ird,key', "missing"])
        self.assertEqual(set(got), {"inventory", "img_a b", 'we"ird,key'})
        self.assertEqual(await self.s.keys(), sorted(["inventory", "img_a b", 'we"ird,key']))
        self.assertEqual(await self.s.get_many([]), {})

    async def test_bad_key_gives_readable_error(self):
        bad = await SupabaseStore.create(self.url, "wrong-key-" + "y" * 20)
        with self.assertRaises(StoreError) as cm:
            await bad.keys()
        self.assertIn("401", str(cm.exception))

    async def test_unreachable_server_is_store_error(self):
        dead = await SupabaseStore.create("http://127.0.0.1:1", GOOD_KEY)
        with self.assertRaises(StoreError):
            await dead.get("x")

    async def test_create_validates_input(self):
        with self.assertRaises(StoreError):
            await SupabaseStore.create("abc.supabase.co", GOOD_KEY)
        with self.assertRaises(StoreError):
            await SupabaseStore.create("https://abc.supabase.co", "  ")


if __name__ == "__main__":
    unittest.main()
