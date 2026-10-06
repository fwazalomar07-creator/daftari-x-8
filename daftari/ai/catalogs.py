"""البحث في كتالوجات الفلاتر على الإنترنت (Sardes, MANN, Delsa, FMI …) وإرجاع نصوص النتائج للمساعد.

لكل كتالوج «طريقة» (mode):
  form     — صفحات تقليدية (PHP/ASP): يكتشف البرنامج نموذج البحث تلقائياً، يملؤه بالاستعلام، يرسله (مع الكوكيز)،
             ثم يستخرج نص الجداول. بلا مكتبات خارجية.
  browser  — مواقع تعتمد JavaScript (FileMaker WebDirect، MANN …): يفتح متصفحاً مخفياً عبر Playwright (اختياري)،
             يكتب في أول حقل بحث ويقرأ النتيجة. يحتاج:  pip install playwright  ثم  playwright install chromium
  template — رابط بحث جاهز فيه {q} (GET) إن عرفتَه: يُجلب مباشرة.
  filemaker — سيرفر FileMaker (مثل FMI): WebDirect لا يُقرأ بالاتصال العادي، فنستعمل Data API الرسمي (ai/fmi.py) بحساب قراءة؛
             يعمل على أندرويد وiOS وويندوز. إن لم يتوفر حساب وكان Playwright موجوداً (ويندوز) نجرّب المتصفح، وإلا نعطي رابطاً لفتح الموقع.

وفي كل الحالات إن فشلت الطريقة الأساسية أو لم تُرجع شيئاً يجرّب «بحث الموقع» (site:النطاق + الاستعلام) كحل أخير.
عند الفشل/الفراغ يحفظ نسخة تشخيصية (نص + لقطة شاشة للمتصفح) في  ~/.daftari/catalog_debug  لمعرفة سبب المشكلة وضبط المصدر.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import importlib.util
import http.cookiejar
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from .websearch import SearchError, UA, search_web

MAX_TEXT = 2500          # أقصى حروف تُرجَع لكل كتالوج (حتى لا نُغرق النموذج)
HTTP_TIMEOUT = 20.0
BROWSER_TIMEOUT = 45.0


class CatalogError(Exception):
    pass


@dataclass
class CatalogSource:
    id: str
    name: str
    url: str
    mode: str = "form"            # form | browser | template
    enabled: bool = True
    search_url: str = ""          # لوضع template: رابط فيه {q}
    note: str = ""
    user: str = ""                # لوضع filemaker: حساب قراءة (امتياز fmrest)
    password: str = ""
    layout: str = ""              # اختياري: اسم الشاشة (layout) المراد البحث فيها؛ فارغ = اكتشاف تلقائي
    db: str = ""                  # اختياري: اسم قاعدة البيانات؛ فارغ = من الرابط (…/fmi/webd/data → data)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "url": self.url, "mode": self.mode, "enabled": self.enabled,
                "search_url": self.search_url, "note": self.note, "user": self.user, "password": self.password,
                "layout": self.layout, "db": self.db}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CatalogSource":
        return cls(id=str(d.get("id") or d.get("name") or "src").strip().lower().replace(" ", "-"),
                   name=str(d.get("name") or d.get("id") or "كتالوج"), url=str(d.get("url") or "").strip(),
                   mode=str(d.get("mode") or "form"), enabled=bool(d.get("enabled", True)),
                   search_url=str(d.get("search_url") or "").strip(), note=str(d.get("note") or ""),
                   user=str(d.get("user") or "").strip(), password=str(d.get("password") or ""),
                   layout=str(d.get("layout") or "").strip(), db=str(d.get("db") or "").strip())


# الكتالوجات الافتراضية. الروابط قابلة للتعديل من الإعدادات.
DEFAULT_SOURCES: list[CatalogSource] = [
    CatalogSource("fmi", "كتالوج FMI (FileMaker)", "http://188.59.6.153/fmi/webd/data", "filemaker",
                  note="FileMaker WebDirect — يُقرأ عبر Data API بحساب قراءة (اسم مستخدم/كلمة مرور) أو يُفتح يدوياً"),
    CatalogSource("sardes", "Sardes — بحث", "http://katalog.sardesfiltre.com/index.php?sayfa=sardesarama", "form"),
    CatalogSource("sardes-xref", "Sardes — مرجع متقاطع", "http://katalog.sardesfiltre.com/index.php?sayfa=caprazreferans", "form"),
    CatalogSource("mann", "MANN-FILTER", "https://catalog.mann-filter.com/EU/tur/oenumbers", "browser",
                  note="كتالوج MANN الرسمي — يعتمد JavaScript"),
    CatalogSource("delsa", "Delsa (تركي)", "https://www.delsafilter.com.tr", "browser",
                  note="تأكد من رابط كتالوج Delsa الصحيح من الإعدادات"),
]


def merged_sources(settings: dict[str, Any] | None) -> list[CatalogSource]:
    """الافتراضية + ما عدّله/أضافه المستخدم في الإعدادات (settings['catalogs'] قائمة قواميس). نفس id يغلب الافتراضي."""
    by_id = {s.id: s for s in DEFAULT_SOURCES}
    order = [s.id for s in DEFAULT_SOURCES]
    for d in (settings or {}).get("catalogs") or []:
        if isinstance(d, dict):
            s = CatalogSource.from_dict(d)
            if s.mode == "browser" and "/fmi/webd/" in s.url.lower():
                s.mode = "filemaker"          # الإعدادات القديمة: WebDirect لا يُقرأ بالمتصفح على الجوال
            if s.id not in by_id:
                order.append(s.id)
            by_id[s.id] = s
    return [by_id[i] for i in order]


# ---------------------------------------------------------------------------------- HTML → نص
class _TextExtractor(HTMLParser):
    """يحوّل HTML إلى نص مقروء: الجداول صفوفاً بفواصل « | »، ويتجاهل السكربتات والأنماط."""
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "head"):
            self._skip += 1
        elif tag in self.BLOCK or tag == "tr":
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.out.append(f" [{alt}] ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("tr", "table"):
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.out.append(data)


def html_to_text(raw: str, limit: int = MAX_TEXT) -> str:
    p = _TextExtractor()
    try:
        p.feed(raw)
    except Exception:  # noqa: BLE001 — HTML مشوّه
        pass
    text = "".join(p.out)
    lines = []
    for ln in text.splitlines():
        ln = re.sub(r"[ \t\u00a0]+", " ", ln).strip(" |")
        ln = re.sub(r"(\s*\|\s*)+", " | ", ln).strip()
        if ln and (not lines or lines[-1] != ln):
            lines.append(ln)
    return "\n".join(lines)[:limit]


# ---------------------------------------------------------------------------------- اكتشاف النموذج
@dataclass
class _Form:
    action: str = ""
    method: str = "get"
    fields: list[dict[str, str]] = field(default_factory=list)   # {name,type,value}
    selects: dict[str, str] = field(default_factory=dict)
    submit: tuple[str, str] | None = None


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self._cur: _Form | None = None
        self._sel: str | None = None
        self._sel_first: bool = False

    def handle_starttag(self, tag, attrs):
        d = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._cur = _Form(d.get("action", ""), (d.get("method") or "get").lower())
            self.forms.append(self._cur)
        elif self._cur is None:
            return
        elif tag == "input":
            t, n = (d.get("type") or "text").lower(), d.get("name", "")
            if t in ("submit", "button", "image"):
                if n and self._cur.submit is None:
                    self._cur.submit = (n, d.get("value", ""))
            elif n:
                if t in ("checkbox", "radio") and "checked" not in d:
                    return
                self._cur.fields.append({"name": n, "type": t, "value": d.get("value", "")})
        elif tag == "textarea" and d.get("name"):
            self._cur.fields.append({"name": d["name"], "type": "text", "value": ""})
        elif tag == "select":
            self._sel, self._sel_first = d.get("name", ""), True
        elif tag == "option" and self._sel:
            if self._sel_first or "selected" in d:
                self._cur.selects[self._sel] = d.get("value", "")
                self._sel_first = False

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None
        elif tag == "select":
            self._sel = None


_SEARCH_HINTS = ("ara", "search", "q", "kod", "code", "keyword", "ref", "no", "part", "oem", "term", "text", "sorgu")


def pick_search_form(html: str) -> tuple[_Form, str] | None:
    """يختار نموذج البحث وحقله النصي الأنسب. يرجع (النموذج، اسم الحقل) أو None."""
    p = _FormParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001
        return None
    best: tuple[int, _Form, str] | None = None
    for f in p.forms:
        texts = [x for x in f.fields if x["type"] in ("text", "search", "")]
        if not texts:
            continue
        for x in texts:
            nm = x["name"].lower()
            score = 10 if any(h == nm or h in nm for h in _SEARCH_HINTS) else 1
            if best is None or score > best[0]:
                best = (score, f, x["name"])
    return (best[1], best[2]) if best else None


# ---------------------------------------------------------------------------------- HTTP (وضع form/template)
def _decode(raw: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "windows-1254", "latin-1"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


class _Http:
    """جلسة HTTP بكوكيز (مواقع PHP تحتاج الجلسة بين طلب الصفحة وإرسال النموذج)."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.headers = {"User-Agent": UA, "Accept-Language": "tr,ar,en;q=0.8", "Accept": "text/html,*/*"}

    def request(self, url: str, data: bytes | None = None, referer: str = "") -> tuple[str, str]:
        h = dict(self.headers)
        if referer:
            h["Referer"] = referer
        if data is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with self.opener.open(req, timeout=HTTP_TIMEOUT) as r:
                return _decode(r.read(), r.headers.get("Content-Type", "")), r.geturl()
        except urllib.error.HTTPError as e:
            raise CatalogError(f"الموقع ردّ بحالة {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CatalogError(f"تعذّر الاتصال: {e}") from e


def fetch_url(url: str) -> str:
    """يجلب صفحة عامة كنص HTML (للأداة read_webpage). يرفع CatalogError بسبب مفهوم."""
    page, _final = _Http().request(url)
    return page


def form_search(src: CatalogSource, query: str) -> str:
    """يكتشف نموذج البحث في صفحة المصدر ويرسل الاستعلام. يرجع نص النتيجة."""
    http_ = _Http()
    page, final_url = http_.request(src.url)
    found = pick_search_form(page)
    if not found:
        raise CatalogError("لم أجد نموذج بحث في الصفحة (غالباً الموقع يعتمد JavaScript) — جرّب الوضع browser")
    form, qname = found
    payload: dict[str, str] = {}
    for f in form.fields:
        payload[f["name"]] = f["value"]
    for n, v in form.selects.items():
        payload.setdefault(n, v)
    payload[qname] = query
    if form.submit:
        payload[form.submit[0]] = form.submit[1]
    action = urllib.parse.urljoin(final_url, form.action or final_url)
    enc = urllib.parse.urlencode(payload)
    if form.method == "post":
        result, _ = http_.request(action, enc.encode("utf-8"), referer=final_url)
    else:
        sep = "&" if "?" in action else "?"
        result, _ = http_.request(action + sep + enc, referer=final_url)
    return result


def template_search(src: CatalogSource, query: str) -> str:
    if "{q}" not in src.search_url:
        raise CatalogError("رابط البحث يجب أن يحوي {q}")
    page, _ = _Http().request(src.search_url.replace("{q}", urllib.parse.quote(query)))
    return page


# ---------------------------------------------------------------------------------- المتصفح (Playwright اختياري)
_INPUT_SELECTORS = [
    "input[type=search]", "input[placeholder*='ara' i]", "input[placeholder*='search' i]", "input[placeholder*='بحث']",
    "input[name*='search' i]", "input[name*='q' i]", "input[type=text]", "input:not([type])", "textarea",
]


async def browser_search(src: CatalogSource, query: str, debug_dir: Path | None = None) -> str:
    try:
        from playwright.async_api import async_playwright   # type: ignore
    except ImportError as e:
        raise CatalogError("Playwright غير مثبت: نفّذ  pip install playwright  ثم  playwright install chromium") from e
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            raise CatalogError(f"تعذّر تشغيل المتصفح (هل نفّذت playwright install chromium؟): {str(e)[:150]}") from e
        try:
            ctx = await browser.new_context(user_agent=UA, ignore_https_errors=True, locale="tr-TR")
            page = await ctx.new_page()
            await page.goto(src.url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: BLE001 — WebDirect لا يهدأ أبداً؛ نكمل
                pass
            await page.wait_for_timeout(3000)       # تحميل تطبيقات JS الثقيلة (FileMaker)
            target = None
            for sel in _INPUT_SELECTORS:
                loc = page.locator(sel)
                for i in range(min(await loc.count(), 6)):
                    el = loc.nth(i)
                    if await el.is_visible():
                        target = el
                        break
                if target:
                    break
            if target is None:
                text = (await page.inner_text("body"))[:MAX_TEXT]
                await _save_debug(page, src, text, debug_dir)
                raise CatalogError("لم أجد حقل بحث ظاهراً في الصفحة — حُفظت لقطة تشخيص")
            await target.click()
            await target.fill(query)
            await target.press("Enter")
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(2500)
            text = await page.inner_text("body")
            await _save_debug(page, src, text, debug_dir)
            return text
        finally:
            await browser.close()


async def _save_debug(page: Any, src: CatalogSource, text: str, debug_dir: Path | None) -> None:
    if not debug_dir:
        return
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"{src.id}.txt").write_text(text, encoding="utf-8")
        await page.screenshot(path=str(debug_dir / f"{src.id}.png"), full_page=False)
    except Exception:  # noqa: BLE001 — التشخيص لا يُفشل البحث
        pass


# ---------------------------------------------------------------------------------- بحث الموقع (حل أخير)
def site_search(src: CatalogSource, query: str, fetch: Callable | None = None) -> str:
    host = urllib.parse.urlparse(src.url).netloc
    if not host:
        raise CatalogError("رابط المصدر غير صالح")
    try:
        res = search_web(f"site:{host} {query}", max_results=5, fetch=fetch)
    except SearchError as e:
        raise CatalogError(str(e)) from e
    lines = [f"{r['title']} — {r['url']}" + (f"\n  {r['snippet']}" if r.get("snippet") else "") for r in res["results"]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------- دقة البحث
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def query_variants(q: str) -> list[str]:
    """صيغ الاستعلام: أرقام القطع تُكتب بأشكال مختلفة بين الكتالوجات (HU 7008 z / HU7008z / HU-7008-Z).
    نرجع حتى 3 صيغ: الأصلية بعد توحيد الأرقام العربية، المضغوطة (بلا مسافات/شرطات)، والمفصولة بين الحروف والأرقام."""
    q0 = re.sub(r"\s+", " ", (q or "").translate(_DIGITS)).strip()
    if not q0:
        return []
    compact = re.sub(r"[\s\-_./\\]+", "", q0)
    spaced = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", compact)
    out: list[str] = []
    for v in (q0, compact, spaced):
        if v and v.lower() not in (x.lower() for x in out):
            out.append(v)
    return out[:3]


def _key(s: str) -> str:
    return re.sub(r"[^0-9a-z\u0600-\u06ff]+", "", s.lower().translate(_DIGITS))


def focus_lines(text: str, variants: list[str]) -> str:
    """يقدّم الأسطر التي تحوي رقم/اسم الاستعلام فعلاً (بصرف النظر عن المسافات والشرطات) قبل بقية الصفحة،
    فلا تضيع النتيجة المطلوبة تحت قوائم الموقع وعند الاقتطاع إلى MAX_TEXT."""
    keys = [k for k in (_key(v) for v in variants) if len(k) >= 2]
    if not keys:
        return text
    hit, rest = [], []
    for ln in text.splitlines():
        (hit if any(k in _key(ln) for k in keys) else rest).append(ln)
    return "\n".join(hit + rest) if hit else text


# ---------------------------------------------------------------------------------- التنسيق العام
def _clean_result(raw: str, via: str, variants: list[str] | None = None) -> str:
    if via in ("browser", "site", "filemaker"):
        txt = raw
    else:
        txt = html_to_text(raw, MAX_TEXT * 3)
    txt = "\n".join(ln.strip() for ln in txt.splitlines() if ln.strip())
    if variants:
        txt = focus_lines(txt, variants)
    return txt[:MAX_TEXT]


def _has_playwright() -> bool:
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


def _fmi_hint(src: CatalogSource) -> str:
    return (f"«{src.name}» تطبيق FileMaker WebDirect (JavaScript) فلا يُقرأ بالاتصال العادي. الحل: أدخل اسم مستخدم وكلمة مرور "
            "FileMaker (حساب قراءة) في الإعدادات ← كتالوجات الفلاتر ← هذا الكتالوج ليبحث فيه المساعد مباشرة، "
            "أو افتح الموقع يدوياً بالرابط وابحث بنفسك.")


async def filemaker_search(src: CatalogSource, variants: list[str], debug_dir: Path | None = None,
                           images_out: list | None = None) -> str:
    """Data API أولاً (يعمل على الجوال)، ثم المتصفح إن كان Playwright موجوداً، وإلا خطأ مفهوم مع طريقة الحل.
    صور الصنف (حقول container) تُضاف إلى images_out كـ [{label, data}] لتُعرض في المحادثة."""
    from . import fmi
    err = ""
    if src.user and src.password:
        try:
            res = await asyncio.to_thread(fmi.search, src.url, src.user, src.password, variants, src.layout, src.db)
            if res.text:
                if images_out is not None:
                    images_out.extend(res.images)
                return res.text
            err = f"لا سجلات مطابقة في شاشة FMI «{res.layout}»"
        except fmi.FmiError as e:
            err = str(e)
    if _has_playwright():            # ويندوز/لينكس بحزم اختيارية
        try:
            return await browser_search(src, variants[0], debug_dir)
        except CatalogError as e:
            err = err or str(e)
    if err:
        raise CatalogError(err)
    raise CatalogError(_fmi_hint(src))


async def _primary(src: CatalogSource, query: str, variants: list[str], debug_dir: Path | None,
                   images_out: list | None = None) -> str:
    """نص نتيجة المصدر الأساسي لصيغة واحدة (أو كل الصيغ دفعة واحدة في filemaker)."""
    if src.mode == "filemaker":
        raw = await asyncio.wait_for(filemaker_search(src, variants, debug_dir, images_out), BROWSER_TIMEOUT + 25)
        return _clean_result(raw, "filemaker", variants)
    if src.mode == "browser":
        raw = await asyncio.wait_for(browser_search(src, query, debug_dir), BROWSER_TIMEOUT)
        return _clean_result(raw, "browser", variants)
    if src.mode == "template":
        raw = await asyncio.wait_for(asyncio.to_thread(template_search, src, query), HTTP_TIMEOUT + 5)
    else:
        raw = await asyncio.wait_for(asyncio.to_thread(form_search, src, query), HTTP_TIMEOUT * 2 + 5)
    return _clean_result(raw, "html", variants)


async def search_one(src: CatalogSource, query: str, debug_dir: Path | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": src.id, "name": src.name, "url": src.url, "open_url": src.url, "status": "error",
                           "via": src.mode, "text": "", "error": ""}
    variants = query_variants(query) or [query]
    images: list = []
    primary_err, text, via = "", "", src.mode
    # filemaker يرسل كل الصيغ في طلب واحد؛ البقية نجرّب الصيغ بالتتابع حتى نجد نتيجة
    attempts = [variants] if src.mode == "filemaker" else [[v] for v in variants]
    for tried, vs in enumerate(attempts):
        try:
            text = await _primary(src, vs[0], variants if src.mode == "filemaker" else vs, debug_dir, images)
            primary_err = ""
        except asyncio.TimeoutError:
            primary_err = "انتهت المهلة"
        except CatalogError as e:
            primary_err = str(e)
        except Exception as e:  # noqa: BLE001
            primary_err = f"{type(e).__name__}: {e}"
        if len(text) >= 40 or src.mode == "filemaker" and text:
            if tried:
                out["matched_as"] = vs[0]
            break
        if primary_err and src.mode in ("filemaker", "browser"):
            break                      # فشل بنيوي (غير مثبّت/بلا حساب): لا فائدة من تغيير صيغة الاستعلام
    # نص قصير جداً غالباً صفحة فارغة/رسالة «لا نتائج» — نعتبره فارغاً ونجرّب الحل الأخير
    if len(text) < 40 and not (src.mode == "filemaker" and text):
        try:
            alt = await asyncio.to_thread(site_search, src, variants[0])
            if alt.strip():
                text, via = alt, "site"
        except CatalogError as e:
            primary_err = primary_err or str(e)
    if text and len(text) >= 20:
        out.update(status="ok", via=via, text=text, error=primary_err if via == "site" else "")
        if images and via != "site":
            out["_images"] = images          # bytes: لا تُرسل للنموذج، تُعرض للمالك في المحادثة (ToolBox ينزعها)
            out["images_found"] = len(images)
    else:
        out.update(status="empty" if not primary_err else "error", via=via, error=primary_err or "لا نتائج")
    return out


async def search_catalogs(query: str, sources: list[CatalogSource], only: list[str] | None = None,
                          debug_dir: Path | None = None) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        raise CatalogError("اكتب اسم الفلتر أو رقمه")
    wanted = [s for s in sources if s.enabled and (not only or s.id in only or s.name in only)]
    if not wanted:
        raise CatalogError("لا توجد كتالوجات مفعّلة (أو لم يطابق الاسم المطلوب)")
    results = await asyncio.gather(*(search_one(s, q, debug_dir) for s in wanted))
    return {"query": q, "variants": query_variants(q), "sources": list(results),
            "hint": "اذكر لكل معلومة اسم الكتالوج الذي جاءت منه. المصادر بحالة error/empty لم تُرجع بيانات — لا تخمّن لها. "
                    "إن فشل كتالوج وكان له open_url فأخبر المالك بالسبب بدقة واعرض عليه فتح الموقع بأداة open_catalog."}
