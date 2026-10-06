"""قراءة كتالوج FMI (FileMaker WebDirect) عبر Data API + دقة البحث + أداة فتح الموقع — بخادم FileMaker وهمي محلي."""
import json, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from daftari.tests import fake_flet
fake_flet.install()

from daftari.ai import catalogs as cat
from daftari.ai import fmi
from daftari.ai.tools import ToolBox
from daftari.data.repository import Repository
from daftari.data.store import MemoryStore
from daftari.ledger import Ledger

def _jpeg() -> bytes:
    import io
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (1400, 900), (30, 90, 160)).save(b, "JPEG"); return b.getvalue()


JPEG = _jpeg()
RECORDS = [
    {"Code": "HU 7008 z", "Name": "فلتر زيت MANN", "Cars": "BMW 320 / VW Golf", "Notes": "",
     "Photo": "http://HOST/Streaming_SSL/MainDB/abc.jpg?RCType=EmbeddedRCFileProcessor"},
    {"Code": "OC 1234", "Name": "Oil filter Mahle", "Cars": "Toyota Corolla", "Photo": "", "Notes": "x"},
]
PORTALS = {"HU 7008 z": {"CrossRef": [{"CrossRef::Brand": "Bosch", "CrossRef::Number": "0 986 AF1", "recordId": "5", "modId": "1"},
                                      {"CrossRef::Brand": "Mahle", "CrossRef::Number": "OC 999", "recordId": "6"}]}}
FIELDS = [{"name": "Code", "result": "text"}, {"name": "Name", "result": "text"}, {"name": "Cars", "result": "text"},
          {"name": "Photo", "result": "container"}, {"name": "Notes", "result": "text"}, {"name": "Qty", "result": "number"}]


class FakeFM(BaseHTTPRequestHandler):
    log: list = []
    enabled = True
    layouts_ = [{"name": "Reports"}, {"name": "_hidden"}, {"name": "Folder", "isFolder": True,
                                                            "folderLayoutNames": [{"name": "FilterCatalog"}]}]

    def log_message(self, *a):  # صمت
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def _msg(self, code="0", text="OK", **resp):
        return {"response": resp, "messages": [{"code": code, "message": text}]}

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        FakeFM.log.append(("GET", self.path, self.headers.get("Cookie", "")))
        if self.path.startswith("/Streaming_SSL/"):
            if self.headers.get("Cookie", "") != "X-FMS-Session-Key=TOKEN1":
                return self._send(401, {})
            self.send_response(200); self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(JPEG))); self.end_headers(); self.wfile.write(JPEG)
        elif self.path == "/fmi/webd/data":
            raw = b"<html><body>Enable JavaScript in your browser to use this application.</body></html>"
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(raw)
        elif not FakeFM.enabled:
            self._send(404, {})
        elif self.path == "/fmi/data/version":
            self._send(200, self._msg(productInfo={"name": "FileMaker Data API Engine", "version": "20.3.1"}))
        elif not self.headers.get("Authorization", "").startswith("Bearer "):
            self._send(401, self._msg("952", "Invalid FileMaker Data API token"))
        elif self.path.endswith("/layouts"):
            self._send(200, self._msg(layouts=FakeFM.layouts_))
        elif "/layouts/" in self.path:
            self._send(200, self._msg(fieldMetaData=FIELDS))
        else:
            self._send(404, {})

    def do_POST(self):
        body = self._body()
        FakeFM.log.append(("POST", self.path, body))
        if not FakeFM.enabled:
            return self._send(404, {})
        if self.path.endswith("/sessions"):
            import base64
            ok = self.headers.get("Authorization") == "Basic " + base64.b64encode("reader:pw123".encode()).decode()
            return self._send(200, self._msg(token="TOKEN1")) if ok else self._send(401, self._msg("212", "Invalid user account"))
        if self.path.endswith("/_find"):
            if "FilterCatalog" not in self.path:             # الشاشات الأخرى فارغة: يجب أن نجد الصحيحة
                return self._send(404, self._msg("401", "No records match the request"))
            hits = []
            for rec in RECORDS:
                for crit in body["query"]:
                    (field, pat), = crit.items()
                    if pat.strip("*").lower() in str(rec.get(field, "")).lower():
                        fd = {k: (v.replace("HOST", self.headers["Host"]) if isinstance(v, str) else v) for k, v in rec.items()}
                        hits.append({"fieldData": fd, "portalData": PORTALS.get(rec["Code"], {})}); break
            if not hits:
                return self._send(404, self._msg("401", "No records match the request"))
            return self._send(200, self._msg(data=hits[: int(body.get("limit", 10))]))
        self._send(404, {})

    def do_DELETE(self):
        FakeFM.log.append(("DELETE", self.path)); self._send(200, self._msg())


class Server:
    def __enter__(self):
        FakeFM.log, FakeFM.enabled = [], True
        self.srv = HTTPServer(("127.0.0.1", 0), FakeFM)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        return self

    def __exit__(self, *a):
        self.srv.shutdown(); self.srv.server_close()


class FmiClientTests(unittest.TestCase):
    def test_parse_url(self):
        self.assertEqual(fmi.parse_webd_url("http://188.59.6.153/fmi/webd/data"), ("http://188.59.6.153", "data"))
        self.assertEqual(fmi.parse_webd_url("https://h:8443/fmi/webd/My%20DB?x=1"), ("https://h:8443", "My DB"))
        with self.assertRaises(fmi.FmiError):
            fmi.parse_webd_url("not a url")

    def test_sanitize(self):
        self.assertEqual(fmi.sanitize_criteria('=<HU 7008*"'), "HU 7008")

    def test_probe(self):
        with Server() as s:
            self.assertIn("productInfo", fmi.probe(s.base))
            FakeFM.enabled = False
            with self.assertRaises(fmi.FmiError):
                fmi.probe(s.base)

    def test_search_finds_record_via_spaced_variant_and_logs_out(self):
        with Server() as s:
            res = fmi.search(s.base + "/fmi/webd/data", "reader", "pw123", cat.query_variants("HU7008z"))
            self.assertEqual((res.layout, res.count), ("FilterCatalog", 1))
            self.assertIn("Code: HU 7008 z", res.text)
            self.assertIn("فلتر زيت MANN", res.text)
            self.assertNotIn("Photo", res.text)                       # حقول container تُهمل
            self.assertNotIn("http://", res.text)                     # ولا روابط الصور
            self.assertIn("[CrossRef]", res.text)                     # الجداول المرتبطة
            self.assertIn("Brand: Bosch, Number: 0 986 AF1", res.text)
            self.assertNotIn("recordId", res.text)
            self.assertEqual(len(res.images), 1)                      # صورة الفلتر نُزّلت بكوكي الجلسة
            self.assertEqual((res.images[0]["label"], res.images[0]["data"][:3]), ("HU 7008 z", b"\xff\xd8\xff"))
            from PIL import Image; import io
            self.assertLessEqual(max(Image.open(io.BytesIO(res.images[0]["data"])).size), 1000)   # مصغّرة
            self.assertTrue(any(x[0] == "GET" and x[1].startswith("/Streaming_SSL/") and "TOKEN1" in x[2] for x in FakeFM.log))
            self.assertNotIn("Notes", res.text)                       # الفارغة تُهمل
            self.assertTrue(any(x[0] == "DELETE" for x in FakeFM.log))  # تسجيل الخروج دائماً
            find = next(x for x in FakeFM.log if x[0] == "POST" and x[1].endswith("/_find"))
            fields = {k for q in find[2]["query"] for k in q}
            self.assertNotIn("Qty", fields)                           # الحقول الرقمية لا تُبحث نصياً

    def test_layouts_folder_flattened_and_ranked(self):
        with Server() as s:
            with fmi.FmiClient(s.base, "data", "reader", "pw123") as c:
                names = c.layouts()
            self.assertEqual(names, ["Reports", "_hidden", "FilterCatalog"])
            self.assertEqual(fmi.pick_layouts(names)[0], "FilterCatalog")
            self.assertEqual(fmi.pick_layouts(names, "Reports"), ["Reports"])

    def test_no_match_returns_empty(self):
        with Server() as s:
            res = fmi.search(s.base + "/fmi/webd/data", "reader", "pw123", ["ZZZ999"])
            self.assertEqual((res.text, res.count), ("", 0))

    def test_errors_are_explained(self):
        with Server() as s:
            with self.assertRaises(fmi.FmiError) as cm:
                fmi.search(s.base + "/fmi/webd/data", "reader", "WRONG", ["x"])
            self.assertIn("كلمة المرور", str(cm.exception))
            with self.assertRaises(fmi.FmiError) as cm:
                fmi.search(s.base + "/fmi/webd/data", "", "", ["x"])
            self.assertIn("اسم مستخدم", str(cm.exception))
            FakeFM.enabled = False
            with self.assertRaises(fmi.FmiError) as cm:
                fmi.search(s.base + "/fmi/webd/data", "reader", "pw123", ["x"])
            self.assertIn("Data API", str(cm.exception))
        with self.assertRaises(fmi.FmiError) as cm:      # سيرفر غير موجود
            fmi.search("http://127.0.0.1:9/fmi/webd/data", "u", "p", ["x"])
        self.assertIn("تعذّر الاتصال", str(cm.exception))


class CatalogFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_fmi_source_ok_with_account(self):
        with Server() as s:
            src = cat.CatalogSource("fmi", "FMI", s.base + "/fmi/webd/data", "filemaker", user="reader", password="pw123")
            r = await cat.search_one(src, "hu-7008 Z")
            self.assertEqual(r["status"], "ok", r)
            self.assertIn("HU 7008 z", r["text"])
            self.assertEqual(r["open_url"], src.url)

    async def test_fmi_source_without_account_explains_and_offers_open(self):
        with Server() as s:
            src = cat.CatalogSource("fmi", "FMI", s.base + "/fmi/webd/data", "filemaker")
            with mock.patch.object(cat, "_has_playwright", lambda: False), \
                 mock.patch.object(cat, "site_search", lambda *a, **k: (_ for _ in ()).throw(cat.CatalogError("x"))):
                r = await cat.search_one(src, "HU 7008 z")
            self.assertEqual(r["status"], "error")
            self.assertIn("WebDirect", r["error"])
            self.assertIn("فتح الموقع", r["error"])
            self.assertEqual(r["open_url"], src.url)

    async def test_wrong_password_reported_not_hidden(self):
        with Server() as s:
            src = cat.CatalogSource("fmi", "FMI", s.base + "/fmi/webd/data", "filemaker", user="reader", password="bad")
            with mock.patch.object(cat, "_has_playwright", lambda: False), \
                 mock.patch.object(cat, "site_search", lambda *a, **k: ""):
                r = await cat.search_one(src, "HU 7008 z")
            self.assertEqual(r["status"], "error")
            self.assertIn("كلمة المرور", r["error"])

    async def test_form_mode_retries_with_alternate_variant(self):
        src = cat.CatalogSource("f", "F", "http://x.example/", "form")
        seen = []

        def fake_form(s, q):
            seen.append(q)
            return ("<table><tr><td>HU 7008 z</td><td>Oil filter MANN for BMW 320 and many more cars</td></tr></table>"
                    if q.lower() == "hu 7008 z" else "<p>no result</p>")
        with mock.patch.object(cat, "form_search", fake_form):
            r = await cat.search_one(src, "HU7008z")
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(seen, ["HU7008z", "HU 7008 z"])          # الصيغة الأولى فشلت فجُرّبت الثانية
        self.assertEqual(r["matched_as"], "HU 7008 z")

    def test_variants_and_focus(self):
        self.assertEqual(cat.query_variants("٧٠٠٨"), ["7008"])
        self.assertEqual(cat.query_variants("HU-7008 z")[1], "HU7008z")
        txt = cat.focus_lines("menu\nabout\nMANN | HU 7008 z | oil", cat.query_variants("HU7008z"))
        self.assertTrue(txt.startswith("MANN | HU 7008 z"))


class ImagesInChat(unittest.IsolatedAsyncioTestCase):
    async def test_toolbox_hides_bytes_from_model_and_keeps_pictures_for_ui(self):
        with Server() as s, tempfile.TemporaryDirectory() as d:
            led = Ledger(Repository(MemoryStore(), cache_dir=Path(d) / "c"))
            await led.load()
            led.settings["catalogs"] = [{"id": "fmi", "name": "FMI", "url": s.base + "/fmi/webd/data", "mode": "filemaker",
                                         "user": "reader", "password": "pw123"}]
            tb = ToolBox(led, None, Path(d), None, lambda: True)
            out = await tb.execute("search_catalogs", {"query": "HU 7008 z", "sources": "fmi"})
            self.assertTrue(out["ok"], out)
            json.dumps(out)                                           # لا bytes في ما يُرسل للنموذج
            src = out["result"]["sources"][0]
            self.assertEqual(src["status"], "ok")
            self.assertIn("images_note", src)
            self.assertEqual(len(tb.catalog_pictures), 1)
            self.assertEqual(tb.catalog_pictures[0][0]["data"][:3], b"\xff\xd8\xff")

    async def test_attach_catalog_image_tool_and_ui_helper(self):
        from daftari.data.images import ImageStore
        with tempfile.TemporaryDirectory() as d:
            led = Ledger(Repository(MemoryStore(), cache_dir=Path(d) / "c"))
            await led.load()
            a = await led.add_item("فلتر زيت MANN", "HU7008Z", 5, 9, 3)
            await led.add_item("فلتر زيت ثاني", "OC1", 5, 9, 3)
            images = ImageStore(None, Path(d) / "img")
            tb = ToolBox(led, None, Path(d), None, lambda: True, images=images)
            no = await tb.execute("attach_catalog_image", {"item": "HU7008Z"})        # لا صور بعد
            self.assertFalse(no["ok"])
            tb.catalog_pictures.append([{"source": "FMI", "label": "HU 7008 z", "data": JPEG}])
            amb = await tb.execute("attach_catalog_image", {"item": "فلتر زيت"})        # أكثر من صنف: لا يخمّن
            self.assertFalse(amb["ok"]); self.assertIn("أكثر من صنف", amb["error"])
            bad = await tb.execute("attach_catalog_image", {"item": "HU7008Z", "picture_no": 5})
            self.assertFalse(bad["ok"])
            ok = await tb.execute("attach_catalog_image", {"item": "HU7008Z"})
            self.assertTrue(ok["ok"], ok)
            self.assertEqual(ok["result"]["id"], a.id)
            self.assertEqual(images.get_local(a.id)[:3], b"\xff\xd8\xff")
            self.assertTrue(led._item(a.id).img)                                      # بصمة الصورة حُدّثت

            # نفس المسار الذي يستعمله زر «إضافتها لصنف» في الواجهة
            from daftari.tests import fake_flet
            from daftari.ui import widgets as w
            b = next(i for i in led.inventory if i.code == "OC1")

            class App:
                page = fake_flet.Page()
                ledger = led
                images_ = images
            App.images = images

            async def after_change():
                pass
            App.after_change = staticmethod(after_change)
            self.assertTrue(await w.attach_image_to_item(App(), b, JPEG))
            self.assertIsNotNone(images.get_local(b.id))
            self.assertFalse(await w.attach_image_to_item(App(), b, b"not an image"))

    def test_image_buttons_build_in_card(self):
        from daftari.tests import fake_flet
        from daftari.ui.tabs import assistant as A

        class App:
            page = fake_flet.Page()
        row = A._image_actions(App(), {"label": "HU 7008 z", "data": JPEG}, compact=True)
        full = A._image_actions(App(), {"label": "HU 7008 z", "data": JPEG})
        self.assertTrue(row is not None and full is not None)
        self.assertEqual(A._safe_filename('HU 7008/z: "x"'), "HU-7008-z-x.jpg")
        self.assertEqual(A._safe_filename(""), "catalog-image.jpg")

    def test_collect_up_to_100_images_and_deadline(self):
        class C:
            n = 0
            def download(self, url):
                C.n += 1; return JPEG
        recs = [{"fieldData": {"Code": f"C{i}", "Photo": f"http://h/Streaming_SSL/{i}"}} for i in range(130)]
        flds = [{"name": "Photo", "result": "container"}]
        self.assertEqual(len(fmi.collect_images(C(), recs, flds)), 100)        # لا سقف 4: حتى 100
        self.assertEqual(len(fmi.collect_images(C(), recs[:7], flds)), 7)
        self.assertEqual(len(fmi.collect_images(C(), recs, flds, limit=3)), 3)
        old = fmi.IMAGES_DEADLINE
        fmi.IMAGES_DEADLINE = -1                                               # مهلة منتهية: لا تعلّق، تعرض ما وصل
        try:
            self.assertEqual(fmi.collect_images(C(), recs, flds), [])
        finally:
            fmi.IMAGES_DEADLINE = old
        self.assertEqual((fmi.MAX_IMAGES, fmi.MAX_RECORDS), (100, 100))

    def test_bad_container_is_skipped(self):
        class C:
            def download(self, url): return b"<html>not an image</html>"
        recs = [{"fieldData": {"Code": "X", "Photo": "http://h/Streaming_SSL/a"}}]
        self.assertEqual(fmi.collect_images(C(), recs, [{"name": "Photo", "result": "container"}]), [])

    def test_chat_export_handles_catalog_pictures(self):
        from daftari.ai import chat_export as ce
        msgs = [("user", "فلتر HU 7008 z"), ("assistant", "هذه صورته"),
                ("catalog_pictures", [{"source": "FMI", "label": "HU 7008 z", "data": JPEG}]),
                ("link", {"name": "FMI", "url": "http://x/fmi/webd/data"})]
        entries = ce.build_entries(msgs, [""] * 4)
        self.assertEqual(len(entries[2].images), 1)
        self.assertIn("FMI", entries[3].text)


class AutoAttach(unittest.IsolatedAsyncioTestCase):
    async def _env(self, s, d):
        from daftari.data.images import ImageStore
        led = Ledger(Repository(MemoryStore(), cache_dir=Path(d) / "c"))
        await led.load()
        led.settings["catalogs"] = [{"id": "fmi", "name": "FMI", "url": s.base + "/fmi/webd/data", "mode": "filemaker",
                                     "user": "reader", "password": "pw123"}]
        images = ImageStore(None, Path(d) / "img")
        return led, images, ToolBox(led, None, Path(d), None, lambda: True, images=images)

    async def test_search_adds_image_automatically_once_and_never_overwrites(self):
        with Server() as s, tempfile.TemporaryDirectory() as d:
            led, images, tb = await self._env(s, d)
            it = await led.add_item("فلتر MANN", "HU7008Z", 5, 9, 3)
            other = await led.add_item("فلتر آخر", "OC 1234", 5, 9, 3)
            out = await tb.execute("search_catalogs", {"query": "HU 7008 z", "sources": "fmi"})
            res = out["result"]
            self.assertEqual([x["code"] for x in res["auto_attached"]], ["HU7008Z"])
            self.assertIn("تلقائياً", res["auto_attach_note"])
            self.assertEqual(images.get_local(it.id)[:3], b"\xff\xd8\xff")       # أُضيفت بلا أي ضغط زر
            self.assertTrue(led._item(it.id).img)
            self.assertIsNone(images.get_local(other.id))                        # غير المطابق لم يُمسّ
            self.assertTrue(any("HU7008Z" in n for n in tb.notices))
            json.dumps(res)
            before = images.get_local(it.id)
            out2 = await tb.execute("search_catalogs", {"query": "HU 7008 z", "sources": "fmi"})
            self.assertNotIn("auto_attached", out2["result"])                    # لا تكرار
            self.assertIn("auto_attach_skipped", out2["result"])                 # صورة موجودة: لم تُستبدل
            self.assertEqual(images.get_local(it.id), before)

    async def test_no_guessing_when_ambiguous_or_disabled(self):
        with Server() as s, tempfile.TemporaryDirectory() as d:
            led, images, tb = await self._env(s, d)
            a = await led.add_item("فلتر 1", "HU7008Z", 5, 9, 3)
            b = await led.add_item("فلتر 2", "hu 7008 z", 5, 9, 3)               # نفس الكود بعد التطبيع → غموض
            out = await tb.execute("search_catalogs", {"query": "HU 7008 z", "sources": "fmi"})
            self.assertNotIn("auto_attached", out["result"])
            self.assertIsNone(images.get_local(a.id)); self.assertIsNone(images.get_local(b.id))
        with Server() as s, tempfile.TemporaryDirectory() as d:
            led, images, tb = await self._env(s, d)
            led.settings["autoCatalogImages"] = False
            c = await led.add_item("فلتر", "HU7008Z", 5, 9, 3)
            await tb.execute("search_catalogs", {"query": "HU 7008 z", "sources": "fmi"})
            self.assertIsNone(images.get_local(c.id))                            # الميزة معطّلة من الإعدادات
            self.assertEqual(len(tb.catalog_pictures), 1)                        # لكن الصورة تُعرض للمالك بأزرارها

    async def test_fill_missing_images_bulk(self):
        with Server() as s, tempfile.TemporaryDirectory() as d:
            led, images, tb = await self._env(s, d)
            has = await led.add_item("عنده صورة", "HU7008Z", 5, 9, 3)
            await tb._attach_bytes(has, JPEG)
            await led.add_item("بلا صورة بالكتالوج", "OC 1234", 5, 9, 9)       # السجل موجود لكن بلا حقل صورة
            await led.add_item("غير موجود", "ZZZ-999", 5, 9, 5)
            await led.add_item("بلا كود", "", 5, 9, 5)
            out = await tb.execute("fill_missing_images", {"limit": 10})
            self.assertTrue(out["ok"], out)
            r = out["result"]
            self.assertEqual(r["added"], [])
            self.assertEqual(len(r["not_found_in_catalog"]), 2)                  # OC 1234 و ZZZ-999؛ المحمَّل مسبقاً والبلا كود لم يُفحصا
            # صنف جديد يطابق سجلاً له صورة
            hu = await led.add_item("HU الثاني", "HU 7008 z", 5, 9, 1)
            led._item(has.id).img = None; images._ram.pop(has.id, None)
            try: images._path(has.id).unlink()
            except FileNotFoundError: pass
            out = await tb.execute("fill_missing_images", {"limit": 10})
            r = out["result"]
            self.assertEqual({x["code"] for x in r["added"]}, {"HU7008Z", "HU 7008 z"})
            self.assertEqual(images.get_local(hu.id)[:3], b"\xff\xd8\xff")
            self.assertEqual(len(tb.catalog_pictures[-1]), 2)
            self.assertTrue(tb.notices)

    async def test_fill_requires_account(self):
        with tempfile.TemporaryDirectory() as d:
            led = Ledger(Repository(MemoryStore(), cache_dir=Path(d) / "c")); await led.load()
            tb = ToolBox(led, None, Path(d), None, lambda: True)
            out = await tb.execute("fill_missing_images", {})
            self.assertFalse(out["ok"]); self.assertIn("حساب FileMaker", out["error"])
            self.assertTrue(tb.tools["fill_missing_images"].write)               # تتطلب موافقة المالك


class DeveloperIdentity(unittest.IsolatedAsyncioTestCase):
    def test_prompt_names_developer_and_referral(self):
        from daftari.ai.agent import Assistant, DEVELOPER_REFERRAL
        sysmsg = Assistant(None, None, "متجر العمر").system
        self.assertIn("فواز العمر", sysmsg)
        self.assertIn("إدلب", sysmsg)
        self.assertIn("يرجى مراجعة المطوّر فواز العمر.", sysmsg)
        self.assertEqual(DEVELOPER_REFERRAL, "يرجى مراجعة المطوّر فواز العمر.")
        self.assertIn("لا تنكر", sysmsg)                       # لا ينكر أنه ذكاء اصطناعي
        import re
        self.assertEqual(re.findall(r"\{[a-z_]+\}", sysmsg), [])   # لا متغيرات ناقصة في القالب

    async def test_unknown_tool_points_to_developer(self):
        with tempfile.TemporaryDirectory() as d:
            led = Ledger(Repository(MemoryStore(), cache_dir=Path(d) / "c")); await led.load()
            out = await ToolBox(led, None, Path(d), None, lambda: True).execute("make_coffee", {})
            self.assertFalse(out["ok"])
            self.assertIn("فواز العمر", out["error"])


class OpenCatalogTool(unittest.IsolatedAsyncioTestCase):
    async def test_open_catalog_adds_link(self):
        with tempfile.TemporaryDirectory() as d:
            led = Ledger(Repository(MemoryStore(), cache_dir=Path(d) / "c"))
            await led.load()
            tb = ToolBox(led, None, Path(d), None, lambda: True)
            self.assertIn("open_catalog", [x["name"] for x in tb.declarations()])
            out = await tb.execute("open_catalog", {})
            self.assertTrue(out["ok"], out)
            self.assertEqual(tb.links[-1]["url"], "http://188.59.6.153/fmi/webd/data")
            bad = await tb.execute("open_catalog", {"source": "nope"})
            self.assertFalse(bad["ok"])


if __name__ == "__main__":
    unittest.main()
