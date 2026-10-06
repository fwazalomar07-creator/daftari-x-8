"""النطق (Text-to-Speech) بلغتين: اكتشاف لغة النص تلقائياً + خطة نطق + توليد الصوت عبر Gemini.

ملاحظة: البرنامج لا يحوي (حتى الآن) زر «اقرأ بصوت عالٍ» في الواجهة — هذه الوحدة هي الأساس الجاهز:
  • detect_lang(text)      → 'ar' أو 'en' بنسبة الحروف (Regex) — للنص المختلط يغلب الأكثر.
  • speech_plan(text)      → [(lang, مقطع)] يقسّم الجملة المختلطة (عربي + أكواد/أسماء إنجليزية) لمقاطع، كلٌّ بلغته الصحيحة،
                              فلا تُتجاهل الحروف الإنجليزية ولا تُنطق بلكنة عربية.
  • synthesize(client, text) → WAV (24kHz) عبر نموذج Gemini TTS؛ يمرّر lang لكل مقطع ويدمجها.
تشغيل الصوت داخل الواجهة يحتاج مكوّن تشغيل (flet-audio) ويُضاف عند طلب الميزة.
"""
from __future__ import annotations

import base64
import re
from typing import Any

from .gemini import GeminiError

_AR = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_EN = re.compile(r"[A-Za-z]")
# مقطع لاتيني متصل: كلمات/أكواد (HU-7008, 5W-40, SAE 10W30) مع ما يربطها
_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[\s\-/.+][A-Za-z0-9]+)*")

TTS_MODEL = "gemini-2.5-flash-preview-tts"
TTS_RATE = 24000


def detect_lang(text: str, default: str = "ar") -> str:
    """'en' إن غلبت الحروف الإنجليزية، 'ar' إن غلبت العربية. بلا حروف (أرقام فقط) → default."""
    ar, en = len(_AR.findall(text or "")), len(_EN.findall(text or ""))
    if not ar and not en:
        return default
    return "en" if en > ar else "ar"


def speech_plan(text: str, min_run: int = 2) -> list[tuple[str, str]]:
    """يقسّم النص لمقاطع (lang, chunk): المقاطع اللاتينية الطويلة (≥ min_run حرفاً) 'en' والباقي 'ar'. المتجاورة بنفس اللغة تُدمج."""
    text = (text or "").strip()
    if not text:
        return []
    out: list[tuple[str, str]] = []
    pos = 0

    def push(lang: str, chunk: str) -> None:
        if not chunk.strip():
            return
        if out and out[-1][0] == lang:
            out[-1] = (lang, out[-1][1] + chunk)
        else:
            out.append((lang, chunk))

    for m in _LATIN_RUN.finditer(text):
        run = m.group(0)
        if len(_EN.findall(run)) < min_run:
            continue
        push("ar", text[pos:m.start()])
        push("en", run)
        pos = m.end()
    push("ar", text[pos:])
    return [(lang, chunk.strip()) for lang, chunk in out if chunk.strip()]


def build_tts_body(text: str, lang: str, voice: str = "Kore") -> dict[str, Any]:
    prefix = "Say in English: " if lang == "en" else "انطق بالعربية بوضوح: "
    return {"contents": [{"role": "user", "parts": [{"text": prefix + text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}


def parse_tts_reply(data: dict[str, Any]) -> bytes:
    for cand in data.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    raise GeminiError("لم يُرجع نموذج النطق أي صوت")


async def synthesize(client: Any, text: str, voice: str = "Kore") -> bytes:
    """يرجع WAV (PCM 16-bit، 24kHz). client: GeminiClient (له _post وcfg.model). يدمج مقاطع اللغتين بالترتيب."""
    from .media import pcm16_to_wav
    plan = speech_plan(text)
    if not plan:
        raise GeminiError("لا يوجد نص للنطق")
    pcm = b""
    original = client.cfg.model
    try:
        client.cfg.model = TTS_MODEL
        for lang, chunk in plan:
            status, data = await client._post(build_tts_body(chunk, lang, voice))
            if status != 200:
                raise GeminiError(str(client._explain(status, data)))
            pcm += parse_tts_reply(data)
    finally:
        client.cfg.model = original
    return pcm16_to_wav(pcm, TTS_RATE)
