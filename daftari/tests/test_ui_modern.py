"""اختبارات الواجهة الجديدة: كتالوج الفاتورة، تخطيط POS، شاشة المساعد وموافقاتها، والتحديث من السحابة."""
import asyncio, json
from decimal import Decimal as D

from daftari.tests.test_ui_smoke import Smoke, walk, find_btn   # يثبّت flet الوهمية أيضاً
from daftari.tests.test_ai import FakeTransport, reply_json
from daftari.ai.agent import Assistant
from daftari.ai.gemini import GeminiClient, GeminiConfig
from daftari.ai.tools import ToolBox
from daftari.ui.tabs import assistant as assistant_tab


def texts(ctl):
    return [c.args[0] for c in walk(ctl) if getattr(c, "args", None) and isinstance(c.args[0], str)]


class ModernUI(Smoke):
    async def test_invoice_catalog_shows_all_items_without_searching(self):
        self.app.tab, self.app.sub = "invoice", {}
        ctl = await self.app._build_current()
        t = texts(ctl)
        self.assertIn("فلتر زيت تويوتا", t)          # بدون أي بحث
        self.assertIn("زيت محرك 5W30", t)
        await self.led.add_item("صنف أُضيف للتو", "NEW1", 1, 2, 5)
        ctl = await self.app._build_current()
        self.assertIn("صنف أُضيف للتو", texts(ctl))  # الأصناف الجديدة تظهر فوراً

    async def test_invoice_catalog_puts_out_of_stock_last_and_marks_them(self):
        await self.led.add_item("أ نفد", "Z0", 1, 2, 0)
        self.app.tab, self.app.sub = "invoice", {}
        t = texts(await self.app._build_current())
        self.assertLess(t.index("فلتر زيت تويوتا"), t.index("أ نفد"))
        self.assertIn("نفد", t)

    async def test_pos_split_layout_on_wide_screens(self):
        self.page.width = 1400
        self.app._wide = True
        self.assertTrue(self.app.pos_split())
        self.app.tab, self.app.sub = "invoice", {}
        ctl = await self.app._build_current()
        self.assertTrue(any(getattr(c, "kw", {}).get("width") == 420 for c in walk(ctl)))   # لوحة الفاتورة
        self.page.width = 900
        self.assertFalse(self.app.pos_split())

    async def test_cart_totals_after_adding_from_catalog(self):
        self.app.tab, self.app.sub = "invoice", {}
        ctl = await self.app._build_current()
        tile = next(c for c in walk(ctl) if getattr(c, "kw", {}).get("width") == 176 and c.kw.get("on_click")
                    and "فلتر زيت تويوتا" in texts(c))
        tile.on_click(None)
        self.assertEqual(sum(self.app.invoice_draft.cart.values()), 1)

    async def test_customers_history_inventory_new_layouts_build(self):
        for key in ("customers", "history", "inventory", "assistant", "more"):
            self.app.tab, self.app.sub = key, {}
            self.assertGreater(len(list(walk(await self.app._build_current()))), 5, key)

    async def test_refresh_without_cloud_warns(self):
        await self.app.refresh_cloud()
        self.assertTrue(self.page.dialogs)            # toast خطأ: وضع محلي

    async def test_refresh_with_cloud_reloads_other_device_changes(self):
        store = self.led.repo.store
        raw, ts = await store.get("inventory")
        raw.append({"id": "other1", "name": "صنف من جهاز آخر", "code": "O", "cost": 1, "price": 2, "stock": 3})
        await store.put_if_unchanged("inventory", raw, ts)
        self.app.store = store
        await self.app.refresh_cloud()
        self.assertTrue(any(i.name == "صنف من جهاز آخر" for i in self.led.inventory))

    async def test_go_auto_reloads_stale_inventory_from_cloud(self):
        store = self.led.repo.store
        raw, ts = await store.get("inventory")
        raw.append({"id": "other2", "name": "جهاز ثانٍ", "code": "O2", "cost": 1, "price": 2, "stock": 3})
        await store.put_if_unchanged("inventory", raw, ts)
        self.app.store = store
        self.led.last_loaded = 0
        await self.app.go("invoice")      # الرسم فوراً، والتحديث من السحابة يجري بمهمة خلفية
        bg = [t for t in self.page.tasks if t[0] == self.app._bg_reload]
        self.assertTrue(bg)
        await bg[-1][0](*bg[-1][1])
        self.assertTrue(any(i.name == "جهاز ثانٍ" for i in self.led.inventory))


class AssistantUI(Smoke):
    def prime(self, *responses, auto=False):
        cfg = GeminiConfig(api_key="K")
        tools = ToolBox(self.led, None, self.app.home, assistant_tab._confirm_factory(self.app), lambda: self.app.ai_state["auto"])
        self.app.ai_state.update(messages=[], busy=False, auto=auto, cfg=cfg,
                                 assistant=Assistant(GeminiClient(cfg, FakeTransport(*responses)), tools, "المتجر"))

    async def send(self, ctl, text):
        tf = next(c for c in walk(ctl) if getattr(c, "kw", {}).get("label", "").startswith("اكتب سؤالك"))
        tf.value = text
        await find_btn(ctl, "إرسال").on_click(None)

    async def test_setup_card_when_no_key(self):
        self.app.ai_state.update(messages=[], busy=False, auto=False, assistant=None, cfg=GeminiConfig())
        self.app.tab, self.app.sub = "assistant", {}
        t = " ".join(texts(await self.app._build_current()))
        self.assertIn("مزوّدو الذكاء الاصطناعي", t)      # بطاقة الإعداد توجّه للإعدادات (أي مزوّد، لا Gemini وحده)

    async def test_chat_roundtrip_with_tool(self):
        self.prime(reply_json([{"functionCall": {"name": "business_summary", "args": {}}}]),
                   reply_json([{"text": "عندك صنفان."}]))
        self.app.tab, self.app.sub = "assistant", {}
        ctl = await self.app._build_current()
        await self.send(ctl, "ملخص")
        msgs = self.app.ai_state["messages"]
        self.assertEqual([m[0] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[1][1], "عندك صنفان.")
        self.assertFalse(self.app.ai_state["busy"])

    async def test_excel_export_tool_adds_file_card_without_approval(self):
        """طلب Excel: ينفَّذ بلا حوار موافقة (لا يغيّر البيانات)، ويظهر ملف بزرّي فتح/حفظ، ولا يُكتب مساره في الرد."""
        self.prime(reply_json([{"functionCall": {"name": "export_dataset", "args": {"dataset": "inventory"}}}]),
                   reply_json([{"text": "جاهز: ملف المخزون أدناه."}]))
        self.app.tab, self.app.sub = "assistant", {}
        ctl = await self.app._build_current()
        await self.send(ctl, "صدّر المخزون إلى إكسل")
        self.assertFalse(self.page.dialogs)                                   # لا موافقة مطلوبة
        msgs = self.app.ai_state["messages"]
        self.assertEqual([m[0] for m in msgs], ["user", "assistant", "file"])
        self.assertTrue(msgs[2][1].endswith(".xlsx"))
        from pathlib import Path
        self.assertTrue(Path(msgs[2][1]).exists())
        ctl2 = await self.app._build_current()
        t = texts(ctl2)
        self.assertTrue(any(x.endswith(".xlsx") for x in t))
        self.assertIn("فتح", t)
        self.assertIn("حفظ باسم…", t)

    async def test_markdown_table_reply_gets_excel_button(self):
        table = "## أرباح\n| الشهر | الربح |\n|--|--:|\n| أكتوبر | $50 |\n"
        self.prime(reply_json([{"text": table}]))
        self.app.tab, self.app.sub = "assistant", {}
        ctl = await self.app._build_current()
        await self.send(ctl, "جدول")
        t = texts(await self.app._build_current())
        self.assertIn("تصدير الجدول إلى Excel", t)
        self.prime(reply_json([{"text": "لا جداول هنا."}]))
        ctl = await self.app._build_current()
        await self.send(ctl, "سؤال")
        self.assertNotIn("تصدير الجدول إلى Excel", texts(await self.app._build_current()))

    async def test_write_waits_for_user_approval_in_dialog(self):
        self.prime(reply_json([{"functionCall": {"name": "add_item", "args": {"name": "صنف المساعد", "cost": 2, "price": 4, "stock": 3}}}]),
                   reply_json([{"text": "تمت الإضافة."}]))
        self.app.tab, self.app.sub = "assistant", {}
        ctl = await self.app._build_current()
        task = asyncio.create_task(self.send(ctl, "أضف صنفاً"))
        for _ in range(50):
            await asyncio.sleep(0)
            if self.page.dialogs:
                break
        self.assertTrue(self.page.dialogs, "لم يظهر حوار الموافقة")
        self.assertFalse(any(i.name == "صنف المساعد" for i in self.led.inventory))   # لم يُنفَّذ قبل الموافقة
        self.page.dialogs[-1].actions[1].on_click(None)                              # موافقة
        await task
        self.assertTrue(any(i.name == "صنف المساعد" for i in self.led.inventory))
        self.assertEqual(self.app.ai_state["messages"][-1][1], "تمت الإضافة.")

    async def test_rejection_keeps_data_unchanged(self):
        self.prime(reply_json([{"functionCall": {"name": "add_item", "args": {"name": "مرفوض", "cost": 1, "price": 2}}}]),
                   reply_json([{"text": "حسناً، لم أضف شيئاً."}]))
        self.app.tab, self.app.sub = "assistant", {}
        ctl = await self.app._build_current()
        task = asyncio.create_task(self.send(ctl, "أضف"))
        for _ in range(50):
            await asyncio.sleep(0)
            if self.page.dialogs:
                break
        self.page.dialogs[-1].actions[0].on_click(None)                              # رفض
        await task
        self.assertFalse(any(i.name == "مرفوض" for i in self.led.inventory))

    async def test_api_error_shown_as_error_bubble_not_crash(self):
        self.prime((403, b"{}"))
        self.app.ai_state["assistant"].client.retries = 0
        self.app.tab, self.app.sub = "assistant", {}
        await self.send(await self.app._build_current(), "hi")
        self.assertEqual(self.app.ai_state["messages"][-1][0], "error")
        self.assertIn("مرفوض", self.app.ai_state["messages"][-1][1])

    async def test_composer_has_image_and_voice_buttons(self):
        self.app.tab, self.app.sub = "assistant", {}
        ctl = await self.app._build_current()
        tips = [c.kw.get("tooltip") for c in walk(ctl) if getattr(c, "kw", None)]
        self.assertIn("رفع صورة أو ملف Excel", tips)
        self.assertIn("تعرف صوتي", tips)

    async def test_image_upload_then_send_includes_image_bubble(self):
        from daftari.tests.test_motion_images_docs import _jpeg
        jpeg = _jpeg()

        async def fake_pick(*a, **kw):
            return ("part.jpg", jpeg)
        self.app.pick_file_bytes = fake_pick
        self.prime(reply_json([{"text": "هذه قطعة فلتر زيت."}]))
        self.app.tab, self.app.sub = "assistant", {}
        ctl = await self.app._build_current()
        up = next(c for c in walk(ctl) if getattr(c, "kw", {}).get("tooltip") == "رفع صورة أو ملف Excel")
        await up.on_click(None)
        self.assertTrue(self.app.ai_state["pending"])
        await self.send(ctl, "ما هذه؟")
        roles = [m[0] for m in self.app.ai_state["messages"]]
        self.assertEqual(roles[:3], ["user_image", "user", "assistant"])
        self.assertFalse(self.app.ai_state["pending"])
        parts = self.app.ai_state["assistant"].client._transport.calls[0][2]["contents"][0]["parts"]
        self.assertIn("inlineData", parts[0])

    async def test_voice_records_transcribes_and_sends(self):
        self.prime(reply_json([{"text": "ملخص اليوم"}]),
                   reply_json([{"text": "هذا ملخص المتجر."}]))
        class FakeVoice:                       # مسجّل مباشر وهمي (بدل ميكروفون حقيقي)
            recording, elapsed = False, 0

            async def start(self):
                self.recording = True

            async def stop(self):
                self.recording = False
                return b"RIFF\0\0\0\0WAVEfmt ", "audio/wav"

            async def cancel(self):
                self.recording = False
        self.app._voice = fv = FakeVoice()
        self.app.tab, self.app.sub = "assistant", {}
        ctl = await self.app._build_current()
        mic = next(c for c in walk(ctl) if getattr(c, "kw", {}).get("tooltip") == "تعرف صوتي")
        await mic.on_click(None)
        self.assertTrue(self.app.ai_state["listening"])
        self.assertTrue(fv.recording)              # بدأ التسجيل مباشرة بلا اختيار ملف
        stop = next(c for c in walk(ctl) if getattr(c, "kw", {}).get("tooltip") == "إيقاف التسجيل")
        await stop.on_click(None)
        msgs = self.app.ai_state["messages"]
        self.assertEqual(msgs[0][1], "ملخص اليوم")
        self.assertEqual(msgs[1][1], "هذا ملخص المتجر.")
        self.assertFalse(self.app.ai_state["listening"])


if __name__ == "__main__":
    import unittest; unittest.main()


class DashboardData(Smoke):
    """لوحة التحكم تعرض فواتير اليوم وأرباحها فعلاً (كانت تظهر فارغة)."""

    async def test_dashboard_shows_today_invoice_and_profit(self):
        self.app.tab, self.app.sub = "dashboard", {}
        ctl = await self.app._build_current()
        t = texts(ctl)
        self.assertIn("فواتير اليوم", t)
        self.assertTrue(any("فاتورة " in x for x in t))          # فاتورة اليوم موجودة
        self.assertIn("أبو أحمد", t)
        self.assertFalse([x for x in t if x.startswith("تعذّر عرض")])   # لا قسم فاشل

    async def test_dashboard_has_no_wrap_row_with_expanding_children(self):
        """Row(wrap=True) مع أبناء expand يكسر رسم Flet (السبب المرجّح لفراغ اللوحة)."""
        self.app.tab, self.app.sub = "dashboard", {}
        ctl = await self.app._build_current()
        for c in walk(ctl):
            if getattr(c, "kw", None) and c.kw.get("wrap") is True:
                for ch in c.kw.get("controls", None) or (c.args[0] if c.args and isinstance(c.args[0], list) else []):
                    self.assertFalse(getattr(ch, "kw", {}).get("expand"))


class PartySummaryUI(Smoke):
    """صفحة ملخص العميل/المورد: فواتير وسندات وأرباح بشارات دائرية."""

    async def test_customer_summary_page(self):
        c = self.led.customers[0]
        self.app.tab, self.app.sub = "customers", {"detail": c.id}
        t = texts(await self.app._build_current())
        for needle in ("المبيعات", "الأرباح منه", "المقبوض", "نسبة التحصيل", "أكثر الأصناف شراءً"):
            self.assertIn(needle, t)
        self.assertTrue(any(x.startswith("$") for x in t))                 # الأسعار بالدولار
        self.assertTrue(any(x.startswith("فاتورة ") for x in t))

    async def test_supplier_summary_page(self):
        s = next(x for x in self.led.suppliers if x.name == "مورد 1")
        self.app.tab, self.app.sub = "suppliers", {"detail": s.id}
        t = texts(await self.app._build_current())
        for needle in ("المشتريات", "المدفوع له", "الدين علينا", "نسبة السداد"):
            self.assertIn(needle, t)
        self.assertTrue(any(x.startswith("فاتورة شراء") for x in t))

    async def test_missing_party_is_safe(self):
        self.app.tab, self.app.sub = "customers", {"detail": "nope"}
        t = texts(await self.app._build_current())
        self.assertTrue(any("غير موجود" in x for x in t))

    async def test_lists_have_summary_button(self):
        for tab in ("customers", "suppliers"):
            self.app.tab, self.app.sub = tab, {}
            self.assertIn("ملخص", texts(await self.app._build_current()))
