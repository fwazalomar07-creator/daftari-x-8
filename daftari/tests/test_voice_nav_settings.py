"""تسجيل صوتي مباشر · قائمة منسدلة متوافقة · تنقل سلس."""
import asyncio
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

from . import fake_flet

fake_flet.install()

from daftari.ai import voice as V  # noqa: E402
from daftari.ai.media import sniff_audio_mime  # noqa: E402
from daftari.ui import widgets as w  # noqa: E402


def run(c):
    return asyncio.run(c)


class FakeSD:
    """sounddevice وهمية: تُغذّي الـ callback بعيّنات عالية السعة."""
    loud = True

    class RawInputStream:
        def __init__(self, samplerate, channels, dtype, callback):
            self.cb = callback

        def start(self):
            amp = 8000 if FakeSD.loud else 3
            self.cb(struct.pack("<" + "h" * 400, *([amp, -amp] * 200)), 400, None, None)

        def stop(self): pass
        def close(self): pass


class VoiceTests(unittest.TestCase):
    def setUp(self):
        sys.modules["sounddevice"] = FakeSD
        FakeSD.loud = True
        self.page = fake_flet.Page()
        # الترتيب الجديد: Flet AudioRecorder أولاً ثم sounddevice احتياطياً — نعطّل مكوّن Flet هنا لنختبر الاحتياطي
        self._had_ar = hasattr(sys.modules["flet"], "AudioRecorder")
        sys.modules["flet_audio_recorder"] = None
        sys.modules["flet"].AudioRecorder = None

    def tearDown(self):
        sys.modules.pop("sounddevice", None)
        sys.modules.pop("flet_audio_recorder", None)
        if not self._had_ar and "AudioRecorder" in vars(sys.modules["flet"]):
            del sys.modules["flet"].AudioRecorder

    def test_sounddevice_record_returns_wav(self):
        async def go():
            r = V.VoiceRecorder(self.page, Path(tempfile.mkdtemp()))
            await r.start()
            self.assertTrue(r.recording)
            data, mime = await r.stop()
            return r, data, mime
        r, data, mime = run(go())
        self.assertFalse(r.recording)
        self.assertEqual(mime, "audio/wav")
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(r.backend, "sounddevice")

    def test_silence_is_rejected_with_arabic_message(self):
        FakeSD.loud = False

        async def go():
            r = V.VoiceRecorder(self.page, Path(tempfile.mkdtemp()))
            await r.start()
            await r.stop()
        with self.assertRaises(V.VoiceError) as cm:
            run(go())
        self.assertIn("كلام", str(cm.exception))

    def test_web_mode_never_uses_sounddevice(self):
        self.page.web = True
        self.assertEqual(V.VoiceRecorder(self.page)._order(), ["flet"])

    def test_desktop_order_is_flet_first_then_optional_sounddevice(self):
        self.assertEqual(V.VoiceRecorder(self.page)._order(), ["flet", "sounddevice"])

    def test_no_backend_gives_actionable_error(self):
        sys.modules["sounddevice"] = None          # يجعل import يفشل
        sys.modules["flet_audio_recorder"] = None
        sys.modules["flet"].AudioRecorder = None

        async def go():
            r = V.VoiceRecorder(self.page, Path(tempfile.mkdtemp()))
            await r.start()
        try:
            with self.assertRaises(V.VoiceError) as cm:
                run(go())
            msg = str(cm.exception)
            self.assertIn("flet-audio-recorder", msg)
            self.assertNotIn("pip install sounddevice", msg)           # sounddevice لم يعد مطلوباً ولا يُنصح به
        finally:
            sys.modules.pop("flet_audio_recorder", None)

    def test_cancel_discards(self):
        async def go():
            r = V.VoiceRecorder(self.page, Path(tempfile.mkdtemp()))
            await r.start()
            await r.cancel()
            return r.recording
        self.assertFalse(run(go()))

    def test_sniff_audio_mime_by_content(self):
        self.assertEqual(sniff_audio_mime(b"RIFF\0\0\0\0WAVEfmt "), "audio/wav")
        self.assertEqual(sniff_audio_mime(b"\0\0\0\x20ftypM4A "), "audio/mp4")
        self.assertEqual(sniff_audio_mime(b"OggS" + b"\0" * 10), "audio/ogg")
        self.assertEqual(sniff_audio_mime(b"xxxx", "audio/x"), "audio/x")


class StrictDropdown:
    """يحاكي Dropdown الحديث: يرفض on_change ويقبل on_select فقط."""
    def __init__(self, label=None, value=None, options=None, on_select=None, **kw):
        for k in kw:
            raise TypeError(f"Dropdown.__init__() got an unexpected keyword argument '{k}'")
        self.on_select, self.value, self.options, self.extra = on_select, value, options, kw


class OldDropdown:
    def __init__(self, label=None, value=None, options=None, on_change=None, **kw):
        if "on_select" in kw:
            raise TypeError("got an unexpected keyword argument 'on_select'")
        self.on_change, self.value = on_change, value


class DropdownCompat(unittest.TestCase):
    def test_new_flet_gets_on_select(self):
        got = []
        d = w.safe_ctl(StrictDropdown, label="x", value="a", options=[], on_change=lambda e: got.append(1))
        self.assertTrue(callable(d.on_select))

    def test_old_flet_keeps_on_change(self):
        d = w.safe_ctl(OldDropdown, label="x", value="a", options=[], on_change=lambda e: None)
        self.assertTrue(callable(d.on_change))

    def test_unknown_kwargs_are_dropped(self):
        d = w.safe_ctl(StrictDropdown, label="x", value="a", options=[], on_change=lambda e: None, weird_kw=1)
        self.assertNotIn("weird_kw", d.extra)

    def test_settings_dropdown_uses_picker_value(self):
        picked = []

        class Ev:  # الحدث
            control = type("C", (), {"value": "blue"})()
        d = w.dropdown("ثيم", "green", [("green", "أخضر"), ("blue", "أزرق")], picked.append)
        handler = getattr(d, "on_change", None) or getattr(d, "on_select", None)
        run(handler(Ev()))
        self.assertEqual(picked, ["blue"])


class ScrollNav(unittest.TestCase):
    def test_buttons_scroll_with_duration_and_ignore_overlap(self):
        calls = []

        class Col:
            async def scroll_to(self, **kw):
                calls.append(kw)

        col = Col()
        old = w.SCROLL_MS
        w.SCROLL_MS = 40
        try:
            stack = w.with_scroll_nav(col)
            pad = stack.args[0][1] if stack.args else stack.controls[1]
            buttons = pad.content.controls
            self.assertEqual(len(buttons), 4)

            async def go():
                # ضغطتان متتاليتان بسرعة: الثانية تُتجاهل أثناء الحركة
                await asyncio.gather(buttons[2].on_click(None), buttons[2].on_click(None))
                await buttons[3].on_click(None)
                await buttons[0].on_click(None)
            run(go())
        finally:
            w.SCROLL_MS = old
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["delta"], w.SCROLL_STEP_PX)
        self.assertEqual(calls[1]["offset"], -1)     # آخر القائمة
        self.assertEqual(calls[2]["offset"], 0)      # أول القائمة
        self.assertIn("duration", calls[0])


if __name__ == "__main__":
    unittest.main()


class StoreBarcode(unittest.TestCase):
    def test_falls_back_to_embedded_copy_when_file_missing(self):
        import base64
        from daftari.printing.render import store_barcode_bytes
        raw = b"\xff\xd8\xff\xe0fake-jpeg"
        st = {"storeBarcode": "/no/such/file.jpg", "storeBarcodeB64": base64.b64encode(raw).decode()}
        self.assertEqual(store_barcode_bytes(st), raw)
        self.assertIsNone(store_barcode_bytes({"storeBarcode": "", "storeBarcodeB64": ""}))

    def test_file_wins_over_embedded_copy(self):
        import base64
        from daftari.printing.render import store_barcode_bytes
        f = Path(tempfile.mkdtemp()) / "b.jpg"
        f.write_bytes(b"FILE")
        st = {"storeBarcode": str(f), "storeBarcodeB64": base64.b64encode(b"COPY").decode()}
        self.assertEqual(store_barcode_bytes(st), b"FILE")


class GeminiConnection(unittest.TestCase):
    def test_connection_reset_is_retried_then_succeeds(self):
        from daftari.ai.gemini import GeminiClient, GeminiConfig, GeminiConnectionError
        import json as _json
        calls = {"n": 0}

        def transport(url, headers, body, timeout):
            calls["n"] += 1
            if calls["n"] < 3:
                raise GeminiConnectionError("Remote end closed connection without response")
            ok = {"candidates": [{"content": {"role": "model", "parts": [{"text": "أهلاً"}]}, "finishReason": "STOP"}]}
            return 200, _json.dumps(ok).encode()

        c = GeminiClient(GeminiConfig(api_key="k"), transport=transport, retries=3, backoff=0.0)
        r = run(c.generate([{"role": "user", "parts": [{"text": "مرحبا"}]}]))
        self.assertEqual(r.text, "أهلاً")
        self.assertEqual(calls["n"], 3)

    def test_persistent_failure_gives_actionable_message(self):
        from daftari.ai.gemini import GeminiClient, GeminiConfig, GeminiConnectionError, GeminiError

        def transport(url, headers, body, timeout):
            raise GeminiConnectionError("Remote end closed connection without response")

        c = GeminiClient(GeminiConfig(api_key="k"), transport=transport, retries=1, backoff=0.0)
        with self.assertRaises(GeminiError) as cm:
            run(c.generate([{"role": "user", "parts": [{"text": "x"}]}]))
        self.assertIn("VPN", str(cm.exception))

    def test_check_connection_reports_missing_key_and_bad_model(self):
        import io, json as _json, urllib.request
        from daftari.ai import gemini as G
        ok, msg = G.check_connection("", "m")
        self.assertFalse(ok)

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class Op:
            def open(self, req, timeout=0):
                return Resp(_json.dumps({"models": [{"name": "models/gemini-3.5-flash"},
                                                    {"name": "models/gemini-3.6-flash"}]}).encode())
        old = G._opener
        G._opener = lambda: Op()
        try:
            ok, msg = G.check_connection("k", "gemini-3.5-flash")
            self.assertTrue(ok)
            ok, msg = G.check_connection("k", "no-such-model")
            self.assertFalse(ok)
            self.assertIn("gemini-3.6-flash", msg)
        finally:
            G._opener = old


class ScanViewImports(unittest.TestCase):
    def test_scan_view_has_widgets_imported(self):
        """كان يظهر «name 'w' is not defined» عند فتح مسح فاتورة شراء."""
        from daftari.ui import scan_view
        self.assertTrue(hasattr(scan_view, "w"))


class AndroidRecorderPathTests(unittest.TestCase):
    """خطأ الجهاز الحقيقي: PlatformException(record, …/flet/app/assets/<مسارنا المطلق>… ENOENT). مكوّن Flet على أندرويد يلصق
    مسارنا بعد assets/ فلا يُنشأ الملف. نحاكي ذلك ونتأكد أن التسجيل ينجح بالطرق البديلة وأن الرسائل مناسبة لأندرويد."""

    WAV = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + bytes(range(256)) * 8

    def _android_page(self):
        pg = fake_flet.Page()
        pg.platform = "android"
        return pg

    def _install(self, behaviour: str, assets: Path):
        """behaviour: 'abs_broken' (يرفض المسار المطلق) | 'only_relative' (يقبل نسبياً داخل assets) | 'always_broken'"""
        outer = self
        tmp = Path(tempfile.mkdtemp())

        class Rec:
            def __init__(self, *a, **k):
                self.path = None

            async def has_permission(self):
                return True

            async def start_recording(self, output_path=None):
                if behaviour == "always_broken":
                    raise Exception("PlatformException(record, ENOENT (No such file or directory), null)")
                if output_path is None:
                    if behaviour == "abs_broken":
                        self.path = tmp / "cache-rec.wav"          # ملف مؤقت يختاره النظام
                        self.path.write_bytes(outer.WAV)
                        return True
                    raise Exception("output_path is required")
                if Path(output_path).is_absolute():
                    raise Exception(f"PlatformException(record, {assets}{output_path}: open failed: ENOENT, null)")
                self.path = assets / output_path                   # نسبي: يُكتب داخل assets/
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(outer.WAV)
                return True

            async def stop_recording(self):
                return str(self.path)

        mod = types.ModuleType("flet_audio_recorder")
        mod.AudioRecorder = Rec
        sys.modules["flet_audio_recorder"] = mod
        return Rec

    def tearDown(self):
        sys.modules.pop("flet_audio_recorder", None)

    def _recorder(self, assets: Path):
        r = V.VoiceRecorder(self._android_page(), Path(tempfile.mkdtemp()))
        r._assets_dir = lambda: assets
        return r

    def test_order_has_no_sounddevice_on_android(self):
        self.assertEqual(V.VoiceRecorder(self._android_page())._order(), ["flet"])

    def test_auto_temp_path_works_when_absolute_is_broken(self):
        assets = Path(tempfile.mkdtemp())
        self._install("abs_broken", assets)

        async def go():
            r = self._recorder(assets)
            await r.start()
            self.assertEqual(r._flet_kind, "auto")
            return await r.stop()
        data, mime = run(go())
        self.assertEqual((data[:4], mime), (b"RIFF", "audio/wav"))

    def test_relative_inside_assets_fallback(self):
        assets = Path(tempfile.mkdtemp())
        self._install("only_relative", assets)

        async def go():
            r = self._recorder(assets)
            await r.start()
            self.assertEqual(r._flet_kind, "relative")
            return await r.stop()
        data, _ = run(go())
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(list((assets / "voice").glob("*.wav")), [])      # الملف المؤقت حُذف بعد القراءة

    def test_total_failure_message_is_android_specific(self):
        assets = Path(tempfile.mkdtemp())
        self._install("always_broken", assets)

        async def go():
            await self._recorder(assets).start()
        with self.assertRaises(V.VoiceError) as cm:
            run(go())
        msg = str(cm.exception)
        self.assertIn("الأذونات", msg)
        self.assertNotIn("pip install", msg)                 # لا نصائح ويندوز/pip على الجوال
        self.assertNotIn("sounddevice", msg)
        self.assertIn("ENOENT", msg)                         # تفاصيل الخطأ الحقيقي تبقى ظاهرة للتشخيص

    def test_desktop_still_uses_absolute_path(self):
        assets = Path(tempfile.mkdtemp())
        seen = []

        class Rec:
            async def start_recording(self, output_path=None):
                seen.append(output_path); Path(output_path).write_bytes(AndroidRecorderPathTests.WAV); return True

            async def stop_recording(self):
                return seen[0]
        mod = types.ModuleType("flet_audio_recorder"); mod.AudioRecorder = lambda *a, **k: Rec()
        sys.modules["flet_audio_recorder"] = mod
        sys.modules["sounddevice"] = None
        try:
            async def go():
                r = V.VoiceRecorder(fake_flet.Page(), Path(tempfile.mkdtemp()))
                await r.start()
                return await r.stop()
            data, _ = run(go())
        finally:
            sys.modules.pop("sounddevice", None)
        self.assertTrue(Path(seen[0]).is_absolute())
        self.assertEqual(data[:4], b"RIFF")
