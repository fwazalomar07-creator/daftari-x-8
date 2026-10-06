"""قارئ كتالوج FMI (FileMaker) عبر FileMaker Data API — بلا متصفح ولا مكتبات خارجية، فيعمل على أندرويد وiOS وويندوز.

لماذا؟ الرابط  http://188.59.6.153/fmi/webd/data  هو FileMaker WebDirect: تطبيق JavaScript لا تُقرأ صفحته بالاتصال العادي
(تُرجع فقط «Enable JavaScript»)، وPlaywright لا يعمل على الجوال. أما Data API فهو واجهة REST رسمية في نفس السيرفر:
    POST  /fmi/data/vLatest/databases/{db}/sessions                  (Basic: المستخدم:كلمة المرور) → token
    GET   /fmi/data/vLatest/databases/{db}/layouts                   → قائمة الشاشات (layouts)
    GET   /fmi/data/vLatest/databases/{db}/layouts/{layout}          → حقول الشاشة
    POST  /fmi/data/vLatest/databases/{db}/layouts/{layout}/records/_find
    DELETE /fmi/data/vLatest/databases/{db}/sessions/{token}
اسم قاعدة البيانات يُؤخذ من الرابط تلقائياً (…/fmi/webd/data → data).

المتطلبات على السيرفر: تفعيل Data API (FileMaker Server Admin Console) وحساب له امتياز fmrest (قراءة فقط يكفي).
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

TIMEOUT = 20.0
MAX_RECORDS = 100              # أقصى سجلات في البحث الواحد (كل سجل قد يحمل صورة)
MAX_FIND_FIELDS = 10
MAX_LAYOUT_TRIES = 3
MAX_VALUE_CHARS = 220
MAX_IMAGES = 100               # أقصى صور تُجلب للمحادثة في البحث الواحد (1…100)
IMAGES_DEADLINE = 90.0         # ثوانٍ: نتوقف عن تحميل المزيد بعدها ونعرض ما وصل (بدل تعليق المحادثة)
MAX_IMAGE_BYTES = 4_000_000    # لا نحمّل صورة أكبر من هذا (حماية الذاكرة/الشبكة)
IMAGE_SIDE = 1000
MAX_PORTAL_ROWS = 8
LAYOUT_HINTS = ("catalog", "catalogue", "كتالوج", "filter", "filtre", "فلتر", "product", "item", "part", "search",
                "list", "main", "data")


class FmiError(Exception):
    pass


@dataclass
class FmiResult:
    text: str
    layout: str
    count: int
    images: list[dict[str, Any]] = field(default_factory=list)   # [{"label": str, "data": bytes(JPEG)}]


def parse_webd_url(url: str) -> tuple[str, str]:
    """http://host[:port]/fmi/webd/data → ('http://host[:port]', 'data'). يقبل أيضاً الجذر بلا مسار."""
    u = urllib.parse.urlparse((url or "").strip())
    if not u.scheme or not u.netloc:
        raise FmiError("رابط FMI غير صالح")
    m = re.search(r"/fmi/webd/([^/?#]+)", u.path, re.I)
    return f"{u.scheme}://{u.netloc}", urllib.parse.unquote(m.group(1)) if m else ""


def sanitize_criteria(q: str) -> str:
    """يمنع تفسير نص الاستعلام كمشغّلات بحث FileMaker (= < > ! ? @ # * ~ \" \\) أو كتاريخ."""
    return re.sub(r'[=<>!?@#*~"\\]', " ", q or "").strip()


def _http(method: str, url: str, headers: dict[str, str] | None = None, body: Any = None) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FmiError(f"تعذّر الاتصال بسيرفر FMI: {getattr(e, 'reason', e)}") from e
    try:
        return code, json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        return code, {"_raw": raw[:200].decode("utf-8", "replace")}


def _fm_code(payload: dict[str, Any]) -> tuple[str, str]:
    msgs = payload.get("messages") or [{}]
    m = msgs[0] if msgs and isinstance(msgs[0], dict) else {}
    return str(m.get("code", "")), str(m.get("message", ""))


def _raise_for(code: int, payload: dict[str, Any], what: str) -> None:
    fm, msg = _fm_code(payload)
    if fm in ("0", "") and 200 <= code < 300:
        return
    if fm == "212" or code == 401 and fm not in ("401",):
        raise FmiError("اسم المستخدم أو كلمة المرور غير صحيحة في FMI (أو الحساب بلا امتياز fmrest)")
    if fm in ("802", "954") or code in (403, 404) and fm == "":
        raise FmiError("Data API غير مفعّل على سيرفر FMI أو قاعدة البيانات غير مفتوحة — "
                       "يجب على مدير السيرفر تفعيله (FileMaker Server ← Connectors ← FileMaker Data API)")
    if fm == "105":
        raise FmiError(f"الشاشة (layout) غير موجودة: {what}")
    raise FmiError(f"FMI ردّ بخطأ {fm or code}: {msg or payload.get('_raw', '')}".strip())


def probe(base: str) -> dict[str, Any]:
    """يفحص هل Data API متاح (بلا حساب). يرجع معلومات النسخة أو يرفع FmiError بسبب مفهوم."""
    code, p = _http("GET", f"{base}/fmi/data/version")
    if code == 200 and isinstance(p.get("response"), dict):
        return p["response"]
    raise FmiError("Data API غير متاح على هذا السيرفر (قد يكون معطّلاً أو خلف جدار حماية)")


class FmiClient:
    def __init__(self, base: str, db: str, user: str, password: str, version: str = "vLatest"):
        if not db:
            raise FmiError("اسم قاعدة بيانات FMI غير معروف — ضعه في حقل «قاعدة البيانات» أو استعمل رابطاً مثل …/fmi/webd/data")
        self.base, self.db, self.user, self.password = base.rstrip("/"), db, user, password
        self.root = f"{self.base}/fmi/data/{version}/databases/{urllib.parse.quote(db)}"
        self.token = ""

    def __enter__(self) -> "FmiClient":
        auth = base64.b64encode(f"{self.user}:{self.password}".encode("utf-8")).decode("ascii")
        code, p = _http("POST", f"{self.root}/sessions", {"Authorization": f"Basic {auth}"}, {})
        _raise_for(code, p, "login")
        self.token = ((p.get("response") or {}).get("token")) or ""
        if not self.token:
            raise FmiError("لم يُرجع FMI رمز جلسة")
        return self

    def __exit__(self, *exc) -> None:
        if self.token:
            try:
                _http("DELETE", f"{self.root}/sessions/{self.token}")
            except FmiError:
                pass
            self.token = ""

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def layouts(self) -> list[str]:
        code, p = _http("GET", f"{self.root}/layouts", self._auth())
        _raise_for(code, p, "layouts")
        out: list[str] = []

        def walk(items: list[Any]) -> None:
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                if it.get("isFolder") or it.get("folderLayoutNames"):
                    walk(it.get("folderLayoutNames") or [])
                elif it.get("name"):
                    out.append(str(it["name"]))
        walk((p.get("response") or {}).get("layouts") or [])
        return out

    def fields(self, layout: str) -> list[dict[str, str]]:
        code, p = _http("GET", f"{self.root}/layouts/{urllib.parse.quote(layout, safe='')}", self._auth())
        _raise_for(code, p, layout)
        return [{"name": str(f.get("name", "")), "result": str(f.get("result", "text"))}
                for f in (p.get("response") or {}).get("fieldMetaData") or [] if f.get("name")]

    def find(self, layout: str, queries: list[dict[str, str]], limit: int = MAX_RECORDS) -> list[dict[str, Any]]:
        code, p = _http("POST", f"{self.root}/layouts/{urllib.parse.quote(layout, safe='')}/records/_find",
                        self._auth(), {"query": queries, "limit": str(limit)})
        fm, _ = _fm_code(p)
        if fm == "401":                      # لا سجلات مطابقة — ليست خطأ
            return []
        _raise_for(code, p, layout)
        return [r for r in (p.get("response") or {}).get("data") or [] if isinstance(r, dict)]

    def download(self, url: str) -> bytes:
        """يحمّل ملف حقل صورة (container). رابطه يحتاج رمز الجلسة كـ Cookie (X-FMS-Session-Key)."""
        full = urllib.parse.urljoin(self.base + "/", url)
        cands = [full]
        u, b = urllib.parse.urlparse(full), urllib.parse.urlparse(self.base)
        if (u.scheme, u.netloc) != (b.scheme, b.netloc):          # السيرفر قد يعيد https/اسم داخلي لا نصل إليه
            cands.append(urllib.parse.urlunparse(u._replace(scheme=b.scheme, netloc=b.netloc)))
        last: Exception | None = None
        for c in cands:
            req = urllib.request.Request(c, headers={"Cookie": f"X-FMS-Session-Key={self.token}", "Accept": "image/*,*/*"})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    data = r.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    raise FmiError("الصورة كبيرة جداً")
                return data
            except FmiError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = e
        raise FmiError(f"تعذّر تحميل الصورة: {last}")


def pick_layouts(names: list[str], preferred: str = "") -> list[str]:
    if preferred:
        return [preferred]
    def score(n: str) -> int:
        low = n.lower()
        return -sum(1 for h in LAYOUT_HINTS if h in low) + (5 if low.startswith(("_", "-")) else 0)
    ranked = sorted(names, key=lambda n: (score(n), names.index(n)))
    return ranked[:MAX_LAYOUT_TRIES]


def build_queries(fields: list[dict[str, str]], variants: list[str]) -> list[dict[str, str]]:
    """OR عبر كل الحقول النصية × كل صيغ الاستعلام (FileMaker: كل كائن في المصفوفة = طلب بحث منفصل)."""
    text = [f["name"] for f in fields if f["result"] in ("text", "") and "::" not in f["name"]][:MAX_FIND_FIELDS]
    crit = [c for c in (sanitize_criteria(v) for v in variants) if c]
    return [{name: f"*{c}*"} for c in crit for name in text]


def _clean(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip()


def format_records(records: list[dict[str, Any]], fields: list[dict[str, str]]) -> str:
    """نص مضغوط لكل سجل: حقوله غير الفارغة + الجداول المرتبطة (portals: مرجعيات/سيارات…) حتى 8 صفوف لكل جدول."""
    skip = {f["name"] for f in fields if f["result"] == "container"}
    lines = []
    for rec in records:
        data = rec.get("fieldData") if "fieldData" in rec else rec
        parts = []
        for k, v in (data or {}).items():
            if k in skip or v in ("", None) or isinstance(v, (dict, list)):
                continue
            sv = _clean(v)
            if sv and not sv.lower().startswith(("http://", "https://")):      # روابط الصور لا تفيد النموذج
                parts.append(f"{k}: {sv[:MAX_VALUE_CHARS]}")
        for pname, rows in ((rec.get("portalData") or {}) if isinstance(rec, dict) else {}).items():
            vals = []
            for row in (rows or [])[:MAX_PORTAL_ROWS]:
                cells = [f"{k.split('::')[-1]}: {_clean(v)[:80]}" for k, v in row.items()
                         if k not in ("recordId", "modId") and not k.endswith(("recordId", "modId")) and v not in ("", None)
                         and not isinstance(v, (dict, list))]
                if cells:
                    vals.append(", ".join(cells))
            if vals:
                parts.append(f"[{pname}] " + " ؛ ".join(vals))
        if parts:
            lines.append(" | ".join(parts))
    return "\n".join(lines)


def _caption(rec: dict[str, Any]) -> str:
    data = rec.get("fieldData") if "fieldData" in rec else rec
    for v in (data or {}).values():
        if isinstance(v, str) and v.strip() and not v.lower().startswith("http") and len(v) < 90:
            return _clean(v)
    return ""


def norm_key(v: Any) -> str:
    """توحيد قيمة للمقارنة: أحرف/أرقام فقط بحروف صغيرة (HU 7008-z == hu7008z) مع الأرقام العربية."""
    t = str(v or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")).lower()
    return re.sub(r"[^0-9a-z\u0600-\u06ff]+", "", t)


def record_keys(rec: dict[str, Any]) -> list[str]:
    """القيم النصية القصيرة في السجل (الكود، أرقام OEM…) مطبّعة — أساس المطابقة الصارمة مع كود الصنف."""
    data = rec.get("fieldData") if "fieldData" in rec else rec
    out: list[str] = []
    for v in (data or {}).values():
        if isinstance(v, (dict, list)) or v in ("", None):
            continue
        sv = str(v).strip()
        if sv.lower().startswith(("http://", "https://")) or len(sv) > 60:
            continue
        k = norm_key(sv)
        if len(k) >= 3 and k not in out:
            out.append(k)
    return out


def _to_jpeg(raw: bytes) -> bytes | None:
    """يتأكد أن الملف صورة فعلاً (وليس PDF/HTML خطأ) ويحوّله JPEG مصغّراً؛ وإلا None."""
    try:
        from PIL import Image, ImageOps
        import io
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return None


def collect_images(client: "FmiClient", records: list[dict[str, Any]], fields: list[dict[str, str]],
                   limit: int = MAX_IMAGES) -> list[dict[str, Any]]:
    """يحمّل صور حقول container لأول السجلات المطابقة. فشل صورة لا يُفشل البحث."""
    import time
    t0 = time.monotonic()
    cont = [f["name"] for f in fields if f["result"] == "container"]
    out: list[dict[str, Any]] = []
    for rec in records:
        if len(out) >= limit or time.monotonic() - t0 > IMAGES_DEADLINE:
            break
        data = rec.get("fieldData") if "fieldData" in rec else rec
        for name in cont:
            url = str((data or {}).get(name) or "").strip()
            if not url.lower().startswith(("http://", "https://", "/")):
                continue
            try:
                jpg = _to_jpeg(client.download(url))
            except FmiError:
                continue
            if jpg:
                out.append({"label": _caption(rec) or name, "data": jpg, "keys": record_keys(rec)})
                break                       # صورة واحدة لكل سجل
    return out


def search(url: str, user: str, password: str, variants: list[str], layout: str = "", db: str = "",
           with_images: bool = True) -> FmiResult:
    """بحث متزامن (يُستدعى عبر asyncio.to_thread). يرفع FmiError بسبب مفهوم بالعربية."""
    base, db_url = parse_webd_url(url)
    if not (user and password):
        raise FmiError("بحث FMI يحتاج اسم مستخدم وكلمة مرور FileMaker (الإعدادات ← كتالوجات الفلاتر ← FMI)")
    last_layouts: list[str] = []
    with FmiClient(base, db or db_url, user, password) as c:
        names = [layout] if layout else c.layouts()
        last_layouts = pick_layouts(names, layout)
        if not last_layouts:
            raise FmiError("لا توجد شاشات (layouts) يستطيع هذا الحساب قراءتها")
        for lay in last_layouts:
            flds = c.fields(lay)
            queries = build_queries(flds, variants)
            if not queries:
                continue
            recs = c.find(lay, queries)
            if recs:
                imgs = collect_images(c, recs, flds) if with_images else []
                return FmiResult(format_records(recs, flds), lay, len(recs), imgs)
    return FmiResult("", ", ".join(last_layouts), 0)


def fetch_images_for_codes(url: str, user: str, password: str, codes: list[str], layout: str = "", db: str = "",
                           on_progress=None) -> dict[str, dict[str, Any]]:
    """لكل كود يبحث في FMI بجلسة واحدة ويحمّل صورة السجل **الذي يطابق الكود تماماً** فقط (لا مطابقة تقريبية).
    يرجع {code: {"label", "data", "keys"}} للأكواد التي وُجدت لها صورة. أخطاء الشبكة المفردة لا توقف الباقي."""
    base, db_url = parse_webd_url(url)
    if not (user and password):
        raise FmiError("يلزم حساب FileMaker (مستخدم وكلمة مرور) في الإعدادات")
    found: dict[str, dict[str, Any]] = {}
    with FmiClient(base, db or db_url, user, password) as c:
        names = [layout] if layout else c.layouts()
        order = pick_layouts(names, layout)
        if not order:
            raise FmiError("لا توجد شاشات (layouts) يستطيع هذا الحساب قراءتها")
        fields_cache: dict[str, list[dict[str, str]]] = {}
        for i, code in enumerate(codes):
            want = norm_key(code)
            if len(want) < 3:
                continue
            for lay in list(order):
                try:
                    flds = fields_cache.setdefault(lay, c.fields(lay))
                    queries = build_queries(flds, query_variants_for(code))
                    if not queries:
                        continue
                    recs = [r for r in c.find(lay, queries, limit=MAX_RECORDS) if want in record_keys(r)]
                    if not recs:
                        continue
                    imgs = collect_images(c, recs, flds, limit=1)
                    if imgs:
                        found[code] = imgs[0]
                        if lay != order[0]:
                            order.remove(lay); order.insert(0, lay)       # الشاشة الناجحة أولاً للأكواد التالية
                        break
                except FmiError:
                    continue
            if on_progress:
                on_progress(i + 1, len(codes))
    return found


def query_variants_for(code: str) -> list[str]:
    """صيغ بحث للكود (نفس منطق الكتالوجات بلا استيراد دائري)."""
    q0 = re.sub(r"\s+", " ", str(code or "")).strip()
    compact = re.sub(r"[\s\-_./\\]+", "", q0)
    spaced = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", compact)
    out: list[str] = []
    for v in (q0, compact, spaced):
        if v and v.lower() not in (x.lower() for x in out):
            out.append(v)
    return out[:3]
