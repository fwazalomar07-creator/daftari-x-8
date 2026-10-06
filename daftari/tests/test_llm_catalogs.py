"""اختبارات: تحليل 429، مزوّدو OpenAI/Anthropic، التبديل التلقائي بين المزوّدين، وكتالوجات الفلاتر."""
import asyncio, json, unittest
from unittest import mock

from daftari.ai import catalogs as cat
from daftari.ai.agent import Assistant
from daftari.ai.gemini import GeminiClient, GeminiConfig, GeminiError, RateLimitError, parse_rate_info
from daftari.ai.llm import (AnthropicClient, MultiClient, OpenAIClient, ProviderSpec, _Slot, build_client, load_specs)


class FakeTransport:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    async def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body)))
        return self.responses.pop(0)


def j(status, body):
    return status, json.dumps(body).encode()


GEM_OK = lambda t: j(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": t}]}, "finishReason": "STOP"}]})
Q429_MIN = {"error": {"code": 429, "message": "quota", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}]}}
Q429_DAY = {"error": {"code": 429, "message": "quota", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}

TOOLS = [{"name": "search_items", "description": "d",
          "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING", "description": "q"}}, "required": ["query"]}}]
HIST = [
    {"role": "user", "parts": [{"text": "ابحث عن فلتر"}]},
    {"role": "model", "parts": [{"text": "سأبحث"}, {"functionCall": {"name": "search_items", "args": {"query": "oil"}}}]},
    {"role": "user", "parts": [{"functionResponse": {"name": "search_items", "response": {"ok": True, "result": [1]}}}]},
]


class RateInfoTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_minute_and_daily(self):
        self.assertEqual(parse_rate_info(Q429_MIN), (34.0, False, False))
        self.assertEqual(parse_rate_info(Q429_DAY)[1], True)
        self.assertTrue(parse_rate_info({"error": {"message": "Quota exceeded, limit: 0"}})[2])

    async def test_daily_quota_not_retried_and_message_explains(self):
        tr = FakeTransport(j(429, Q429_DAY), GEM_OK("never"))
        with self.assertRaises(RateLimitError) as cm:
            await GeminiClient(GeminiConfig(api_key="K"), tr, retries=3, backoff=0).generate([])
        self.assertEqual(len(tr.calls), 1)                   # لم تُحرق طلبات إضافية
        self.assertTrue(cm.exception.daily)
        self.assertIn("اليومية", str(cm.exception))
        self.assertIn("Google AI Pro", str(cm.exception))   # يشرح أن اشتراك التطبيق ≠ حصة API

    async def test_minute_quota_waits_retry_delay_then_succeeds(self):
        tr = FakeTransport(j(429, {"error": {"details": [{"@type": "x.RetryInfo", "retryDelay": "2s"}]}}), GEM_OK("ok"))
        slept = []

        async def fake_sleep(s):
            slept.append(s)
        with mock.patch("daftari.ai.gemini.asyncio.sleep", fake_sleep):
            r = await GeminiClient(GeminiConfig(api_key="K"), tr, retries=2, backoff=0).generate([])
        self.assertEqual(r.text, "ok")
        self.assertEqual(slept, [2.0])


class OpenAIAdapterTests(unittest.IsolatedAsyncioTestCase):
    def spec(self, **kw):
        return ProviderSpec("groq", "openai", "gk", "llama", "https://api.groq.com/openai/v1", **kw)

    def test_message_and_tool_conversion(self):
        body = OpenAIClient(self.spec()).build_body(HIST, "sys", TOOLS)
        m = body["messages"]
        self.assertEqual(m[0], {"role": "system", "content": "sys"})
        self.assertEqual(m[1], {"role": "user", "content": "ابحث عن فلتر"})
        self.assertEqual(m[2]["tool_calls"][0]["function"]["name"], "search_items")
        self.assertEqual(json.loads(m[2]["tool_calls"][0]["function"]["arguments"]), {"query": "oil"})
        self.assertEqual(m[3]["role"], "tool")
        self.assertEqual(m[3]["tool_call_id"], m[2]["tool_calls"][0]["id"])     # الربط بالمعرّف سليم
        self.assertEqual(body["tools"][0]["function"]["parameters"]["type"], "object")
        self.assertEqual(body["tools"][0]["function"]["parameters"]["properties"]["query"]["type"], "string")

    def test_image_becomes_image_url(self):
        h = [{"role": "user", "parts": [{"inlineData": {"mimeType": "image/jpeg", "data": "AAA"}}, {"text": "ما هذا؟"}]}]
        m = OpenAIClient(self.spec()).build_body(h, None, None)["messages"][0]
        self.assertEqual(m["content"][0]["image_url"]["url"], "data:image/jpeg;base64,AAA")

    async def test_generate_parses_tool_calls_into_gemini_shape(self):
        tr = FakeTransport(j(200, {"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "x", "type": "function",
                                             "function": {"name": "search_items", "arguments": "{\"query\": \"zeit\"}"}}]}}]}))
        r = await OpenAIClient(self.spec(), tr).generate(HIST, "s", TOOLS)
        self.assertEqual(r.calls, [{"name": "search_items", "args": {"query": "zeit"}}])
        self.assertIn("functionCall", r.content["parts"][0])
        self.assertEqual(tr.calls[0][1]["Authorization"], "Bearer gk")
        self.assertTrue(tr.calls[0][0].endswith("/chat/completions"))

    def test_groq_gets_lite_toolset(self):
        many = [{"name": n, "description": "d"} for n in ("search_items", "add_item", "create_invoice", "search_catalogs")]
        names = [t["function"]["name"] for t in OpenAIClient(self.spec()).build_body(HIST, None, many)["tools"]]
        self.assertEqual(names, ["search_items", "search_catalogs"])          # أدوات الكتابة محذوفة عن Groq الصغير
        other = ProviderSpec("openai", "openai", "k", "m", "https://api.openai.com/v1")
        self.assertEqual(len(OpenAIClient(other).build_body(HIST, None, many)["tools"]), 4)

    async def test_local_model_needs_no_key(self):
        sp = ProviderSpec.from_dict({"id": "ollama"})
        self.assertTrue(sp.ready)
        self.assertNotIn("Authorization", OpenAIClient(sp)._headers())

    async def test_429_becomes_ratelimit(self):
        with self.assertRaises(RateLimitError):
            await OpenAIClient(self.spec(), FakeTransport(j(429, {"error": {"message": "slow down"}})), retries=0).generate(HIST)


class AnthropicAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_conversion_and_roles_alternate(self):
        sp = ProviderSpec("anthropic", "anthropic", "k", "claude-x", "https://api.anthropic.com/v1")
        body = AnthropicClient(sp).build_body(HIST, "sys", TOOLS)
        self.assertEqual(body["system"], "sys")
        roles = [m["role"] for m in body["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user"])
        tu = body["messages"][1]["content"][1]
        self.assertEqual((tu["type"], tu["name"], tu["input"]), ("tool_use", "search_items", {"query": "oil"}))
        tr = body["messages"][2]["content"][0]
        self.assertEqual((tr["type"], tr["tool_use_id"]), ("tool_result", tu["id"]))
        self.assertEqual(body["tools"][0]["input_schema"]["type"], "object")

    async def test_generate_parse(self):
        sp = ProviderSpec("anthropic", "anthropic", "k", "claude-x", "https://api.anthropic.com/v1")
        tr = FakeTransport(j(200, {"content": [{"type": "text", "text": "تمام"},
                                               {"type": "tool_use", "id": "t", "name": "search_items", "input": {"query": "a"}}],
                                   "stop_reason": "tool_use"}))
        r = await AnthropicClient(sp, tr).generate(HIST, None, TOOLS)
        self.assertEqual(r.text, "تمام")
        self.assertEqual(r.calls[0]["name"], "search_items")
        self.assertEqual(tr.calls[0][1]["x-api-key"], "k")


class RouterTests(unittest.IsolatedAsyncioTestCase):
    def gem(self, *resp):
        tr = FakeTransport(*resp)
        sp = ProviderSpec("gemini", "gemini", "K", "m1", "https://generativelanguage.googleapis.com/v1beta")
        return _Slot(sp, GeminiClient(GeminiConfig(api_key="K", model="m1"), tr, retries=0)), tr

    def oai(self, *resp):
        tr = FakeTransport(*resp)
        sp = ProviderSpec("groq", "openai", "gk", "llama", "https://api.groq.com/openai/v1")
        return _Slot(sp, OpenAIClient(sp, tr, retries=0)), tr

    async def test_falls_back_on_quota_and_rests_failed_provider(self):
        g, gtr = self.gem(j(429, Q429_DAY), GEM_OK("لن يُستدعى"))
        o, otr = self.oai(j(200, {"choices": [{"message": {"content": "من groq"}}]}),
                          j(200, {"choices": [{"message": {"content": "ثانية"}}]}))
        mc = MultiClient([g, o])
        r = await mc.generate([{"role": "user", "parts": [{"text": "hi"}]}])
        self.assertEqual((r.text, mc.last_used), ("من groq", "groq"))
        self.assertIn("Google Gemini", mc.last_errors[0])
        r2 = await mc.generate([{"role": "user", "parts": [{"text": "hi"}]}])      # Gemini في راحة → مباشرةً groq
        self.assertEqual(r2.text, "ثانية")
        self.assertEqual(len(gtr.calls), 1)

    async def test_single_provider_keeps_original_error(self):
        g, _ = self.gem(j(403, {}))
        with self.assertRaises(GeminiError) as cm:
            await MultiClient([g]).generate([])
        self.assertIn("مرفوض", str(cm.exception))

    async def test_all_fail_lists_every_reason(self):
        g, _ = self.gem(j(429, Q429_DAY))
        o, _ = self.oai(j(401, {"error": {"message": "bad key"}}))
        with self.assertRaises(GeminiError) as cm:
            await MultiClient([g, o]).generate([])
        self.assertIn("Google Gemini", str(cm.exception))
        self.assertIn("فشلت كل النماذج", str(cm.exception))

    async def test_agent_switches_provider_mid_tool_loop(self):
        """Gemini يطلب أداة، ثم تنتهي حصته، فيكمل groq من نفس السجل ويعطي الجواب النهائي."""
        call = [{"functionCall": {"name": "business_summary", "args": {}}}]
        g, _ = self.gem(j(200, {"candidates": [{"content": {"role": "model", "parts": call}}]}), j(429, Q429_DAY))
        o, otr = self.oai(j(200, {"choices": [{"message": {"content": "الملخص جاهز"}}]}))

        class Tools:
            def declarations(self): return TOOLS
            async def execute(self, name, args): return {"ok": True, "result": {"items": 3}}
        a = Assistant(MultiClient([g, o]), Tools(), "م")
        out = await a.ask("لخّص")
        self.assertEqual(out, "الملخص جاهز")
        sent = otr.calls[0][2]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "tool"])   # سجل Gemini تُرجم سليماً

    def test_gemini3_gets_signature_shim_for_foreign_history(self):
        c = GeminiClient(GeminiConfig(api_key="K", model="gemini-3-flash"))
        out = c._prepare_contents(HIST)
        self.assertEqual(out[1]["parts"][1]["thoughtSignature"], "skip_thought_signature_validator")
        self.assertNotIn("thoughtSignature", HIST[1]["parts"][1])                 # الأصل لم يُمسّ


class SpecLoadingTests(unittest.TestCase):
    def test_env_and_settings_merge_and_order(self):
        env = {"GEMINI_API_KEY": "g", "GEMINI_MODEL": "gm", "GROQ_API_KEY": "gq", "LLM_ORDER": "groq,gemini"}
        sp = load_specs({"llmProviders": [{"id": "openai", "api_key": "ok", "model": "gpt-x"}]}, env)
        ids = [x.id for x in sp]
        self.assertEqual(ids[:2], ["groq", "gemini"])
        self.assertIn("openai", ids)
        self.assertEqual(next(x for x in sp if x.id == "gemini").model, "gm")
        self.assertTrue(next(x for x in sp if x.id == "groq").ready)
        self.assertEqual(next(x for x in sp if x.id == "groq").base_url, "https://api.groq.com/openai/v1")

    def test_build_client_reduces_internal_retries_when_multiple(self):
        sp = load_specs({}, {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"})
        self.assertEqual(build_client(sp).retries, 1)
        self.assertEqual(build_client(load_specs({}, {"GEMINI_API_KEY": "g"})).retries, 3)


# ===================================================================================== الكتالوجات
SEARCH_PAGE = """<html><body><form action="sonuc.php" method="post">
<input type="hidden" name="tok" value="abc"><input type="text" name="arama"><select name="dil"><option value="tr">TR</option></select>
<input type="submit" name="btn" value="Ara"></form></body></html>"""
RESULT_PAGE = """<html><head><title>x</title><script>var a=1</script></head><body><table>
<tr><th>Kod</th><th>Marka</th><th>Referans</th></tr><tr><td>SF 123</td><td>Sardes</td><td>MANN HU 7008 z</td></tr></table></body></html>"""


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    def test_html_to_text_tables_and_scripts(self):
        t = cat.html_to_text(RESULT_PAGE)
        self.assertIn("SF 123 | Sardes | MANN HU 7008 z", t)
        self.assertNotIn("var a", t)

    def test_pick_form_finds_query_field(self):
        form, name = cat.pick_search_form(SEARCH_PAGE)
        self.assertEqual((name, form.method, form.action), ("arama", "post", "sonuc.php"))
        self.assertEqual(form.selects["dil"], "tr")

    def test_form_search_submits_with_hidden_fields_and_cookies(self):
        sent = {}

        class FakeHttp:
            def request(self, url, data=None, referer=""):
                if data is None:
                    return SEARCH_PAGE, "http://k.example/index.php?sayfa=ara"
                sent.update(url=url, data=data.decode(), referer=referer)
                return RESULT_PAGE, url
        with mock.patch.object(cat, "_Http", FakeHttp):
            raw = cat.form_search(cat.CatalogSource("s", "S", "http://k.example/index.php?sayfa=ara"), "SF 123")
        self.assertEqual(sent["url"], "http://k.example/sonuc.php")
        for frag in ("tok=abc", "arama=SF+123", "dil=tr", "btn=Ara"):
            self.assertIn(frag, sent["data"])
        self.assertIn("MANN HU 7008 z", raw)

    async def test_search_one_ok_via_form(self):
        src = cat.CatalogSource("s", "Sardes", "http://k.example/x", "form")
        with mock.patch.object(cat, "form_search", lambda s, q: RESULT_PAGE):
            r = await cat.search_one(src, "SF 123")
        self.assertEqual(r["status"], "ok")
        self.assertIn("Sardes", r["text"])

    async def test_search_one_falls_back_to_site_search(self):
        src = cat.CatalogSource("m", "MANN", "https://catalog.mann-filter.com/EU/tur/x", "browser")

        async def boom(*a, **k):
            raise cat.CatalogError("Playwright غير مثبت")
        with mock.patch.object(cat, "browser_search", boom), \
                mock.patch.object(cat, "site_search", lambda s, q, fetch=None: "HU 7008 z — https://catalog.mann-filter.com/p/1 \n  filtre yağ"):
            r = await cat.search_one(src, "HU 7008 z")
        self.assertEqual((r["status"], r["via"]), ("ok", "site"))
        self.assertIn("Playwright", r["error"])

    async def test_search_one_reports_error_when_everything_fails(self):
        src = cat.CatalogSource("f", "FMI", "http://188.59.6.153/fmi/webd/data", "browser")

        async def boom(*a, **k):
            raise cat.CatalogError("Playwright غير مثبت")

        def nosite(*a, **k):
            raise cat.CatalogError("لا نتائج")
        with mock.patch.object(cat, "browser_search", boom), mock.patch.object(cat, "site_search", nosite):
            r = await cat.search_one(src, "x")
        self.assertEqual(r["status"], "error")
        self.assertIn("Playwright", r["error"])

    async def test_search_catalogs_runs_all_enabled_and_filters(self):
        srcs = [cat.CatalogSource("a", "A", "http://a.example", "form"),
                cat.CatalogSource("b", "B", "http://b.example", "form", enabled=False),
                cat.CatalogSource("c", "C", "http://c.example", "form")]
        with mock.patch.object(cat, "form_search", lambda s, q: RESULT_PAGE):
            res = await cat.search_catalogs("SF 123", srcs)
            self.assertEqual([r["id"] for r in res["sources"]], ["a", "c"])
            only = await cat.search_catalogs("SF 123", srcs, only=["c"])
        self.assertEqual([r["id"] for r in only["sources"]], ["c"])
        with self.assertRaises(cat.CatalogError):
            await cat.search_catalogs("  ", srcs)

    def test_merged_sources_user_overrides_default(self):
        out = cat.merged_sources({"catalogs": [{"id": "delsa", "name": "Delsa", "url": "https://x.example/katalog", "mode": "form"},
                                               {"id": "yeni", "name": "جديد", "url": "http://n.example"}]})
        ids = [s.id for s in out]
        self.assertEqual(ids[:5], ["fmi", "sardes", "sardes-xref", "mann", "delsa"])
        self.assertEqual(next(s for s in out if s.id == "delsa").url, "https://x.example/katalog")
        self.assertIn("yeni", ids)


class CatalogToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_registered_and_executes(self):
        from daftari.ai.tools import ToolBox
        from daftari.data.repository import Repository
        from daftari.data.store import MemoryStore
        from daftari.ledger import Ledger
        led = Ledger(Repository(MemoryStore()))
        await led.load()
        tb = ToolBox(led)
        self.assertIn("search_catalogs", [d["name"] for d in tb.declarations()])
        with mock.patch.object(cat, "form_search", lambda s, q: RESULT_PAGE):
            out = await tb.execute("search_catalogs", {"query": "SF 123", "sources": "sardes"})
        self.assertTrue(out["ok"])
        self.assertEqual([r["id"] for r in out["result"]["sources"]], ["sardes"])
        bad = await tb.execute("search_catalogs", {"query": "x", "sources": "nonexistent"})
        self.assertFalse(bad["ok"])


if __name__ == "__main__":
    unittest.main()
