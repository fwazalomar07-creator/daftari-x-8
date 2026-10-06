"""طبقة نماذج متعددة المزوّدين — البرنامج لا يرتبط بنموذج واحد.

المزوّدون المدعومون (كلهم بلا مكتبات خارجية):
  • gemini      — Google Gemini (الأصلي)
  • openai      — أي خدمة بواجهة OpenAI Chat Completions: OpenAI, OpenRouter, Groq, DeepSeek, Mistral, Together,
                  xAI, وكذلك النماذج المحلية Ollama / LM Studio / vLLM … (بتغيير base_url فقط)
  • anthropic   — Claude

السجل الداخلي للمحادثة يبقى بصيغة Gemini (contents/parts/functionCall/functionResponse)، وكل مزوّد يحوّله إلى صيغته؛
لذلك يمكن التبديل بين المزوّدين حتى في منتصف سؤال فيه أدوات.

MultiClient: يجرّب المزوّد الأساسي، وعند 429 (انتهت الحصة) أو انقطاع أو خطأ مزوّد ينتقل تلقائياً للتالي، ويضع المزوّد الفاشل
في «فترة راحة» حتى لا تُحرق طلبات عليه.
"""
from __future__ import annotations

import asyncio
import base64
import http.client
import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .gemini import (BASE_URL as GEMINI_BASE, DEFAULT_MODEL as GEMINI_DEFAULT_MODEL, GeminiClient, GeminiConfig,
                     GeminiConnectionError, GeminiError, RateLimitError, Reply, _is_async, _opener,
                     _urllib_transport, check_connection as gemini_check)

# --------------------------------------------------------------------------------- الإعدادات المسبقة
# id: (الاسم الظاهر، النوع، base_url، النموذج الافتراضي، يحتاج مفتاحاً؟، اسم متغير البيئة للمفتاح)
PRESETS: dict[str, dict[str, Any]] = {
    "gemini": dict(label="Google Gemini", kind="gemini", base_url=GEMINI_BASE, model=GEMINI_DEFAULT_MODEL,
                   needs_key=True, env="GEMINI_API_KEY"),
    "openai": dict(label="OpenAI (ChatGPT)", kind="openai", base_url="https://api.openai.com/v1", model="gpt-4o-mini",
                   needs_key=True, env="OPENAI_API_KEY"),
    "anthropic": dict(label="Anthropic Claude", kind="anthropic", base_url="https://api.anthropic.com/v1",
                      model="claude-haiku-4-5-20251001", needs_key=True, env="ANTHROPIC_API_KEY"),
    "openrouter": dict(label="OpenRouter (مئات النماذج بمفتاح واحد)", kind="openai", base_url="https://openrouter.ai/api/v1",
                       model="openai/gpt-4o-mini", needs_key=True, env="OPENROUTER_API_KEY"),
    "groq": dict(label="Groq (سريع، حصة مجانية سخية)", kind="openai", base_url="https://api.groq.com/openai/v1",
                 model="openai/gpt-oss-120b", needs_key=True, env="GROQ_API_KEY"),
    "deepseek": dict(label="DeepSeek", kind="openai", base_url="https://api.deepseek.com/v1", model="deepseek-chat",
                     needs_key=True, env="DEEPSEEK_API_KEY"),
    "mistral": dict(label="Mistral", kind="openai", base_url="https://api.mistral.ai/v1", model="mistral-small-latest",
                    needs_key=True, env="MISTRAL_API_KEY"),
    "ollama": dict(label="Ollama (محلي على جهازك — مجاني)", kind="openai", base_url="http://localhost:11434/v1",
                   model="qwen2.5:7b", needs_key=False, env=""),
    "lmstudio": dict(label="LM Studio (محلي)", kind="openai", base_url="http://localhost:1234/v1", model="local-model",
                     needs_key=False, env=""),
    "custom": dict(label="مخصّص (أي خدمة بواجهة OpenAI)", kind="openai", base_url="", model="", needs_key=False, env=""),
}
PRESET_ORDER = list(PRESETS)

# نماذج النسخ الصوتي (/audio/transcriptions) للمزوّدين الذين يدعمونها بواجهة OpenAI
TRANSCRIBE_MODELS = {"openai": "whisper-1", "groq": "whisper-large-v3-turbo"}


@dataclass
class ProviderSpec:
    id: str                       # معرّف فريد (اسم الإعداد المسبق أو اسم مخصص)
    kind: str = "openai"          # gemini | openai | anthropic
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    enabled: bool = True
    label: str = ""

    @property
    def preset(self) -> dict[str, Any]:
        return PRESETS.get(self.id) or PRESETS["custom"]

    @property
    def name(self) -> str:
        return self.label or self.preset["label"]

    @property
    def ready(self) -> bool:
        if not self.enabled or not self.model:
            return False
        if self.kind == "gemini":
            return bool(self.api_key)
        if not self.base_url:
            return False
        needs_key = self.preset["needs_key"] if self.id in PRESETS else False
        return bool(self.api_key) or not needs_key

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "api_key": self.api_key, "model": self.model,
                "base_url": self.base_url, "enabled": self.enabled, "label": self.label}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProviderSpec":
        pid = str(d.get("id") or "custom")
        pre = PRESETS.get(pid) or PRESETS["custom"]
        return cls(id=pid, kind=str(d.get("kind") or pre["kind"]), api_key=str(d.get("api_key") or "").strip(),
                   model=str(d.get("model") or pre["model"]).strip(),
                   base_url=str(d.get("base_url") or pre["base_url"]).strip().rstrip("/"),
                   enabled=bool(d.get("enabled", True)), label=str(d.get("label") or ""))


def load_specs(settings: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> list[ProviderSpec]:
    """يبني قائمة المزوّدين مرتّبة: من إعدادات التطبيق (llmProviders / llmOrder) + متغيرات البيئة.
    Gemini يُقرأ دائماً من GEMINI_API_KEY / GEMINI_MODEL (التي يضبطها التطبيق من الإعدادات القديمة) لإبقاء التوافق."""
    env = os.environ if env is None else env
    settings = settings or {}
    by_id: dict[str, ProviderSpec] = {}
    for d in settings.get("llmProviders") or []:
        if isinstance(d, dict) and d.get("id"):
            sp = ProviderSpec.from_dict(d)
            by_id[sp.id] = sp
    # متغيرات البيئة: مزوّد جاهز لكل مفتاح موجود (لا تغلب ما في الإعدادات)
    for pid, pre in PRESETS.items():
        key = (env.get(pre["env"]) or "").strip() if pre["env"] else ""
        model = (env.get(f"{pid.upper()}_MODEL") or "").strip()
        base = (env.get(f"{pid.upper()}_BASE_URL") or "").strip().rstrip("/")
        if pid == "gemini":
            sp = by_id.get("gemini") or ProviderSpec("gemini", "gemini", model=pre["model"], base_url=pre["base_url"])
            sp.api_key = key or sp.api_key
            sp.model = model or sp.model or pre["model"]
            by_id["gemini"] = sp
        elif key and pid not in by_id:
            by_id[pid] = ProviderSpec(pid, pre["kind"], key, model or pre["model"], base or pre["base_url"])
        elif pid in by_id:
            sp = by_id[pid]
            sp.api_key = sp.api_key or key
            sp.model = sp.model or model or pre["model"]
            sp.base_url = sp.base_url or base or pre["base_url"]
    for sp in by_id.values():
        if not sp.base_url:
            sp.base_url = sp.preset["base_url"]
        if not sp.model:
            sp.model = sp.preset["model"]
    order = [x.strip() for x in (settings.get("llmOrder") or (env.get("LLM_ORDER") or "").split(",")) if str(x).strip()]
    ranked = [i for i in order if i in by_id] + [i for i in by_id if i not in order]
    return [by_id[i] for i in ranked]


def specs_to_settings(specs: list[ProviderSpec]) -> dict[str, Any]:
    # Gemini يبقى بمفتاحه القديم geminiApiKey/geminiModel أيضاً، فنكتب الباقي فقط هنا
    return {"llmProviders": [s.to_dict() for s in specs if s.id != "gemini"],
            "llmOrder": [s.id for s in specs]}


# --------------------------------------------------------------------------------- HTTP مشترك
_RETRY = {429, 500, 502, 503, 504}


def _err_message(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    e = data.get("error")
    if isinstance(e, dict):
        return str(e.get("message") or e.get("type") or "")[:300]
    return str(e or data.get("message") or "")[:300]


def _retry_after(data: Any) -> float | None:
    m = re.search(r"(?:try again|retry) in ([\d.]+)\s*(ms|s|m)", json.dumps(data or "", ensure_ascii=False), re.I)
    if not m:
        return None
    v = float(m.group(1))
    return v / 1000 if m.group(2).lower() == "ms" else v * (60 if m.group(2).lower() == "m" else 1)


class _BaseClient:
    """نقل + إعادة محاولة مشتركة. الأصناف المشتقة تبني الجسم وتحلّل الرد."""
    RETRY_STATUS = _RETRY

    def __init__(self, spec: ProviderSpec, transport: Callable | None = None, retries: int = 2, backoff: float = 1.5,
                 timeout: float = 120.0):
        self.spec, self._transport = spec, transport or _urllib_transport
        self.retries, self.backoff, self.timeout = retries, backoff, timeout

    @property
    def ready(self) -> bool:
        return self.spec.ready

    @property
    def cfg(self) -> ProviderSpec:        # توافق مع الكود الذي يقرأ client.cfg.model
        return self.spec

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _url(self) -> str:
        raise NotImplementedError

    async def _post(self, body: dict[str, Any]) -> tuple[int, Any]:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        status, data = 0, {}
        for attempt in range(self.retries + 1):
            try:
                if _is_async(self._transport):
                    status, raw = await self._transport(self._url(), self._headers(), payload, self.timeout)
                else:
                    status, raw = await asyncio.to_thread(self._transport, self._url(), self._headers(), payload, self.timeout)
            except GeminiConnectionError as e:
                if attempt < self.retries:
                    await asyncio.sleep(self.backoff * (attempt + 1))
                    continue
                raise GeminiError(f"تعذّر الاتصال بـ {self.spec.name}: {e}") from e
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                data = {"error": {"message": raw[:200].decode("utf-8", "replace")}}
            if status in self.RETRY_STATUS and attempt < self.retries:
                if status == 429:
                    ra = _retry_after(data)
                    if ra and ra > 30:      # انتظار طويل → لا فائدة، دع الموجّه يبدّل المزوّد
                        return status, data
                    await asyncio.sleep(min(ra, 30.0) if ra else self.backoff * (2 ** attempt))
                else:
                    await asyncio.sleep(self.backoff * (2 ** attempt))
                continue
            return status, data
        return status, data  # pragma: no cover

    def _explain(self, status: int, data: Any) -> GeminiError:
        msg, who = _err_message(data), self.spec.name
        if status in (401, 403):
            return GeminiError(f"مفتاح {who} مرفوض ({status}). راجع المفتاح في الإعدادات. {msg[:120]}")
        if status == 404:
            return GeminiError(f"{who}: النموذج «{self.spec.model}» أو العنوان غير موجود — راجع اسم النموذج/الرابط. {msg[:120]}")
        if status == 429:
            return RateLimitError(f"{who}: تجاوزت الحصة أو معدل الطلبات. {msg[:150]}", _retry_after(data))
        if status == 400 and "tool" in msg.lower():
            return GeminiError(f"{who}: النموذج «{self.spec.model}» لا يدعم الأدوات (function calling) — اختر نموذجاً يدعمها. {msg[:120]}")
        return GeminiError(f"خطأ من {who} ({status}): {msg[:200]}")

    async def transcribe_audio(self, data: bytes, mime: str = "audio/wav") -> str:
        model = TRANSCRIBE_MODELS.get(self.spec.id)
        if self.spec.kind != "openai" or not model:
            raise GeminiError(f"نسخ الصوت لا يدعمه المزوّد الحالي ({self.spec.name}).")
        if not data:
            raise GeminiError("لا يوجد تسجيل صوتي.")
        ext = {"audio/wav": "wav", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac", "audio/ogg": "ogg",
               "audio/webm": "webm", "audio/flac": "flac"}.get((mime or "").lower(), "wav")
        boundary = "----daftari" + os.urandom(8).hex()
        crlf = b"\r\n"
        parts: list[bytes] = []
        for name, val in (("model", model), ("response_format", "json"), ("language", "ar")):
            parts += [f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{name}"'.encode(), b"", val.encode()]
        parts += [f"--{boundary}".encode(),
                  f'Content-Disposition: form-data; name="file"; filename="voice.{ext}"'.encode(),
                  f"Content-Type: {mime or 'audio/wav'}".encode(), b"", data, f"--{boundary}--".encode(), b""]
        payload = crlf.join(parts)
        headers = {**self._headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"}
        url = f"{self.spec.base_url.rstrip('/')}/audio/transcriptions"
        try:
            if _is_async(self._transport):
                status, raw = await self._transport(url, headers, payload, self.timeout)
            else:
                status, raw = await asyncio.to_thread(self._transport, url, headers, payload, self.timeout)
        except GeminiConnectionError as e:
            raise GeminiError(f"تعذّر الاتصال بـ {self.spec.name}: {e}") from e
        try:
            data_j = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            data_j = {"error": {"message": raw[:200].decode("utf-8", "replace")}}
        if status != 200:
            raise self._explain(status, data_j)
        text = str(data_j.get("text") or "").strip()
        if not text:
            raise GeminiError("لم يُلتقط كلام واضح في التسجيل.")
        return text


# --------------------------------------------------------------------------------- تحويل الصيغ
def _lower_schema(node: Any) -> Any:
    """مخطط Gemini (OBJECT/STRING…) → JSON Schema قياسي."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, str):
                out[k] = v.lower()
            else:
                out[k] = _lower_schema(v)
        return out
    if isinstance(node, list):
        return [_lower_schema(x) for x in node]
    return node


def _tool_schema(decl: dict[str, Any]) -> dict[str, Any]:
    return _lower_schema(decl.get("parameters") or {"type": "OBJECT", "properties": {}})


def _text_of(parts: list[dict]) -> str:
    return "".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought"))


# --------------------------------------------------------------------------------- OpenAI-compatible
# أدوات أساسية فقط للمزوّدين ذوي حد الرموز الصغير بالدقيقة (Groq المجاني ≈ 6000 رمز/دقيقة): كل الأدوات تأكل وحدها أغلبه.
LITE_TOOLS = {"search_items", "search_catalogs", "open_catalog", "read_webpage", "attach_catalog_image", "fill_missing_images", "business_summary", "list_low_stock", "list_customers", "list_invoices",
              "get_invoice", "get_customer_statement", "best_sellers", "profit_overview", "google_search",
              # تصدير Excel وإرسال الصور: كانت غائبة فيقول النموذج إنه صدّر ملفاً دون أن توجد أداة تنفّذ ذلك فعلاً
              "export_dataset", "export_excel", "show_item_images",
              "clean_image_background", "read_excel_rows", "import_excel_items", "attach_user_image"}


class OpenAIClient(_BaseClient):
    @property
    def lite(self) -> bool:
        return "groq.com" in (self.spec.base_url or "")

    def _url(self) -> str:
        return f"{self.spec.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.spec.api_key:
            h["Authorization"] = f"Bearer {self.spec.api_key}"
        if "openrouter" in self.spec.base_url:
            h["X-Title"] = "Daftari"
        return h

    @staticmethod
    def convert_messages(contents: list[dict], system: str | None) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        counter = 0
        pending: list[tuple[str, str]] = []       # (id, name) لاستدعاءات النموذج الأخيرة بانتظار نتائجها
        for c in contents:
            parts = c.get("parts") or []
            if c.get("role") == "model":
                calls = []
                pending = []
                for p in parts:
                    fc = p.get("functionCall")
                    if fc:
                        counter += 1
                        cid = f"call_{counter}"
                        pending.append((cid, fc["name"]))
                        calls.append({"id": cid, "type": "function",
                                      "function": {"name": fc["name"], "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False)}})
                m: dict[str, Any] = {"role": "assistant", "content": _text_of(parts) or None}
                if calls:
                    m["tool_calls"] = calls
                if m["content"] or calls:
                    msgs.append(m)
                continue
            # دور المستخدم: نتائج أدوات و/أو نص وصور
            content_parts: list[dict[str, Any]] = []
            for p in parts:
                fr = p.get("functionResponse")
                if fr:
                    pos = next((i for i, (_, n) in enumerate(pending) if n == fr.get("name")), 0 if pending else None)
                    cid = pending.pop(pos)[0] if pos is not None else f"call_{counter}"
                    msgs.append({"role": "tool", "tool_call_id": cid,
                                 "content": json.dumps(fr.get("response"), ensure_ascii=False)})
                elif p.get("text"):
                    content_parts.append({"type": "text", "text": p["text"]})
                elif p.get("inlineData"):
                    d = p["inlineData"]
                    if str(d.get("mimeType", "")).startswith("image/"):
                        content_parts.append({"type": "image_url",
                                              "image_url": {"url": f"data:{d['mimeType']};base64,{d['data']}"}})
            if content_parts:
                if all(x["type"] == "text" for x in content_parts):
                    msgs.append({"role": "user", "content": "\n".join(x["text"] for x in content_parts)})
                else:
                    msgs.append({"role": "user", "content": content_parts})
        return msgs

    def build_body(self, contents: list[dict], system: str | None, tools: list[dict] | None) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.spec.model, "messages": self.convert_messages(contents, system)}
        if tools and self.lite:
            tools = [t for t in tools if t["name"] in LITE_TOOLS]
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                               "parameters": _tool_schema(t)}} for t in tools]
        return body

    async def generate(self, contents: list[dict], system: str | None = None, tools: list[dict] | None = None,
                       google_search: bool | None = None) -> Reply:
        if not self.ready:
            raise GeminiError(f"{self.spec.name}: المفتاح أو النموذج أو الرابط غير مضبوط في الإعدادات.")
        status, data = await self._post(self.build_body(contents, system, tools))
        if status != 200:
            raise self._explain(status, data)
        choices = (data or {}).get("choices") or []
        if not choices:
            raise GeminiError(f"{self.spec.name}: ردّ فارغ — {_err_message(data) or 'أعد المحاولة'}")
        ch = choices[0]
        msg = ch.get("message") or {}
        text = msg.get("content") or ""
        if isinstance(text, list):
            text = "".join(x.get("text", "") for x in text if isinstance(x, dict))
        parts: list[dict[str, Any]] = [{"text": text}] if text else []
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            calls.append({"name": fn.get("name", ""), "args": args})
            parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
        return Reply(content={"role": "model", "parts": parts}, text=text.strip(), calls=calls,
                     finish_reason=str(ch.get("finish_reason") or ""), usage=data.get("usage") or {})


# --------------------------------------------------------------------------------- Anthropic
class AnthropicClient(_BaseClient):
    VERSION = "2023-06-01"

    def __init__(self, *a, max_tokens: int = 8192, **kw):
        super().__init__(*a, **kw)
        self.max_tokens = max_tokens

    def _url(self) -> str:
        return f"{self.spec.base_url}/messages"

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "x-api-key": self.spec.api_key, "anthropic-version": self.VERSION}

    @staticmethod
    def convert_messages(contents: list[dict]) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        counter = 0
        pending: list[tuple[str, str]] = []

        def push(role: str, blocks: list[dict]) -> None:
            if not blocks:
                return
            if msgs and msgs[-1]["role"] == role:      # Anthropic يشترط تناوب الأدوار — ندمج المتتالية
                msgs[-1]["content"].extend(blocks)
            else:
                msgs.append({"role": role, "content": blocks})

        for c in contents:
            parts = c.get("parts") or []
            if c.get("role") == "model":
                pending = []
                blocks: list[dict] = []
                txt = _text_of(parts)
                if txt:
                    blocks.append({"type": "text", "text": txt})
                for p in parts:
                    fc = p.get("functionCall")
                    if fc:
                        counter += 1
                        tid = f"toolu_{counter:04d}"
                        pending.append((tid, fc["name"]))
                        blocks.append({"type": "tool_use", "id": tid, "name": fc["name"], "input": fc.get("args") or {}})
                push("assistant", blocks)
                continue
            results: list[dict] = []
            others: list[dict] = []
            for p in parts:
                fr = p.get("functionResponse")
                if fr:
                    pos = next((i for i, (_, n) in enumerate(pending) if n == fr.get("name")), 0 if pending else None)
                    tid = pending.pop(pos)[0] if pos is not None else f"toolu_{counter:04d}"
                    results.append({"type": "tool_result", "tool_use_id": tid,
                                    "content": json.dumps(fr.get("response"), ensure_ascii=False)})
                elif p.get("text"):
                    others.append({"type": "text", "text": p["text"]})
                elif p.get("inlineData"):
                    d = p["inlineData"]
                    if str(d.get("mimeType", "")).startswith("image/"):
                        others.append({"type": "image", "source": {"type": "base64", "media_type": d["mimeType"], "data": d["data"]}})
            push("user", results + others)     # نتائج الأدوات يجب أن تسبق أي نص في نفس الرسالة
        return msgs

    def build_body(self, contents: list[dict], system: str | None, tools: list[dict] | None) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.spec.model, "max_tokens": self.max_tokens,
                                "messages": self.convert_messages(contents)}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [{"name": t["name"], "description": t.get("description", ""), "input_schema": _tool_schema(t)}
                             for t in tools]
        return body

    async def generate(self, contents: list[dict], system: str | None = None, tools: list[dict] | None = None,
                       google_search: bool | None = None) -> Reply:
        if not self.ready:
            raise GeminiError(f"{self.spec.name}: المفتاح أو النموذج غير مضبوط في الإعدادات.")
        status, data = await self._post(self.build_body(contents, system, tools))
        if status == 529:      # Anthropic: overloaded
            raise RateLimitError(f"{self.spec.name}: الخادم مشغول حالياً.", 20)
        if status != 200:
            raise self._explain(status, data)
        blocks = (data or {}).get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        parts: list[dict[str, Any]] = [{"text": text}] if text else []
        calls = []
        for b in blocks:
            if b.get("type") == "tool_use":
                args = b.get("input") if isinstance(b.get("input"), dict) else {}
                calls.append({"name": b.get("name", ""), "args": args})
                parts.append({"functionCall": {"name": b.get("name", ""), "args": args}})
        return Reply(content={"role": "model", "parts": parts}, text=text.strip(), calls=calls,
                     finish_reason=str(data.get("stop_reason") or ""), usage=data.get("usage") or {})


# --------------------------------------------------------------------------------- اختبار الاتصال
def check_provider(spec: ProviderSpec, timeout: float = 20.0) -> tuple[bool, str]:
    """يختبر المفتاح والرابط والنموذج بطلب GET خفيف على قائمة النماذج (لا يستهلك حصة توليد). (نجح؟، رسالة عربية)."""
    if spec.kind == "gemini":
        return gemini_check(spec.api_key, spec.model, spec.base_url or GEMINI_BASE, timeout)
    if not spec.base_url:
        return False, "أدخل رابط الخدمة (base URL)."
    pre = PRESETS.get(spec.id)
    if (pre["needs_key"] if pre else False) and not spec.api_key:
        return False, "لا يوجد مفتاح — الصقه أولاً."
    headers = ({"x-api-key": spec.api_key, "anthropic-version": AnthropicClient.VERSION} if spec.kind == "anthropic"
               else ({"Authorization": f"Bearer {spec.api_key}"} if spec.api_key else {}))
    req = urllib.request.Request(f"{spec.base_url}/models", headers=headers)
    try:
        with _opener().open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            body = {}
        if e.code in (401, 403):
            return False, f"المفتاح مرفوض ({e.code}). {_err_message(body)[:120]}"
        if e.code == 404:       # بعض الخدمات لا تعرض /models — لا يعني أن الإعداد خاطئ
            return True, "الرابط يستجيب (الخدمة لا تعرض قائمة النماذج) — جرّب سؤالاً في المساعد للتأكد."
        return False, f"خطأ ({e.code}): {_err_message(body)[:150]}"
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
        return False, f"لا يصل البرنامج إلى {spec.name}: {e}"
    items = data.get("data") or data.get("models") or []
    names = {str(m.get("id") or m.get("name") or "").split("/")[-1] if spec.id != "openrouter" else str(m.get("id") or "")
             for m in items if isinstance(m, dict)}
    names.discard("")
    full = {str(m.get("id") or m.get("name") or "") for m in items if isinstance(m, dict)}
    if spec.model and names and spec.model not in names and spec.model not in full:
        sample = sorted(full)[:6]
        return False, f"الاتصال والمفتاح سليمان، لكن النموذج «{spec.model}» غير متاح. أمثلة متاحة: " + "، ".join(sample)
    return True, f"✔ {spec.name}: الاتصال سليم" + (f" والنموذج «{spec.model}» متاح." if names else ".")


# --------------------------------------------------------------------------------- الموجّه
@dataclass
class _Slot:
    spec: ProviderSpec
    client: Any
    cooldown_until: float = 0.0


class MultiClient:
    """يجرّب المزوّدين بالترتيب؛ عند فشل أحدهم ينتقل للتالي. نفس واجهة GeminiClient (generate / transcribe_audio)."""

    def __init__(self, slots: list[_Slot], clock: Callable[[], float] = time.monotonic, default_cooldown: float = 60.0):
        self.slots, self._clock, self.default_cooldown = slots, clock, default_cooldown
        self.last_used: str = ""            # معرّف المزوّد الذي أجاب آخر مرة
        self.last_errors: list[str] = []    # أسباب فشل المزوّدين السابقين في آخر طلب (للعرض)

    # ---- واجهة متوافقة
    @property
    def ready(self) -> bool:
        return any(s.spec.ready for s in self.slots)

    @property
    def cfg(self) -> ProviderSpec | GeminiConfig:
        return next((s.spec for s in self.slots if s.spec.ready), self.slots[0].spec if self.slots else ProviderSpec("custom"))

    @property
    def retries(self) -> int:
        return getattr(self.slots[0].client, "retries", 0) if self.slots else 0

    @retries.setter
    def retries(self, v: int) -> None:
        for s in self.slots:
            if hasattr(s.client, "retries"):
                s.client.retries = v

    # ---- التوليد
    def _order(self) -> list[_Slot]:
        now = self._clock()
        usable = [s for s in self.slots if s.spec.ready]
        fresh = [s for s in usable if s.cooldown_until <= now]
        resting = sorted((s for s in usable if s.cooldown_until > now), key=lambda s: s.cooldown_until)
        return fresh + resting      # إن كانوا كلهم في راحة نجرّبهم على أي حال (الأقرب انتهاءً أولاً)

    async def generate(self, contents: list[dict], system: str | None = None, tools: list[dict] | None = None,
                       google_search: bool | None = None) -> Reply:
        order = self._order()
        if not order:
            raise GeminiError("لا يوجد أي مزوّد ذكاء اصطناعي جاهز — أضف مفتاحاً من الإعدادات ← مزوّدو الذكاء الاصطناعي.")
        self.last_errors = []
        first_exc: GeminiError | None = None
        for slot in order:
            try:
                kw = {} if google_search is None else {"google_search": google_search}
                reply = await slot.client.generate(contents, system, tools, **kw)
                self.last_used = slot.spec.id
                slot.cooldown_until = 0.0
                return reply
            except RateLimitError as e:
                slot.cooldown_until = self._clock() + (86400.0 if e.daily else max(e.retry_after or 0, self.default_cooldown))
                err = e
            except GeminiError as e:
                # مفتاح/نموذج خاطئ أو مشكلة اتصال: راحة قصيرة ثم التالي
                slot.cooldown_until = self._clock() + 30.0
                err = e
            first_exc = first_exc or err
            self.last_errors.append(f"{slot.spec.name}: {err}")
        if len(order) == 1 and first_exc is not None:
            raise first_exc                      # مزوّد واحد: نفس رسالته الأصلية
        raise GeminiError("فشلت كل النماذج المضبوطة:\n- " + "\n- ".join(self.last_errors))

    async def grounded_search(self, query: str) -> dict[str, Any]:
        """بحث ويب مؤسَّس عبر أول مزوّد Gemini جاهز (حتى لو كان المزوّد الرئيسي للمحادثة غيره). يرفع GeminiError إن لم يوجد."""
        errs: list[str] = []
        for slot in self.slots:
            c = slot.client
            if not (getattr(c, "ready", False) and hasattr(c, "grounded_search")):
                continue
            try:
                return await c.grounded_search(query)
            except GeminiError as e:
                errs.append(str(e))
        raise GeminiError(errs[0] if errs else "لا يوجد مزوّد Gemini جاهز للبحث (أضف مفتاح Gemini من الإعدادات).")

    async def transcribe_audio(self, data: bytes, mime: str = "audio/wav") -> str:
        """يجرّب المزوّدين الجاهزين القادرين على نسخ الصوت (Gemini ثم OpenAI/Groq Whisper) حتى ينجح أحدهم."""
        errs: list[str] = []
        capable = [sl for sl in self.slots if sl.spec.ready and (sl.spec.kind == "gemini" or sl.spec.id in TRANSCRIBE_MODELS)]
        for slot in sorted(capable, key=lambda sl: sl.spec.kind != "gemini"):
            try:
                return await slot.client.transcribe_audio(data, mime)
            except GeminiError as e:
                errs.append(f"{slot.spec.name}: {e}")
        if errs:
            raise GeminiError(" | ".join(errs)[:400])
        raise GeminiError("نسخ الصوت يحتاج مفتاح Gemini أو OpenAI أو Groq — أضف أحدها من الإعدادات.")


def make_client(spec: ProviderSpec, retries: int, transport: Callable | None = None) -> Any:
    if spec.kind == "gemini":
        cfg = GeminiConfig.from_env()
        cfg.api_key, cfg.model = spec.api_key or cfg.api_key, spec.model or cfg.model
        if spec.base_url:
            cfg.base_url = spec.base_url
        return GeminiClient(cfg, transport, retries=retries)
    if spec.kind == "anthropic":
        return AnthropicClient(spec, transport, retries=retries)
    return OpenAIClient(spec, transport, retries=retries)


def build_client(specs: list[ProviderSpec], transport: Callable | None = None) -> MultiClient:
    """يبني الموجّه. مع أكثر من مزوّد نقلّل إعادة المحاولة الداخلية (التبديل أسرع من الانتظار)."""
    active = [s for s in specs if s.enabled]
    retries = 1 if len([s for s in active if s.ready]) > 1 else 3
    return MultiClient([_Slot(s, make_client(s, retries, transport)) for s in active])
