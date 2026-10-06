"""تشكيل الحروف العربية (وصلها ببعض) وترتيب الاتجاه (RTL) بدون أي مكتبة خارجية.

يُستخدم فقط حين لا يتوفر libraqm (حال ويندوز غالباً)، فبدونه يرسم Pillow الحروف منفصلة ومعكوسة.
الجداول تُبنى من قاعدة بيانات يونيكود المدمجة بالبايثون (unicodedata) فلا أخطاء نقل يدوية.

الاستخدام: visual(text) ← نص جاهز للرسم من اليسار لليمين (Pillow BASIC layout).
"""
from __future__ import annotations

import unicodedata
from functools import lru_cache

_FORMS: dict[str, dict[str, str]] = {}      # الحرف الأساسي ← {isolated, initial, medial, final}
_LIGA: dict[tuple[str, str], dict[str, str]] = {}   # (ل, ا/أ/إ/آ) ← {isolated, final}


def _build() -> None:
    for cp in list(range(0xFB50, 0xFDFF)) + list(range(0xFE70, 0xFEFD)):
        dec = unicodedata.decomposition(chr(cp))
        if not dec.startswith("<") or ">" not in dec:
            continue
        tag, _, rest = dec.partition("> ")
        tag = tag[1:]
        if tag not in ("isolated", "initial", "medial", "final"):
            continue
        parts = rest.split()
        if len(parts) == 1:
            _FORMS.setdefault(chr(int(parts[0], 16)), {})[tag] = chr(cp)
        elif len(parts) == 2 and parts[0] == "0644" and parts[1] in ("0622", "0623", "0625", "0627"):  # لام + ألف فقط
            _LIGA.setdefault((chr(0x0644), chr(int(parts[1], 16))), {})[tag] = chr(cp)


_build()
_TATWEEL = "\u0640"
_MARKS = {chr(c) for c in range(0x064B, 0x0660)} | {"\u0670"}


def _joins_next(c: str) -> bool:
    return c == _TATWEEL or "initial" in _FORMS.get(c, {})


def _joins_prev(c: str) -> bool:
    return c == _TATWEEL or "final" in _FORMS.get(c, {})


def shape(text: str) -> str:
    """يستبدل الحروف بأشكالها (بداية/وسط/نهاية/منفصل) حسب موقعها — ترتيب منطقي لا بصري."""
    chars = [c for c in text if c not in _MARKS]
    out: list[str] = []
    i, n = 0, len(chars)
    while i < n:
        c = chars[i]
        forms = _FORMS.get(c)
        if forms is None:
            out.append(c)
            i += 1
            continue
        prev = chars[i - 1] if i > 0 else ""
        joined_prev = bool(prev) and _joins_next(prev) and _joins_prev(c)
        nxt = chars[i + 1] if i + 1 < n else ""
        if c == "\u0644" and nxt and (c, nxt) in _LIGA:           # لا / لأ / لإ / لآ
            lig = _LIGA[(c, nxt)]
            out.append(lig.get("final" if joined_prev else "isolated") or lig["isolated"])
            i += 2
            continue
        joined_next = bool(nxt) and _joins_next(c) and _joins_prev(nxt)
        key = ("medial" if joined_prev and joined_next else "final" if joined_prev
               else "initial" if joined_next else "isolated")
        out.append(forms.get(key) or forms.get("isolated") or c)
        i += 1
    return "".join(out)


# ---- ترتيب الاتجاه -----------------------------------------------------------------------------
_MIRROR = str.maketrans("()[]{}<>«»", ")(][}{><»«")
_NUM_SEP = ".,:/%"


def _cls(c: str) -> str:
    o = ord(c)
    if (0x0590 <= o <= 0x08FF) or (0xFB1D <= o <= 0xFDFF) or (0xFE70 <= o <= 0xFEFF):
        if 0x0660 <= o <= 0x0669 or 0x06F0 <= o <= 0x06F9:
            return "L"                     # أرقام عربية-هندية تُعامل كأرقام (تُرسم يساراً ليمين)
        return "R"
    if c.isdigit() or c.isalpha():
        return "L"
    return "N"                             # محايد: مسافة/علامة ترقيم/رمز


@lru_cache(maxsize=4096)
def visual(text: str) -> str:
    """نص عربي (قد يحوي أرقاماً وكلمات لاتينية) ← نص بصري جاهز للرسم من اليسار لليمين."""
    s = shape(text)
    classes = [_cls(c) for c in s]
    n = len(s)
    # علامة +/- مباشرة قبل رقم وفي بداية كلمة تلتصق بالرقم (حتى لا يصير -5 → 5-)
    for i, c in enumerate(s):
        nxt_digit = i + 1 < n and (s[i + 1].isdigit() or (s[i + 1] == "$" and i + 2 < n and s[i + 2].isdigit()))
        if c in "+-" and nxt_digit and (i == 0 or s[i - 1] == " "):
            classes[i] = "L"
        elif c == "$" and i + 1 < n and s[i + 1].isdigit():      # $70 تبقى ملتصقة بالرقم
            classes[i] = "L"
    # فواصل داخل الأرقام (1,250.50) تبقى مع الرقم
    for i in range(1, n - 1):
        if s[i] in _NUM_SEP and s[i - 1].isdigit() and s[i + 1].isdigit():
            classes[i] = "L"
    # حل المحايدات: بين اتجاهين متطابقين ← نفس الاتجاه، وإلا ← اتجاه الفقرة (RTL)
    resolved = list(classes)
    i = 0
    while i < n:
        if classes[i] != "N":
            i += 1
            continue
        j = i
        while j < n and classes[j] == "N":
            j += 1
        before = classes[i - 1] if i > 0 else "R"
        after = classes[j] if j < n else "R"
        d = before if before == after else "R"
        for k in range(i, j):
            resolved[k] = d
        i = j
    # تجميع المقاطع المتتالية بنفس الاتجاه
    runs: list[tuple[str, str]] = []
    for ch, d in zip(s, resolved):
        if runs and runs[-1][0] == d:
            runs[-1] = (d, runs[-1][1] + ch)
        else:
            runs.append((d, ch))
    # فقرة RTL: عكس ترتيب المقاطع، والمقاطع العربية يُعكس داخلها الحرف + تُقلب الأقواس
    out = []
    for d, txt in reversed(runs):
        out.append(txt[::-1].translate(_MIRROR) if d == "R" else txt)
    return "".join(out)
