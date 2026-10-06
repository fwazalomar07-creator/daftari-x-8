"""عميل Gemini عبر REST (بدون مكتبات خارجية) مع استدعاء الأدوات (function calling).

- المفتاح والنموذج من .env:  GEMINI_API_KEY · GEMINI_MODEL · GEMINI_THINKING · GEMINI_MAX_OUTPUT_TOKENS
- لا يوجد «نموذج بلا حدود»: الحدود تأتي من حصة حسابك عند Google. هنا نفتح للنموذج أقصى طول للرد
  وأعلى مستوى تفكير، ونعيد المحاولة تلقائياً عند 429/5xx.
- يُعاد محتوى ردّ النموذج كما هو (بما فيه thoughtSignature) إلى السجل، كما تتطلب نماذج Gemini 3.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import http.client
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

DEFAULT_MODEL = "gemini-3.5-flash-lite"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# transport(url, headers, body_bytes, timeout) -> (status, response_bytes)
Transport = Callable[[str, dict, bytes, float], "tuple[int, bytes] | Awaitable[tuple[int, bytes]]"]


def _is_async(fn: Any) -> bool:
    return inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(getattr(fn, "__call__", None))


class GeminiError(Exception):
    """خطأ مفهوم للمستخدم (رسالة عربية)."""


@dataclass
class GeminiConfig:
    api_key: str = ""
    model: str = DEFAULT_MODEL
    thinking: str = "high"            # minimal | low | medium | high ("" = بدون ضبط)
    max_output_tokens: int = 32768
    timeout: float = 180.0
    base_url: str = BASE_URL
    google_search: bool = True        # أداة Gemini المدمجة للبحث في جوجل

    @classmethod
    def from_env(cls) -> "GeminiConfig":
        def num(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, "") or default)
            except ValueError:
                return default
        flag = (os.getenv("GEMINI_GOOGLE_SEARCH", "0") or "0").strip().lower()
        return cls(api_key=(os.getenv("GEMINI_API_KEY") or "").strip(),
                   model=(os.getenv("GEMINI_MODEL") or DEFAULT_MODEL).strip(),
                   thinking=(os.getenv("GEMINI_THINKING", "low") or "").strip().lower(),
                   max_output_tokens=num("GEMINI_MAX_OUTPUT_TOKENS", 32768),
                   google_search=flag in ("1", "true", "yes", "on"))


    @property
    def ready(self) -> bool:
        return bool(self.api_key)


@dataclass
class Reply:
    content: dict[str, Any]                       # يُضاف كما هو لسجل المحادثة
    text: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)   # [{name, args}]
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


class GeminiConnectionError(GeminiError):
    """انقطاع/حجب اتصال (وليس ردّاً من Gemini) — يُعاد المحاولة تلقائياً."""


class RateLimitError(GeminiError):
    """تجاوز حصة/معدل الطلبات (429). retry_after بالثواني إن عُرفت؛ daily=True يعني انتهت الحصة اليومية (لا فائدة من الانتظار)."""

    def __init__(self, message: str, retry_after: float | None = None, daily: bool = False):
        super().__init__(message)
        self.retry_after, self.daily = retry_after, daily


def parse_rate_info(data: Any) -> tuple[float | None, bool, bool]:
    """يستخرج من جسم خطأ 429: (مهلة الانتظار بالثواني، هل الحصة يومية؟، هل حصة النموذج صفر؟)."""
    blob = json.dumps(data, ensure_ascii=False) if data else ""
    retry: float | None = None
    err = (data.get("error") if isinstance(data, dict) else None) or {}
    for d in (err.get("details") or []):
        if isinstance(d, dict) and str(d.get("@type", "")).endswith("RetryInfo"):
            m = re.match(r"\s*([\d.]+)\s*s", str(d.get("retryDelay", "")))
            if m:
                retry = float(m.group(1))
    if retry is None:
        m = re.search(r"retry in ([\d.]+)\s*s", blob, re.I) or re.search(r"try again in ([\d.]+)\s*s", blob, re.I)
        if m:
            retry = float(m.group(1))
    low = blob.lower()
    daily = "perday" in low or "per day" in low or "daily" in low
    zero = "limit: 0" in low
    return retry, daily, zero


_NET_HELP = ("\nالأسباب الشائعة: (1) خدمة Gemini API محجوبة في بلدك أو عند مزوّد الإنترنت → شغّل VPN ثم أعد المحاولة، "
             "(2) جدار حماية/برنامج حماية يقطع الاتصال، (3) إنترنت ضعيف. لتجربة سريعة: اضغط «اختبار الاتصال» في الإعدادات. "
             "ولاستخدام وسيط (بروكسي) ضع GEMINI_PROXY=http://host:port في ملف .env")


def _opener():
    proxy = (os.getenv("GEMINI_PROXY") or "").strip()
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()          # يستخدم إعدادات بروكسي النظام (ويندوز) تلقائياً


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _opener().open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
        raise GeminiConnectionError(f"تعذّر الاتصال بخوادم Gemini: {e}") from e


def check_connection(api_key: str, model: str, base_url: str = BASE_URL, timeout: float = 20.0) -> tuple[bool, str]:
    """يختبر المفتاح والاتصال والنموذج بطلب GET خفيف (لا يستهلك حصة توليد). يرجع (نجح؟، رسالة عربية)."""
    if not (api_key or "").strip():
        return False, "لا يوجد مفتاح — الصقه أولاً."
    req = urllib.request.Request(f"{base_url}/models?pageSize=200", headers={"x-goog-api-key": api_key.strip()})
    try:
        with _opener().open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        return False, GeminiClient._explain(e.code, data)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
        return False, f"لا يصل البرنامج إلى خوادم Gemini: {e}" + _NET_HELP
    names = {(m.get("name") or "").split("/")[-1] for m in data.get("models", [])}
    if model and names and model not in names:
        flash = sorted(n for n in names if "flash" in n and "image" not in n and "tts" not in n and "live" not in n)
        return False, (f"الاتصال والمفتاح سليمان، لكن النموذج «{model}» غير متاح لحسابك. جرّب: "
                       + "، ".join(flash[:6]))
    return True, f"✔ الاتصال سليم والمفتاح مقبول والنموذج «{model}» متاح."


class GeminiClient:
    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, cfg: GeminiConfig, transport: Transport | None = None, retries: int = 3,
                 backoff: float = 1.5):
        self.cfg, self._transport = cfg, transport or _urllib_transport
        self.retries, self.backoff = retries, backoff

    @property
    def ready(self) -> bool:
        return self.cfg.ready

    def _prepare_contents(self, contents: list[dict]) -> list[dict]:
        """سجل المحادثة قد يحوي ردوداً من مزوّد آخر (بعد التبديل التلقائي): نماذج Gemini 3 تشترط توقيع تفكير على استدعاءات الأدوات،
        فنضع التوقيع التخطّي الموثّق حيث يغيب. لا يمسّ الرسائل الأصلية."""
        if "gemini-3" not in (self.cfg.model or ""):
            return contents
        out = []
        for c in contents:
            parts = c.get("parts") or []
            if c.get("role") == "model" and any("functionCall" in p and "thoughtSignature" not in p for p in parts):
                parts = [({**p, "thoughtSignature": "skip_thought_signature_validator"}
                          if "functionCall" in p and "thoughtSignature" not in p else p) for p in parts]
                c = {**c, "parts": parts}
            out.append(c)
        return out

    # ---- بناء الطلب ---------------------------------------------------------------------
    def build_body(self, contents: list[dict], system: str | None, tools: list[dict] | None,
                   with_thinking: bool = True, google_search: bool = False) -> dict[str, Any]:
        gen: dict[str, Any] = {"maxOutputTokens": self.cfg.max_output_tokens}
        if with_thinking and self.cfg.thinking:
            gen["thinkingConfig"] = {"thinkingLevel": self.cfg.thinking}
        body: dict[str, Any] = {"contents": self._prepare_contents(contents), "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        payload: list[dict[str, Any]] = []
        if google_search:
            payload.append({"googleSearch": {}})
        if tools:
            payload.append({"functionDeclarations": tools})
        if payload:
            body["tools"] = payload
        return body

    async def _post(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        url = f"{self.cfg.base_url}/models/{self.cfg.model}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.cfg.api_key}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        for attempt in range(self.retries + 1):
            try:
                if _is_async(self._transport):
                    status, raw = await self._transport(url, headers, payload, self.cfg.timeout)
                else:  # النقل المتزامن (urllib) يُنفَّذ بخيط حتى لا يجمّد الواجهة
                    status, raw = await asyncio.to_thread(self._transport, url, headers, payload, self.cfg.timeout)
            except GeminiConnectionError as e:
                if attempt < self.retries:      # انقطاع عابر (إعادة ضبط اتصال/شبكة ضعيفة): أعد المحاولة
                    await asyncio.sleep(self.backoff * (attempt + 1))
                    continue
                raise GeminiError(f"{e}{_NET_HELP}") from e
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                data = {"error": {"message": raw[:200].decode("utf-8", "replace")}}
            if status in self.RETRY_STATUS and attempt < self.retries:
                if status == 429:
                    retry, daily, zero = parse_rate_info(data)
                    if daily or zero:      # الحصة اليومية/الصفرية لا تُحلّ بالانتظار — أعد الخطأ فوراً بدل حرق طلبات
                        return status, data
                    wait = min(retry, 30.0) if retry else self.backoff * (2 ** attempt)
                    await asyncio.sleep(wait)
                else:
                    await asyncio.sleep(self.backoff * (2 ** attempt))
                continue
            return status, data
        return status, data  # pragma: no cover

    # ---- الاستدعاء -------------------------------------------------------------------------
    async def generate(self, contents: list[dict], system: str | None = None,
                       tools: list[dict] | None = None, google_search: bool | None = None) -> Reply:
        if not self.cfg.ready:
            raise GeminiError("مفتاح Gemini غير موجود — ضعه في ملف .env باسم GEMINI_API_KEY ثم أعد تشغيل البرنامج.")
        use_gs = bool(self.cfg.google_search if google_search is None else google_search) and bool(tools)
        status, data = await self._post(self.build_body(contents, system, tools, google_search=use_gs))
        blob = json.dumps(data).lower()
        if status == 400 and self.cfg.thinking and "thinking" in blob:
            # بعض النماذج لا تقبل thinkingLevel: أعد بدونه بدل الفشل
            status, data = await self._post(self.build_body(contents, system, tools, with_thinking=False,
                                                            google_search=use_gs))
            blob = json.dumps(data).lower()
        if status == 400 and use_gs and ("google" in blob or "tool" in blob):
            # بعض النماذج لا تمزج googleSearch مع functionDeclarations
            status, data = await self._post(self.build_body(contents, system, tools, google_search=False))
        if status != 200:
            if status == 429:
                retry, daily, _ = parse_rate_info(data)
                raise RateLimitError(self._explain(status, data), retry, daily)
            raise GeminiError(self._explain(status, data))
        return self._parse(data)

    GROUNDING_FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-2.0-flash")

    async def grounded_search(self, query: str) -> dict[str, Any]:
        """بحث جوجل الرسمي عبر Gemini كطلب **مستقل** (أداة googleSearch وحدها، بلا functionDeclarations).

        لماذا؟ قراءة صفحات جوجل/DuckDuckGo مباشرة من الهاتف يحجبها المحرك (كابتشا/Enable JavaScript/VPN)، بينما هذا الطلب يمرّ عبر
        نفس اتصال Gemini الذي يعمل أصلاً، ويعيد خلاصة مع مصادرها الحقيقية. يرجع {"answer", "sources":[{title,url}], "queries", "model"}.
        """
        if not self.cfg.ready:
            raise GeminiError("مفتاح Gemini غير موجود")
        q = (query or "").strip()
        if not q:
            raise GeminiError("أدخل عبارة بحث.")
        prompt = (
            f"ابحث في الويب الآن عن: {q}\n"
            "لخّص ما وجدته في المصادر فعلاً بدقة وبالتفصيل: أرقام القطع، الماركة، الأبعاد (الطول/الارتفاع/القطر/العرض)، المادة، الخصائص، "
            "المرجعيات المقابلة عند الماركات الأخرى، السيارات أو المعدات المناسبة، الأسعار إن وُجدت. "
            "انقل الأرقام كما وردت ولا تخمّن أو تُكمل من عندك، وما لم تجده اكتب «غير متوفر». اذكر لكل معلومة اسم الموقع الذي جاءت منه.")
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "tools": [{"googleSearch": {}}],
                "generationConfig": {"maxOutputTokens": 4096, "temperature": 0.2}}
        models = [self.cfg.model] + [m for m in self.GROUNDING_FALLBACK_MODELS if m != self.cfg.model]
        last = ""
        original = self.cfg.model
        try:
            for m in models:
                self.cfg.model = m
                status, data = await self._post(body)
                if status == 200:
                    return self._parse_grounded(data, m)
                last = str(self._explain(status, data))
                if status in (401, 403, 429):          # مفتاح/حصة: تغيير النموذج لن يحلّها
                    break
        finally:
            self.cfg.model = original
        raise GeminiError(last or "تعذّر البحث عبر Gemini")

    @staticmethod
    def _parse_grounded(data: dict[str, Any], model: str) -> dict[str, Any]:
        cand = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [] if not p.get("thought")).strip()
        meta = cand.get("groundingMetadata") or {}
        sources, seen = [], set()
        for ch in meta.get("groundingChunks") or []:
            web = ch.get("web") or {}
            url = web.get("uri") or ""
            if url and url not in seen:
                seen.add(url)
                sources.append({"title": web.get("title") or url, "url": url})
        if not text:
            raise GeminiError("لم يُرجع بحث Gemini أي نتيجة")
        return {"answer": text, "sources": sources[:10], "queries": meta.get("webSearchQueries") or [], "model": model}

    async def transcribe_audio(self, data: bytes, mime: str = "audio/wav") -> str:
        """ينسخ كلاماً مسجّلاً إلى نص عبر Gemini (رؤية/صوت متعددة الوسائط)."""
        from .media import inline_audio_part
        if not data:
            raise GeminiError("لا يوجد تسجيل صوتي.")
        contents = [{"role": "user", "parts": [
            inline_audio_part(data, mime),
            {"text": "انسخ الكلام في هذا التسجيل حرفياً. إن كان عربياً أبقه عربياً. أرجع النص فقط بلا شرح أو علامات اقتباس."},
        ]}]
        reply = await self.generate(contents, google_search=False)
        text = (reply.text or "").strip().strip("«»\"'")
        if not text:
            raise GeminiError("لم أستطع تمييز كلام واضح في التسجيل — أعد المحاولة في مكان هادئ.")
        return text

    @staticmethod
    def _explain(status: int, data: dict[str, Any]) -> str:
        msg = (data.get("error") or {}).get("message", "") if isinstance(data, dict) else ""
        if status in (401, 403):
            return f"مفتاح Gemini مرفوض ({status}). تأكد من GEMINI_API_KEY وأن الـ API مفعّل لحسابك. {msg[:120]}"
        if status == 404:
            return f"النموذج غير موجود — راجع GEMINI_MODEL في .env. {msg[:120]}"
        if status == 429:
            retry, daily, zero = parse_rate_info(data)
            if zero:
                why = "حصة هذا النموذج صفر على طبقتك الحالية (غالباً الطبقة المجانية لا تشمله) — جرّب نموذجاً آخر أو فعّل الفوترة."
            elif daily:
                why = "انتهت حصة Gemini اليومية."
            elif retry:
                why = f"تجاوزت حد الطلبات في الدقيقة لـ Gemini (يتجدد خلال ~{int(retry) + 1} ثانية)."
            else:
                why = "تجاوزت حصة Gemini (حد الطلبات أو الرموز)."
            return (why + " ملاحظة: اشتراك Gemini في التطبيق/الموقع (Google AI Pro) لا يعطي حصة للـ API؛ مفتاح AI Studio "
                    "بلا فوترة يبقى على الطبقة المجانية المحدودة. فعّل الفوترة للمشروع، أو أضف مزوداً بديلاً من الإعدادات.")
        return f"خطأ من Gemini ({status}): {msg[:200]}"

    @staticmethod
    def _parse(data: dict[str, Any]) -> Reply:
        cands = data.get("candidates") or []
        if not cands:
            block = (data.get("promptFeedback") or {}).get("blockReason")
            raise GeminiError("حُجب الطلب من Gemini" + (f" ({block})" if block else "") + " — أعد صياغة السؤال.")
        cand = cands[0]
        content = cand.get("content") or {"role": "model", "parts": []}
        content.setdefault("role", "model")
        parts = content.get("parts") or []
        text = "".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought"))
        calls = [{"name": p["functionCall"]["name"], "args": p["functionCall"].get("args") or {}}
                 for p in parts if "functionCall" in p]
        return Reply(content=content, text=text.strip(), calls=calls,
                     finish_reason=cand.get("finishReason", ""), usage=data.get("usageMetadata") or {})
