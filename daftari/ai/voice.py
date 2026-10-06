"""تسجيل صوتي مباشر من الميكروفون (زر الفويس في المساعد) — بلا رفع ملفات.

يجرّب خلفيات متعددة بالترتيب حتى تنجح واحدة:
  1) sounddevice  — تسجيل مباشر داخل بايثون (أضمن على ويندوز/ماك/لينكس؛ pip install sounddevice)
  2) flet-audio-recorder — مكوّن Flet الرسمي (pip install flet-audio-recorder)
في وضع المتصفح (web) تُستخدم خلفية Flet فقط لأن sounddevice سيسجّل من ميكروفون الخادم لا المستخدم.
"""
from __future__ import annotations

import array
import asyncio
import importlib
import importlib.util
import inspect
import tempfile
import sys
import time
from pathlib import Path
from typing import Any

from .media import pcm16_to_wav, sniff_audio_mime

SAMPLE_RATE = 16000          # كافٍ للكلام وصغير الحجم
MAX_SECONDS = 120            # إيقاف تلقائي حفاظاً على حجم الطلب
MIN_PEAK = 150               # أقل سعة تُعدّ «صوتاً» (من 32767)



def _has_module(name: str) -> bool:
    """هل المكتبة متاحة؟ (sys.modules أولاً لأن بعض الوحدات المحقونة بلا __spec__)."""
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class VoiceError(Exception):
    """رسالة عربية جاهزة للعرض للمستخدم."""


async def _call(fn, *a, **kw):
    if fn is None:
        return None
    r = fn(*a, **kw)
    return await r if inspect.isawaitable(r) else r


class VoiceRecorder:
    def __init__(self, page: Any, tmp_dir: Path | None = None):
        self.page = page
        self.dir = Path(tmp_dir or Path(tempfile.gettempdir()) / "daftari-voice")
        self.backend: str | None = None
        self.recording = False
        self._t0 = 0.0
        # sounddevice
        self._stream = None
        self._frames: list[bytes] = []
        # flet
        self._flet_rec = None
        self._flet_path: str | None = None
        self._flet_kind = "absolute"

    # ---- حالة ----
    @property
    def elapsed(self) -> int:
        return int(time.monotonic() - self._t0) if self.recording else 0

    def _mobile(self) -> bool:
        """أندرويد/iOS: لا sounddevice هنا (يحتاج PortAudio) — التسجيل عبر مكوّن Flet فقط."""
        plat = str(getattr(self.page, "platform", "") or "").lower()
        return plat.endswith(("android", "ios"))

    def _order(self) -> list[str]:
        # التسجيل الموحّد على كل المنصات: مكوّن AudioRecorder في Flet. sounddevice صار احتياطياً أخيراً لسطح المكتب فقط
        # (غير مطلوب ولا مثبّت في البناء) — لا يُجرَّب إلا إذا فشل مكوّن Flet وكانت المكتبة موجودة أصلاً.
        if getattr(self.page, "web", False) or self._mobile():
            return ["flet"]
        return ["flet", "sounddevice"]

    # ---- بدء ----
    async def start(self) -> None:
        if self.recording:
            return
        errors: list[str] = []
        for name in self._order():
            if name == "sounddevice" and not _has_module("sounddevice"):
                continue                      # احتياطي اختياري غير مثبّت: لا نزعج المستخدم برسالة عنه
            try:
                await (self._start_sd() if name == "sounddevice" else self._start_flet())
            except VoiceError as e:
                errors.append(str(e))
                continue
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {e}")
                continue
            self.backend, self.recording, self._t0 = name, True, time.monotonic()
            return
        if self._mobile():
            raise VoiceError("تعذّر تشغيل الميكروفون.\n• " + "\n• ".join(errors) +
                             "\nتأكد من السماح للتطبيق بالميكروفون: إعدادات الهاتف ← التطبيقات ← دفتري ← الأذونات ← الميكروفون.")
        raise VoiceError("تعذّر تشغيل الميكروفون.\n• " + "\n• ".join(errors) +
                         "\nتأكد من السماح للتطبيقات بالميكروفون (إعدادات ويندوز ← الخصوصية ← الميكروفون) وأن مكوّن التسجيل flet-audio-recorder مثبّت.")

    async def _start_sd(self) -> None:
        try:
            sd = importlib.import_module("sounddevice")
        except ImportError as e:
            raise VoiceError("مكتبة sounddevice غير مثبتة") from e
        self._frames = []
        frames = self._frames

        def cb(indata, _n, _t, _status):
            frames.append(bytes(indata))

        stream = await asyncio.to_thread(
            lambda: sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=cb))
        await asyncio.to_thread(stream.start)
        self._stream = stream

    async def _start_flet(self) -> None:
        mod = None
        try:
            mod = importlib.import_module("flet_audio_recorder")
        except ImportError:
            pass
        if mod is None:
            import flet as ft
            cls = getattr(ft, "AudioRecorder", None)
            if cls is None:
                raise VoiceError("حزمة flet-audio-recorder غير مثبتة")
            mod_cls, cfg_cls, enc = cls, None, None
        else:
            mod_cls = getattr(mod, "AudioRecorder")
            cfg_cls, enc = getattr(mod, "AudioRecorderConfiguration", None), getattr(mod, "AudioEncoder", None)
        rec = self._flet_rec
        if rec is None:
            kw = {}
            if cfg_cls is not None and enc is not None and hasattr(enc, "WAV"):
                try:
                    kw["configuration"] = cfg_cls(encoder=enc.WAV)
                except Exception:  # noqa: BLE001
                    kw = {}
            try:
                rec = mod_cls(**kw)
            except TypeError:
                rec = mod_cls()
            self.page.services.append(rec)
            try:
                self.page.update()
            except Exception:  # noqa: BLE001
                pass
            self._flet_rec = rec
        if await _call(getattr(rec, "has_permission", None)) is False:
            raise VoiceError("لا توجد صلاحية للميكروفون")
        await self._begin_flet_recording(rec)

    def _assets_dir(self) -> Path | None:
        """مجلد assets داخل التطبيق: مكوّن التسجيل على أندرويد يُلحق به أي مسار نعطيه (…/files/flet/app/assets/<المسار>)."""
        for base in (Path(__file__).resolve().parents[2], Path(__file__).resolve().parents[1], Path.cwd()):
            d = base / "assets"
            if d.is_dir():
                return d
        return None

    async def _begin_flet_recording(self, rec) -> None:
        """يجرّب عدة طرق لمسار ملف التسجيل بالترتيب حتى تنجح واحدة.

        سبب خطأ ENOENT على أندرويد: مكوّن Flet كان يلصق مسارنا المطلق بعد …/flet/app/assets/ فيصير مساراً غير موجود.
        فنجرّب: (1) بلا مسار (ملف مؤقت يختاره النظام) ← (2) اسم نسبي داخل assets/ ← (3) المسار المطلق (ويندوز/ماك/لينكس).
        """
        stamp = int(time.time() * 1000)
        self.dir.mkdir(parents=True, exist_ok=True)
        absolute = str(self.dir / f"rec-{stamp}.wav")
        attempts: list[tuple[str, str | None]] = []
        if self._mobile():
            attempts.append(("auto", None))
            assets = self._assets_dir()
            if assets is not None:
                try:
                    (assets / "voice").mkdir(parents=True, exist_ok=True)
                    attempts.append(("relative", f"voice/rec-{stamp}.wav"))
                except OSError:
                    pass
            attempts.append(("absolute", absolute))
        else:
            attempts.append(("absolute", absolute))
        errs: list[str] = []
        for kind, path in attempts:
            try:
                if path is None:
                    ok = await _call(rec.start_recording)
                else:
                    try:
                        ok = await _call(rec.start_recording, output_path=path)
                    except TypeError:
                        ok = await _call(rec.start_recording, path)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{kind}: {e}")
                continue
            if ok is False:
                errs.append(f"{kind}: رفض النظام بدء التسجيل")
                continue
            self._flet_path = path
            self._flet_kind = kind
            return
        raise VoiceError("flet: " + " | ".join(errs)[:400])

    # ---- إيقاف ----
    async def stop(self) -> tuple[bytes, str]:
        """يوقف التسجيل ويرجع (بايتات الصوت، نوع MIME)."""
        if not self.recording:
            raise VoiceError("لا يوجد تسجيل جارٍ")
        backend, self.recording = self.backend, False
        if backend == "sounddevice":
            return await self._stop_sd()
        return await self._stop_flet()

    async def _stop_sd(self) -> tuple[bytes, str]:
        stream, self._stream = self._stream, None
        try:
            if stream is not None:
                await asyncio.to_thread(stream.stop)
                await asyncio.to_thread(stream.close)
        finally:
            pcm = b"".join(self._frames)
            self._frames = []
        if not pcm:
            raise VoiceError("لم يُلتقط أي صوت — تحقق من الميكروفون.")
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        if not samples or max(abs(x) for x in samples) < MIN_PEAK:
            raise VoiceError("لم يُلتقط كلام واضح — اقترب من الميكروفون وتحدّث ثم أوقف التسجيل.")
        return pcm16_to_wav(pcm, SAMPLE_RATE), "audio/wav"

    def _candidates(self, returned: str) -> list[Path]:
        out: list[Path] = []
        assets = self._assets_dir()
        for raw in (returned, self._flet_path or ""):
            if not raw:
                continue
            p = Path(raw)
            out.append(p)
            if not p.is_absolute() and assets is not None:
                out.append(assets / raw)
            if assets is not None:
                out.append(assets / raw.lstrip("/"))          # المسار المُلصَق بعد assets/ (سلوك مكوّن أندرويد)
        seen, uniq = set(), []
        for c in out:
            if str(c) not in seen:
                seen.add(str(c)); uniq.append(c)
        return uniq

    async def _stop_flet(self) -> tuple[bytes, str]:
        rec = self._flet_rec
        res = await _call(getattr(rec, "stop_recording", None)) if rec is not None else None
        returned = str(res or "")
        if returned.startswith(("blob:", "http")) or (not returned and not self._flet_path):
            raise VoiceError("التسجيل من المتصفح غير مدعوم هنا — شغّل البرنامج كتطبيق سطح مكتب.")
        cands = self._candidates(returned)
        data, used = b"", None
        for _ in range(12):                      # ننتظر اكتمال كتابة الملف
            for c in cands:
                try:
                    d = c.read_bytes()
                except OSError:
                    continue
                if len(d) > len(data):
                    data, used = d, c
            if len(data) > 1000:
                break
            await asyncio.sleep(0.15)
        if used is not None:
            try:
                used.unlink(missing_ok=True)
            except OSError:
                pass
        if len(data) <= 1000:
            raise VoiceError("لم يُلتقط صوت — تحقق من صلاحية الميكروفون.")
        return data, sniff_audio_mime(data, "audio/wav")

    async def cancel(self) -> None:
        """يلغي التسجيل ويتجاهل الصوت."""
        if not self.recording:
            return
        try:
            await self.stop()
        except Exception:  # noqa: BLE001
            self.recording = False
