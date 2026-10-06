"""حارس update() + بحث الويب (Gemini المؤسَّس ثم المحركات) + read_webpage + النسخ الصوتي (Whisper) + اكتشاف لغة النطق."""
import asyncio, base64, json, sys, tempfile, types, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from daftari.tests import fake_flet
fake_flet.install()

from daftari.ai import websearch as ws
from daftari.ai.gemini import GeminiClient, GeminiConfig, GeminiError
from daftari.ai.llm import ProviderSpec, build_client, make_client
from daftari.ai.tools import ToolBox
from daftari.data.repository import Repository
from daftari.data.store import MemoryStore
from daftari.ledger import Ledger
from daftari.ui import safe


# ---------------------------------------------------------------- حارس update()
class SafeUpdate(unittest.TestCase):
    def test_safe_update_swallows_only_detached_error(self):
        class Detached:
            def update(self): raise RuntimeError("Text(6056) Control must be added to the page first")

        class Other:
            def update(self): raise RuntimeError("something else")
        safe.safe_update(Detached(), None)                      # لا ينهار
        with self.assertRaises(RuntimeError):
            safe.safe_update(Other())                           # الأخطاء الأخرى تظهر كما هي

    def test_install_wraps_base_control_update(self):
        class BaseControl:
            def update(self): raise RuntimeError("Text(1) Control must be added to the page first")

            def ok(self): return 1
        pkg, mod = types.ModuleType("flet.controls"), types.ModuleType("flet.controls.base_control")
        mod.BaseControl = BaseControl
        saved = {k: sys.modules.get(k) for k in ("flet.controls", "flet.controls.base_control")}
        sys.modules["flet.controls"], sys.modules["flet.controls.base_control"] = pkg, mod
        safe._installed = False
        try:
            self.assertTrue(safe.install())
            self.assertIsNone(BaseControl().update())           # كان ينهار → الآن صامت
            self.assertTrue(safe.install())                      # آمن عند التكرار (لا لفّ مزدوج)
            self.assertFalse(hasattr(BaseControl.update.__wrapped__, "__wrapped__"))
        finally:
            safe._installed = False
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_settings_screen_no_longer_calls_update_on_detached_text(self):
        src = (Path(__file__).resolve().parents[1] / "ui" / "tabs" / "settings.py").read_text(encoding="utf-8")
        import re
        self.assertEqual(re.findall(r"^\s+order_out\.update\(\)", src, flags=re.M), [])
        self.assertIn("w.safe_update(order_out)", src)


# ---------------------------------------------------------------- محركات البحث
BING = ('<ol><li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&p=x&u=a1' +
        base64.urlsafe_b64encode(b"https://onfil.example/HF1173").decode().rstrip("=") + '&ntb=1">ONFIL HF1173 Hydraulic Filter</a></h2>'
        '<div class="b_caption"><p>Height 123 mm, OD 93 mm, thread M20x1.5</p></div></li>'
        '<li class="b_algo"><h2><a href="https://other.example/p">Other</a></h2><p>x</p></li></ol>')
MOJEEK = ('<ul class="results-standard"><li><a class="title" href="https://mojeek.example/a">HF1173 spec</a>'
          '<p class="s">Cellulose media, 10 micron</p></li></ul>')


class Engines(unittest.TestCase):
    def test_bing_parser_decodes_redirect_and_snippet(self):
        p = ws._BingParser(); p.feed(BING)
        self.assertEqual(p.results[0]["url"], "https://onfil.example/HF1173")
        self.assertIn("Height 123 mm", p.results[0]["snippet"])
        self.assertEqual(len(p.results), 2)

    def test_mojeek_parser(self):
        p = ws._MojeekParser(); p.feed(MOJEEK)
        self.assertEqual(p.results, [{"title": "HF1173 spec", "url": "https://mojeek.example/a", "snippet": "Cellulose media, 10 micron"}])

    def test_falls_through_blocked_engines_to_one_that_works(self):
        seen = []

        def fetch(url, body, headers, timeout):
            seen.append(url.split("/")[2])
            if "duckduckgo" in url:
                return 202, "anomaly"                              # DDG يحجب
            if "bing.com" in url:
                return 200, "<html>Enable JavaScript</html>"        # Bing بلا نتائج
            if "mojeek" in url:
                return 200, MOJEEK
            return 200, ""
        r = ws.search_web("onfil hf1173", fetch=fetch)
        self.assertEqual(r["engine"], "mojeek")
        self.assertEqual(seen[:3], ["html.duckduckgo.com", "www.bing.com", "www.mojeek.com"])

    def test_all_blocked_reports_each_reason(self):
        with self.assertRaises(ws.SearchError) as cm:
            ws.search_web("x", fetch=lambda *a: (429, ""))
        self.assertIn("duckduckgo", str(cm.exception)); self.assertIn("bing", str(cm.exception))


# ---------------------------------------------------------------- بحث Gemini المؤسَّس + الأداة
GROUNDED = {"candidates": [{"content": {"parts": [{"text": "ONFIL HF1173: الارتفاع 123 مم، القطر الخارجي 93 مم (onfil.example)."}]},
                            "groundingMetadata": {"webSearchQueries": ["onfil hf1173"],
                                                  "groundingChunks": [{"web": {"uri": "https://onfil.example/HF1173", "title": "Onfil"}},
                                                                      {"web": {"uri": "https://onfil.example/HF1173", "title": "dup"}},
                                                                      {"web": {"uri": "https://b.example", "title": "B"}}]}}]}


def transport_factory(log, statuses=None):
    async def tr(url, headers, payload, timeout):
        body = json.loads(payload.decode())
        log.append((url, body))
        status = (statuses or {}).get(url.split("/models/")[1].split(":")[0], 200)
        return status, json.dumps(GROUNDED if status == 200 else {"error": {"message": "model not found"}}).encode()
    return tr


class Grounded(unittest.IsolatedAsyncioTestCase):
    async def test_grounded_search_is_a_standalone_request_with_only_googlesearch(self):
        log = []
        c = GeminiClient(GeminiConfig(api_key="k", model="gemini-3.5-flash-lite"), transport_factory(log), retries=0)
        r = await c.grounded_search("onfil hf1173")
        body = log[0][1]
        self.assertEqual(body["tools"], [{"googleSearch": {}}])               # بلا functionDeclarations
        self.assertIn("HF1173", r["answer"])
        self.assertEqual([s["url"] for s in r["sources"]], ["https://onfil.example/HF1173", "https://b.example"])   # بلا تكرار
        self.assertEqual(c.cfg.model, "gemini-3.5-flash-lite")                # النموذج الأصلي يُستعاد

    async def test_falls_back_to_another_model_when_first_rejects(self):
        log = []
        c = GeminiClient(GeminiConfig(api_key="k", model="gemini-3.5-flash-lite"),
                         transport_factory(log, {"gemini-3.5-flash-lite": 400}), retries=0)
        r = await c.grounded_search("x")
        self.assertEqual(r["model"], "gemini-2.5-flash")
        self.assertEqual(c.cfg.model, "gemini-3.5-flash-lite")

    async def test_tool_prefers_gemini_then_scrapers_and_explains_failure(self):
        with tempfile.TemporaryDirectory() as d:
            led = Ledger(Repository(MemoryStore(), cache_dir=Path(d) / "c")); await led.load()
            tb = ToolBox(led, None, Path(d), None, lambda: True)

            async def good(q): return {"answer": "خلاصة", "sources": [{"title": "t", "url": "https://x.example"}], "queries": ["q"]}
            tb.search_provider = good
            out = await tb.execute("google_search", {"query": "Onfil HF1173"})
            self.assertTrue(out["ok"]); self.assertEqual(out["result"]["engine"], "gemini-google")
            self.assertEqual(out["result"]["sources"][0]["url"], "https://x.example")

            async def bad(q): raise GeminiError("حصة منتهية")
            tb.search_provider = bad
            orig = ws.default_fetch
            ws.default_fetch = lambda url, body, headers, timeout: (200, MOJEEK) if "mojeek" in url else (200, "")
            try:
                out = await tb.execute("google_search", {"query": "x"})
            finally:
                ws.default_fetch = orig
            self.assertTrue(out["ok"]); self.assertEqual(out["result"]["engine"], "mojeek")
            self.assertIn("حصة منتهية", out["result"]["note"])

            ws.default_fetch = lambda *a: (429, "")
            try:
                out = await tb.execute("google_search", {"query": "x"})
            finally:
                ws.default_fetch = orig
            self.assertFalse(out["ok"])
            self.assertIn("حصة منتهية", out["error"]); self.assertIn("duckduckgo", out["error"])    # الأسباب الحقيقية تصل للنموذج

    async def test_read_webpage_blocks_local_addresses_and_reads_public(self):
        from daftari.ai import catalogs
        with tempfile.TemporaryDirectory() as d:
            led = Ledger(Repository(MemoryStore(), cache_dir=Path(d) / "c")); await led.load()
            tb = ToolBox(led, None, Path(d), None, lambda: True)
            for bad in ("ftp://x.com", "http://127.0.0.1/admin", "http://192.168.1.1/", "javascript:alert(1)"):
                self.assertFalse((await tb.execute("read_webpage", {"url": bad}))["ok"], bad)
            html = "<html><body><h1>HF1173</h1><table><tr><td>Height</td><td>123 mm</td></tr></table></body></html>"
            with mock.patch.object(catalogs, "fetch_url", lambda u: html), \
                 mock.patch("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]):
                out = await tb.execute("read_webpage", {"url": "https://onfil.example/hf1173"})
            self.assertTrue(out["ok"], out)
            self.assertIn("Height | 123 mm", out["result"]["content"])

    async def test_multiclient_grounded_uses_first_gemini_even_if_primary_is_other(self):
        log = []
        specs = [ProviderSpec("openai", "openai", "sk", "gpt-4o-mini", "https://api.openai.com/v1"),
                 ProviderSpec("gemini", "gemini", "g-key", "gemini-3.5-flash-lite", "")]
        mc = build_client(specs, transport=transport_factory(log))
        r = await mc.grounded_search("x")
        self.assertIn("answer", r)


# ---------------------------------------------------------------- نسخ الصوت عبر OpenAI/Groq
class Whisper(unittest.IsolatedAsyncioTestCase):
    async def test_openai_transcription_multipart(self):
        seen = {}

        async def tr(url, headers, payload, timeout):
            seen.update(url=url, headers=headers, payload=payload)
            return 200, json.dumps({"text": "فلتر زيت تويوتا"}).encode()
        spec = ProviderSpec("openai", "openai", "sk-1", "gpt-4o-mini", "https://api.openai.com/v1")
        c = make_client(spec, 0, tr)
        text = await c.transcribe_audio(b"RIFF....WAVEdata", "audio/wav")
        self.assertEqual(text, "فلتر زيت تويوتا")
        self.assertTrue(seen["url"].endswith("/audio/transcriptions"))
        self.assertTrue(seen["headers"]["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="model"', seen["payload"]); self.assertIn(b"whisper-1", seen["payload"])
        self.assertIn(b'filename="voice.wav"', seen["payload"]); self.assertIn(b"RIFF....WAVEdata", seen["payload"])
        self.assertEqual(seen["headers"]["Authorization"], "Bearer sk-1")

    async def test_unsupported_provider_and_error_mapping(self):
        spec = ProviderSpec("deepseek", "openai", "k", "deepseek-chat", "https://api.deepseek.com/v1")
        with self.assertRaises(GeminiError):
            await make_client(spec, 0, None).transcribe_audio(b"x" * 10)

        async def tr401(url, headers, payload, timeout): return 401, b'{"error":{"message":"bad key"}}'
        with self.assertRaises(GeminiError) as cm:
            await make_client(ProviderSpec("groq", "openai", "k", "m", "https://api.groq.com/openai/v1"), 0, tr401).transcribe_audio(b"x" * 10)
        self.assertIn("مرفوض", str(cm.exception))

    async def test_multiclient_falls_from_gemini_to_openai(self):
        calls = []

        async def tr(url, headers, payload, timeout):
            calls.append(url)
            if "generativelanguage" in url:
                return 429, b'{"error":{"message":"quota","status":"RESOURCE_EXHAUSTED"}}'
            return 200, json.dumps({"text": "مرحبا"}).encode()
        specs = [ProviderSpec("gemini", "gemini", "g", "gemini-3.5-flash-lite", ""),
                 ProviderSpec("openai", "openai", "sk", "gpt-4o-mini", "https://api.openai.com/v1")]
        mc = build_client(specs, transport=tr)
        for sl in mc.slots:
            sl.client.retries = 0
        self.assertEqual(await mc.transcribe_audio(b"RIFF" + b"\0" * 2000, "audio/wav"), "مرحبا")
        self.assertTrue(any("generativelanguage" in u for u in calls) and any("audio/transcriptions" in u for u in calls))


# ---------------------------------------------------------------- لغة النطق
class TTS(unittest.IsolatedAsyncioTestCase):
    def test_detect_and_plan(self):
        from daftari.ai.tts import detect_lang, speech_plan
        self.assertEqual((detect_lang("مرحبا بك"), detect_lang("HU 7008 oil filter"), detect_lang("123")), ("ar", "en", "ar"))
        self.assertEqual(speech_plan("مصفاية زيت HU-7008 z من MANN"),
                         [("ar", "مصفاية زيت"), ("en", "HU-7008 z"), ("ar", "من"), ("en", "MANN")])
        self.assertEqual(speech_plan("Oil filter"), [("en", "Oil filter")])
        self.assertEqual(speech_plan(""), [])

    async def test_synthesize_passes_lang_per_chunk_and_returns_wav(self):
        from daftari.ai import tts
        sent = []

        async def tr(url, headers, payload, timeout):
            body = json.loads(payload.decode()); sent.append((url, body["contents"][0]["parts"][0]["text"]))
            pcm = base64.b64encode(b"\x01\x00" * 100).decode()
            return 200, json.dumps({"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16;rate=24000", "data": pcm}}]}}]}).encode()
        c = GeminiClient(GeminiConfig(api_key="k", model="gemini-3.5-flash-lite"), tr, retries=0)
        wav = await tts.synthesize(c, "زيت MANN")
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[0][1].startswith("انطق بالعربية")); self.assertTrue(sent[1][1].startswith("Say in English"))
        self.assertTrue(all(tts.TTS_MODEL in u for u, _ in sent))
        self.assertEqual(c.cfg.model, "gemini-3.5-flash-lite")


if __name__ == "__main__":
    unittest.main()
