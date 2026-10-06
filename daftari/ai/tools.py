"""أدوات «مساعد شركة العمر»: ما يستطيع Gemini رؤيته وفعله داخل البرنامج.

المبادئ:
  • القراءة حرّة (كل جداول app_data + الملفات المصدرية للقراءة فقط، دون .env).
  • الكتابة تمرّ دائماً عبر Ledger (نفس التحقق والدمج والمخزون والديون) — لا SQL مباشر ولا كتابة خام.
  • كل أداة كتابة تطلب موافقة المستخدم (إلا إن فعّل «التنفيذ المباشر»)، وتُؤخذ نسخة احتياطية قبلها.
  • «إضافة الميزات»: يكتب المساعد اقتراح كود في مجلد proposals لتراجعه أنت — لا يعدّل ملفات البرنامج الحية.
"""
from __future__ import annotations

import asyncio

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..core.calc import profit_report
from ..core.models import EXPENSE_CATEGORIES, InvoiceLine, Record, uid
from ..core.calc import invoice_cost, invoice_totals
from ..core.money import ZERO, q2, to_decimal, to_json_number
from ..core.reports import customer_statement, low_stock_items, top_selling
from ..core.search import fuzzy_match, search_inventory
from ..ledger import InvoiceDraft, Ledger, LedgerError
from . import excel
from .websearch import SearchError, search_web

PACKAGE_DIR = Path(__file__).resolve().parent.parent
READ_LIMIT_CHARS = 60_000
MAX_PICTURES = 100          # أقصى صور في الرد الواحد (1…100)
Confirm = Callable[[str], "Awaitable[bool]"]


def jsonable(obj: Any) -> Any:
    """يحوّل Decimal/Record/قوائم إلى JSON آمن للإرسال إلى Gemini."""
    if isinstance(obj, Decimal):
        return to_json_number(obj)
    if isinstance(obj, Record):
        return jsonable(obj.to_dict())
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    return obj


def _obj(props: dict[str, tuple[str, str]], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "OBJECT",
            "properties": {k: {"type": t, "description": d} for k, (t, d) in props.items()},
            **({"required": required} if required else {})}


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    write: bool = False
    describe: Callable[[dict], str] | None = None

    def declaration(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.params.get("properties"):
            d["parameters"] = self.params
        return d


class ToolBox:
    def __init__(self, ledger: Ledger, store: Any = None, home: Path | None = None,
                 confirm: Confirm | None = None, auto_approve: Callable[[], bool] | None = None,
                 images: Any = None):
        self.led, self.store = ledger, store
        self.home = Path(home) if home else None
        self._images = images                        # ImageStore لصور الأصناف (يُنشأ عند الحاجة إن لم يُمرَّر)
        self.confirm = confirm
        self.auto_approve = auto_approve or (lambda: False)
        self.writes_done = 0
        self.documents: list[dict[str, str]] = []   # فواتير/سندات أنشأها المساعد (تظهر في المحادثة بزر عرض/طباعة)
        self.exports: list[dict[str, Any]] = []     # ملفات Excel التي أنشأها المساعد (تظهر في المحادثة بأزرار فتح/حفظ)
        self.catalog_pictures: list[list[dict[str, Any]]] = []   # صور كتالوج (FMI…) تظهر في المحادثة كمعرض
        self.notices: list[str] = []                 # أسطر «تم تلقائياً…» تُعرض للمالك في المحادثة
        self.links: list[dict[str, str]] = []        # روابط مواقع (كتالوجات) طلب المساعد فتحها: تظهر كزر «فتح الموقع»
        self.pictures: list[list[dict[str, Any]]] = []   # صور أصناف أرسلها المساعد (كل استدعاء = معرض صور في المحادثة)
        self.user_images: list[bytes] = []           # صور أرسلها المالك في رسالته الأخيرة (الأصل بجودة جيدة)
        self.cleaned_image: bytes | None = None      # آخر صورة نظّفها المساعد (خلفية بيضاء)
        self.excel_files: dict[str, dict] = {}       # ملفات Excel التي رفعها المالك: اسم → {headers, rows}
        self.tools: dict[str, Tool] = {}
        self._register()

    # ------------------------------------------------------------------ واجهة للوكيل
    def declarations(self) -> list[dict[str, Any]]:
        return [t.declaration() for t in self.tools.values()]

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = self.tools.get(name) or self.tools.get({
            "googleSearch": "google_search", "google_search_retrieval": "google_search",
        }.get(name, ""))
        if not tool:
            return {"ok": False, "error": f"أداة غير معروفة: {name} — هذه الميزة غير متوفرة في البرنامج. "
                    "أخبر المالك بذلك واختم بـ «يرجى مراجعة المطوّر فواز العمر.»"}
        try:
            if tool.write:
                what = tool.describe(args) if tool.describe else f"{name} {json.dumps(args, ensure_ascii=False)}"
                if not self.auto_approve():
                    if not self.confirm or not await self.confirm(what):
                        return {"ok": False, "rejected": True, "error": "رفض المستخدم تنفيذ هذا الإجراء — لم يتغير شيء."}
                self._backup()
            result = await tool.handler(**args)
            if tool.write:
                self.writes_done += 1
            return {"ok": True, "result": jsonable(result)}
        except LedgerError as e:
            return {"ok": False, "error": str(e)}
        except TypeError as e:  # وسائط ناقصة/زائدة من النموذج
            return {"ok": False, "error": f"وسائط غير صحيحة للأداة {name}: {e}"}
        except Exception as e:  # noqa: BLE001 — لا نُسقط المحادثة بسبب خطأ أداة
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ------------------------------------------------------------------ نسخة احتياطية قبل الكتابة
    def _backup(self) -> Path | None:
        if not self.home:
            return None
        d = self.home / "backups"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"قبل-المساعد-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
        path.write_text(json.dumps(self.led.export_backup(), ensure_ascii=False), encoding="utf-8")
        for old in sorted(d.glob("قبل-المساعد-*.json"))[:-30]:  # آخر 30 نسخة فقط
            old.unlink(missing_ok=True)
        return path

    # ------------------------------------------------------------------ مساعدات
    def _find_item(self, ref: str):
        ref = (ref or "").strip()
        it = next((i for i in self.led.inventory if i.id == ref), None)
        if it:
            return it
        it = next((i for i in self.led.inventory if i.code and i.code.strip().lower() == ref.lower()), None)
        if it:
            return it
        found = search_inventory(self.led.inventory, ref)
        if len(found) == 1:
            return found[0]
        raise LedgerError(f"لم أجد صنفاً واحداً محدداً لـ «{ref}» ({len(found)} نتيجة) — استخدم search_items ثم مرّر id.")

    def _find_customer(self, name: str):
        c = self.led._find_customer(name)
        if c:
            return c
        found = [c for c in self.led.customers if fuzzy_match(c.name, name)]
        if len(found) == 1:
            return found[0]
        raise LedgerError(f"لم أجد زبوناً واحداً محدداً باسم «{name}» ({len(found)} نتيجة).")

    @staticmethod
    def _item_row(i) -> dict[str, Any]:
        return {"id": i.id, "name": i.name, "code": i.code, "stock": i.stock, "cost": i.cost,
                "retail_price": i.price, "wholesale_price": i.price_wholesale,
                "distribution_price": i.price_distribution}

    # ------------------------------------------------------------------ أدوات القراءة
    async def business_summary(self):
        led = self.led
        rep = profit_report(led.invoices, led.expenses)
        return {
            "business": led.settings.get("businessName"), "items": len(led.inventory),
            "total_pieces": sum(i.stock for i in led.inventory), "inventory_capital": led.capital(),
            "invoices": len(led.invoices), "customers": len(led.customers), "suppliers": len(led.suppliers),
            "owed_by_customers": sum((c.balance for c in led.customers), Decimal(0)),
            "owed_to_suppliers": sum((s.balance for s in led.suppliers), Decimal(0)),
            "low_stock_items": len(low_stock_items(led.inventory)),
            "today_revenue": rep.today_revenue, "today_profit": rep.today_profit,
            "total_revenue": rep.total_revenue, "total_profit": rep.total_profit,
            "total_expenses": rep.total_expenses, "net_profit": rep.total_net_profit,
        }

    async def search_items(self, query: str = "", limit: int = 25):
        found = search_inventory(self.led.inventory, query)
        return {"total_matches": len(found), "items": [self._item_row(i) for i in found[: max(1, int(limit))]]}

    async def list_low_stock(self, threshold: int = 3):
        return [self._item_row(i) for i in low_stock_items(self.led.inventory, int(threshold))]

    async def list_customers(self, query: str = "", only_debtors: bool = False, limit: int = 50):
        rows = [c for c in self.led.customers if (not query or fuzzy_match(c.name, query) or fuzzy_match(c.phone, query))
                and (not only_debtors or c.balance > 0)]
        rows.sort(key=lambda c: c.balance, reverse=True)
        return {"total_matches": len(rows),
                "customers": [{"id": c.id, "name": c.name, "phone": c.phone, "balance": c.balance} for c in rows[: int(limit)]]}

    async def get_customer_statement(self, name: str, limit: int = 20):
        c = self._find_customer(name)
        st = customer_statement(c, self.led.invoices, oldest_first=False, limit=int(limit))
        return {"customer": c.name, "phone": c.phone, "balance": c.balance, "statement": st.__dict__
                if hasattr(st, "__dict__") else str(st)}

    async def list_invoices(self, query: str = "", date_from: str = "", date_to: str = "", limit: int = 20):
        out = []
        for inv in self.led.invoices:
            day = (inv.date or "")[:10]
            if query and not (fuzzy_match(inv.number, query) or fuzzy_match(inv.customer, query)):
                continue
            if date_from and day < date_from:
                continue
            if date_to and day > date_to:
                continue
            out.append({"number": inv.number, "date": inv.date, "customer": inv.customer, "total": inv.total,
                        "paid": inv.paid, "remaining": inv.remaining, "lines": len(inv.items)})
        return {"total_matches": len(out), "invoices": out[: int(limit)]}

    async def get_invoice(self, number: str):
        inv = next((i for i in self.led.invoices if i.number.lower() == number.strip().lower()), None)
        if not inv:
            raise LedgerError("الفاتورة غير موجودة")
        return inv

    async def list_purchases(self, limit: int = 20):
        return [{"number": p.number, "supplier": p.supplier, "date": p.date, "subtotal": p.subtotal,
                 "paid": p.paid, "remaining": p.remaining, "lines": len(p.items)} for p in self.led.purchases[: int(limit)]]

    async def list_suppliers(self):
        return [{"id": s.id, "name": s.name, "balance": s.balance} for s in self.led.suppliers]

    async def list_expenses(self, limit: int = 30):
        return [{"category": EXPENSE_CATEGORIES.get(e.category, e.category), "amount": e.amount,
                 "note": e.note, "period": e.period, "date": e.date} for e in self.led.expenses[: int(limit)]]

    async def list_vouchers(self, limit: int = 30):
        return [{"number": v.number, "type": v.type, "person": v.person, "amount": v.amount, "date": v.date}
                for v in self.led.vouchers[: int(limit)]]

    async def best_sellers(self, n: int = 10):
        return [{"name": t.name, "qty": t.qty} for t in top_selling(self.led.invoices, int(n))]

    async def profit_overview(self):
        rep = profit_report(self.led.invoices, self.led.expenses)
        return {"months": {k: {"revenue": b.revenue, "profit": b.profit, "expenses": b.expenses, "net": b.net_profit,
                               "invoices": b.count} for k, b in sorted(rep.months.items())},
                "total_revenue": rep.total_revenue, "total_profit": rep.total_profit,
                "total_expenses": rep.total_expenses, "net_profit": rep.total_net_profit}

    async def list_data_keys(self):
        if self.store is not None and hasattr(self.store, "keys"):
            return {"source": "supabase.app_data", "keys": await self.store.keys()}
        return {"source": "local", "keys": list(self.led.export_backup().keys())}

    async def read_data_key(self, key: str, max_chars: int = READ_LIMIT_CHARS):
        """قراءة خام لأي سجل في app_data (قراءة فقط)."""
        if self.store is not None:
            value, updated = await self.store.get(key)
        else:
            value, updated = self.led.export_backup().get(key), None
        if value is None:
            raise LedgerError(f"لا يوجد سجل بالمفتاح «{key}»")
        text = json.dumps(value, ensure_ascii=False)
        cap = max(1000, min(int(max_chars), READ_LIMIT_CHARS))
        return {"key": key, "updated_at": updated, "size_chars": len(text), "truncated": len(text) > cap,
                "json": text[:cap]}

    # ---- قراءة ملفات البرنامج (للقراءة فقط، دون الأسرار) ----
    def _safe_path(self, rel: str) -> Path:
        p = (PACKAGE_DIR / rel).resolve()
        if PACKAGE_DIR not in p.parents and p != PACKAGE_DIR:
            raise LedgerError("المسار خارج مجلد البرنامج")
        if p.name.startswith(".env") or p.suffix in (".pyc", ".jpeg", ".jpg", ".png"):
            raise LedgerError("هذا الملف غير متاح للقراءة")
        return p

    async def list_project_files(self):
        files = [str(p.relative_to(PACKAGE_DIR)) for p in sorted(PACKAGE_DIR.rglob("*.py"))
                 if "__pycache__" not in p.parts]
        return {"files": files}

    async def read_project_file(self, path: str, max_chars: int = READ_LIMIT_CHARS):
        p = self._safe_path(path)
        if not p.is_file():
            raise LedgerError("الملف غير موجود")
        text = p.read_text(encoding="utf-8", errors="replace")
        cap = max(1000, min(int(max_chars), READ_LIMIT_CHARS))
        return {"path": path, "size_chars": len(text), "truncated": len(text) > cap, "content": text[:cap]}

    async def google_search(self, query: str, max_results: int = 6):
        """بحث ويب. الأولوية لبحث جوجل الرسمي عبر Gemini (مستقل عن حجب المحركات)، ثم قراءة محركات البحث مباشرة. للقراءة فقط."""
        errors: list[str] = []
        provider = getattr(self, "search_provider", None)
        if provider is not None:
            try:
                r = await provider(query)
                return {"engine": "gemini-google", "query": query, "answer": r.get("answer", ""),
                        "sources": r.get("sources", []), "search_queries": r.get("queries", []),
                        "note": "الخلاصة من بحث جوجل الفعلي؛ اذكر المصادر المرفقة، وما لم يرد فيها اكتب «غير متوفر»."}
            except Exception as e:  # noqa: BLE001 — نكمل للمحركات المباشرة
                errors.append(f"بحث Gemini: {e}")
        try:
            res = await asyncio.to_thread(search_web, query, int(max_results or 6))
            if errors:
                res["note"] = "تعذّر بحث Gemini فاستُعملت محركات الويب المباشرة: " + errors[0][:120]
            return res
        except SearchError as e:
            errors.append(str(e))
        raise LedgerError("تعذّر البحث على الويب بكل الطرق: " + " | ".join(errors)[:500]
                          + " — أخبر المالك بالسبب بدقة (مفتاح Gemini/الاتصال/حجب المحركات)، واعرض عليه فتح الموقع بنفسه عبر open_catalog إن كان كتالوجاً.")

    async def read_webpage(self, url: str, max_chars: int = 6000):
        """يقرأ نص صفحة ويب عامة (http/https) لاستخراج مواصفات منتج من صفحة وجدتها بالبحث. للقراءة فقط."""
        from . import catalogs
        import ipaddress
        import socket
        import urllib.parse as _up
        u = _up.urlparse((url or "").strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            raise LedgerError("الرابط يجب أن يبدأ بـ http:// أو https://")
        try:                                  # لا نقرأ الشبكة المحلية/الداخلية (حماية من تعليمات خبيثة داخل صفحات)
            for info in socket.getaddrinfo(u.hostname, None):
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    raise LedgerError("لا يمكن قراءة عناوين الشبكة المحلية.")
        except socket.gaierror:
            raise LedgerError("تعذّر الوصول إلى هذا الموقع (الاسم غير موجود).")
        cap = max(1000, min(int(max_chars or 6000), 20000))
        try:
            raw = await asyncio.to_thread(catalogs.fetch_url, u.geturl())
        except catalogs.CatalogError as e:
            raise LedgerError(str(e)) from e
        text = catalogs.html_to_text(raw, cap * 2)
        return {"url": u.geturl(), "truncated": len(text) > cap, "content": text[:cap]}

    async def search_catalogs(self, query: str, sources: str = ""):
        """بحث في كتالوجات الفلاتر على الإنترنت (Sardes, MANN, Delsa, FMI …) باسم الفلتر أو رقمه أو مرجعه. للقراءة فقط."""
        from . import catalogs
        only = [x.strip().lower() for x in re.split(r"[,،]", sources or "") if x.strip()] or None
        try:
            res = await catalogs.search_catalogs(query, catalogs.merged_sources(self.led.settings), only,
                                                 (self.home / "catalog_debug") if self.home else None)
        except catalogs.CatalogError as e:
            raise LedgerError(str(e)) from e
        shown: list[dict[str, Any]] = []
        for r in res.get("sources", []):
            for im in r.pop("_images", None) or []:      # الصور للمالك فقط (bytes)؛ النموذج يعرف فقط أنها أُرسلت
                shown.append({"source": r.get("name", ""), "label": im.get("label", ""), "data": im["data"],
                              "keys": im.get("keys") or []})
            if r.get("images_found"):
                r["images_note"] = f"أُرسلت {r['images_found']} صورة للمالك وتظهر في المحادثة تحت ردّك — لا تصفها من عندك."
        if shown:
            self.catalog_pictures.append(shown)
            done, skipped = await self._auto_attach(shown)
            if done:
                res["auto_attached"] = done
                res["auto_attach_note"] = ("أُضيفت الصور تلقائياً لهذه الأصناف (تطابق كود تام وكانت بلا صورة): "
                                           + "، ".join(f"{d['item']} ({d['code']})" for d in done) + " — أخبر المالك بذلك.")
            if skipped:
                res["auto_attach_skipped"] = skipped
        return res

    # ------------------------------------------------------------------ إضافة صور الكتالوج للمخزون تلقائياً
    def _auto_enabled(self) -> bool:
        v = (self.led.settings or {}).get("autoCatalogImages")
        return v is not False and str(v).lower() not in ("false", "0", "off")

    def _match_item(self, keys: list[str]):
        """الصنف الوحيد الذي يطابق كوده (مطبَّعاً) إحدى قيم سجل الكتالوج تماماً؛ وإلا None (لا تخمين)."""
        from .fmi import norm_key
        ks = set(keys or [])
        hits = [i for i in self.led.inventory if i.code and len(norm_key(i.code)) >= 3 and norm_key(i.code) in ks]
        return hits[0] if len(hits) == 1 else None

    async def _attach_bytes(self, item, data: bytes) -> None:
        from ..data.images import ImageError, compress_image, fingerprint
        store = self._image_store()
        if store is None:
            raise LedgerError("مخزن الصور غير مهيأ في هذه الجلسة.")
        try:
            jpeg = await asyncio.to_thread(compress_image, data)
        except ImageError as e:
            raise LedgerError(str(e)) from e
        await store.put(item.id, jpeg)
        await self.led.set_item_image(item.id, fingerprint(jpeg))

    def _has_image(self, item) -> bool:
        store = self._image_store()
        return bool(item.img) or (store is not None and store.get_local(item.id) is not None)

    async def _auto_attach(self, shown: list[dict[str, Any]]):
        """لكل صورة كتالوج: إن طابق كود سجلها كود صنف واحد في المخزون وكان بلا صورة تُضاف له فوراً.
        لا تستبدل صورة موجودة أبداً، ولا تخمّن عند تعدد المطابقات."""
        if not self._auto_enabled():
            return [], []
        done, skipped = [], []
        for im in shown:
            it = self._match_item(im.get("keys") or [])
            if it is None:
                continue
            if self._has_image(it):
                skipped.append({"item": it.name, "code": it.code, "reason": "للصنف صورة بالفعل — لم تُستبدل"})
                continue
            try:
                await self._attach_bytes(it, im["data"])
            except Exception as e:  # noqa: BLE001 — فشل صورة لا يُفشل البحث
                skipped.append({"item": it.name, "code": it.code, "reason": str(e)})
                continue
            done.append({"item": it.name, "code": it.code})
            self.notices.append(f"✅ أضفتُ صورة الكتالوج تلقائياً للصنف «{it.name}» ({it.code}).")
        return done, skipped

    async def fill_missing_images(self, limit: int = 12, in_stock_only: bool = False):
        """يجلب من كتالوج FMI صور الأصناف الناقصة (بتطابق كود تام) ويضيفها للمخزون دفعة واحدة."""
        import asyncio as _a
        from . import catalogs, fmi
        srcs = [x for x in catalogs.merged_sources(self.led.settings)
                if x.mode == "filemaker" and x.enabled and x.user and x.password]
        if not srcs:
            raise LedgerError("هذه الأداة تحتاج حساب FileMaker في الإعدادات ← كتالوجات الفلاتر ← FMI (مستخدم وكلمة مرور).")
        src = srcs[0]
        if self._image_store() is None:
            raise LedgerError("مخزن الصور غير مهيأ في هذه الجلسة.")
        limit = max(1, min(int(limit or 12), 40))
        cands = [i for i in self.led.inventory
                 if i.code and not self._has_image(i) and (not in_stock_only or i.stock > 0)]
        cands.sort(key=lambda i: -i.stock)
        if not cands:
            return {"checked": 0, "added": [], "note": "كل الأصناف التي لها كود عندها صور بالفعل."}
        batch = cands[:limit]
        try:
            found = await _a.to_thread(fmi.fetch_images_for_codes, src.url, src.user, src.password,
                                       [i.code for i in batch], src.layout, src.db)
        except fmi.FmiError as e:
            raise LedgerError(str(e)) from e
        added, missing, gallery = [], [], []
        for it in batch:
            im = found.get(it.code)
            if not im:
                missing.append(f"{it.name} ({it.code})")
                continue
            try:
                await self._attach_bytes(it, im["data"])
            except LedgerError as e:
                missing.append(f"{it.name} ({it.code}) — {e}")
                continue
            added.append({"item": it.name, "code": it.code})
            gallery.append({"source": src.name, "label": it.name, "data": im["data"]})
        if gallery:
            self.catalog_pictures.append(gallery)
            self.notices.append(f"✅ أضفتُ تلقائياً صور {len(added)} صنف من كتالوج {src.name}.")
        return {"checked": len(batch), "added": added, "not_found_in_catalog": missing,
                "remaining_without_image": len(cands) - len(batch),
                "note": "المطابقة بالكود التام فقط. الصور المضافة تظهر للمالك في المحادثة. إن بقي أصناف فاعرض عليه تكرار الأمر."}

    async def attach_catalog_image(self, item: str, picture_no: int = 1):
        """يضع صورة من آخر صور كتالوج أرسلها المساعد كصورة لصنف في المخزون (بموافقة المالك)."""
        from ..data.images import ImageError, compress_image, fingerprint
        flat = [im for group in self.catalog_pictures for im in group]
        if not flat:
            raise LedgerError("لا توجد صور كتالوج في هذه المحادثة — ابحث أولاً بـ search_catalogs ثم اطلب إضافة الصورة.")
        n = int(picture_no or 1)
        if not 1 <= n <= len(flat):
            raise LedgerError(f"رقم الصورة يجب أن يكون بين 1 و{len(flat)}")
        found = search_inventory(self.led.inventory, item)
        if not found:
            raise LedgerError(f"لا يوجد صنف مطابق لـ «{item}» في المخزون")
        exact = [i for i in found if i.id == item or (i.code and i.code.lower() == str(item).strip().lower())]
        if len(exact) != 1 and len(found) > 1:
            raise LedgerError("أكثر من صنف يطابق: " + "، ".join(f"{i.name} ({i.code or i.id})" for i in found[:5])
                              + " — حدّد الصنف بكوده أو معرّفه.")
        target = exact[0] if exact else found[0]
        await self._attach_bytes(target, flat[n - 1]["data"])
        return {"item": target.name, "id": target.id, "picture_no": n, "label": flat[n - 1].get("label", "")}

    async def open_catalog(self, source: str = "fmi"):
        """يعرض للمالك زر «فتح الموقع» لكتالوج (مثل FMI) ليتصفحه بنفسه. للقراءة فقط."""
        from . import catalogs
        key = (source or "fmi").strip().lower()
        srcs = catalogs.merged_sources(self.led.settings)
        src = next((x for x in srcs if x.id == key or x.name.lower() == key or key in x.id), None)
        if src is None or not src.url:
            raise LedgerError("كتالوج غير معروف. المتاح: " + "، ".join(f"{x.id} ({x.name})" for x in srcs))
        self.links.append({"name": src.name, "url": src.url})
        return {"opened": True, "name": src.name, "url": src.url,
                "note": "ظهر للمالك زر لفتح الموقع. WebDirect يحتاج متصفحاً؛ المساعد لا يقرأ صفحته مباشرة بلا حساب FileMaker."}

    # ------------------------------------------------------------------ صور يرسلها المالك + ملفات Excel
    async def clean_image_background(self, which: int = 1):
        """يُصلح صورة أرسلها المالك: خلفية بيضاء ناصعة (بلون البرنامج) والصنف كاملاً في مربع."""
        from ..data.images import clean_product_photo
        if not self.user_images:
            raise LedgerError("لا توجد صورة مرسلة من المالك لأعالجها — اطلب منه رفع صورة بزر «رفع صورة».")
        idx = max(1, int(which or 1)) - 1
        if idx >= len(self.user_images):
            raise LedgerError(f"المرسَل {len(self.user_images)} صورة فقط.")
        out = await asyncio.to_thread(clean_product_photo, self.user_images[idx])
        self.cleaned_image = out
        self.catalog_pictures.append([{"source": "المساعد", "label": "الصورة بعد التنظيف (خلفية بيضاء)", "data": out, "keys": []}])
        return {"done": True, "note": "ظهرت الصورة المعالجة للمالك في المحادثة (زر تحميل/إضافة لصنف). "
                                      "لإضافتها لصنف استخدم attach_user_image بعد موافقته."}

    async def attach_user_image(self, item: str, cleaned: bool = True):
        """يضع صورة المالك (المنظَّفة افتراضياً) كصورة صنف في المخزون."""
        from ..data.images import ImageError, fingerprint, prepare_product_image
        it = self._find_item(item)
        data = (self.cleaned_image if cleaned and self.cleaned_image else (self.user_images[0] if self.user_images else None))
        if not data:
            raise LedgerError("لا توجد صورة لإضافتها — اطلب من المالك رفع صورة.")
        store = self._image_store()
        if store is None:
            raise LedgerError("مخزن الصور غير مهيأ في هذه الجلسة.")
        try:
            small, full = await asyncio.to_thread(prepare_product_image, data)
        except ImageError as e:
            raise LedgerError(str(e)) from e
        await store.put(it.id, small, full=full)
        await self.led.set_item_image(it.id, fingerprint(small))
        return {"item": it.name, "code": it.code, "image_attached": True}

    def _excel(self, file_name: str) -> dict:
        if not self.excel_files:
            raise LedgerError("لم يرفع المالك ملف Excel بعد.")
        if file_name and file_name in self.excel_files:
            return self.excel_files[file_name]
        if file_name:
            for k, v in self.excel_files.items():
                if file_name.lower() in k.lower():
                    return v
        return list(self.excel_files.values())[-1]

    async def read_excel_rows(self, file_name: str = "", start: int = 0, limit: int = 40):
        t = self._excel(file_name)
        start = max(0, int(start or 0))
        limit = max(1, min(int(limit or 40), 100))
        return {"headers": t["headers"], "total_rows": len(t["rows"]), "start": start,
                "rows": t["rows"][start:start + limit]}

    async def import_excel_items(self, name_col: str, cost_col: str, price_col: str, file_name: str = "",
                                 stock_col: str = "", code_col: str = "", wholesale_col: str = "",
                                 distribution_col: str = "", skip_existing: bool = True):
        """يسجّل أصنافاً دفعة واحدة من ملف Excel المرفوع (الأعمدة تُحدَّد بأسماء عناوينها)."""
        from ..core.money import parse_num
        t = self._excel(file_name)
        heads = t["headers"]

        def col(label: str, required: bool = False):
            label = (label or "").strip()
            if not label:
                if required:
                    raise LedgerError("حدّد عمود الاسم والتكلفة والسعر.")
                return None
            for i, h in enumerate(heads):
                if h.strip() == label:
                    return i
            for i, h in enumerate(heads):
                if label.lower() in h.lower():
                    return i
            raise LedgerError(f"لا يوجد عمود «{label}». الأعمدة: " + "، ".join(heads))
        ci = {k: col(v, k in ("name", "cost", "price")) for k, v in (
            ("name", name_col), ("cost", cost_col), ("price", price_col), ("stock", stock_col),
            ("code", code_col), ("wholesale", wholesale_col), ("distribution", distribution_col))}
        existing_codes = {(i.code or "").strip().lower() for i in self.led.inventory if i.code}
        existing_names = {i.name.strip().lower() for i in self.led.inventory}
        added, skipped, failed = [], [], []

        def num(r, k):
            j = ci[k]
            return None if j is None or not r[j].strip() else parse_num(r[j])
        for n, r in enumerate(t["rows"], 2):
            name = r[ci["name"]].strip()
            if not name:
                continue
            code = r[ci["code"]].strip() if ci["code"] is not None else ""
            if skip_existing and ((code and code.lower() in existing_codes) or name.lower() in existing_names):
                skipped.append(name)
                continue
            try:
                it = await self.led.add_item(name, code, num(r, "cost") or 0, num(r, "price") or 0,
                                             int(num(r, "stock") or 0), num(r, "wholesale"), num(r, "distribution"))
            except Exception as e:  # noqa: BLE001 — صف سيّئ لا يوقف الباقي
                failed.append(f"صف {n} ({name}): {e}")
                continue
            existing_names.add(name.lower())
            if code:
                existing_codes.add(code.lower())
            added.append(it.name)
        return {"added": len(added), "skipped_existing": len(skipped), "failed": failed[:10],
                "examples": added[:5], "skipped_examples": skipped[:5]}

    # ------------------------------------------------------------------ أدوات الكتابة (بموافقة)
    async def add_item(self, name: str, cost: float, price: float, stock: int = 0, code: str = "",
                       price_wholesale: float | None = None, price_distribution: float | None = None):
        it = await self.led.add_item(name, code or "", cost, price, stock, price_wholesale, price_distribution)
        return self._item_row(it)

    async def edit_item(self, item: str, name: str | None = None, code: str | None = None, stock: int | None = None):
        it = self._find_item(item)
        it = await self.led.edit_item(it.id, name if name is not None else it.name,
                                      code if code is not None else it.code, stock if stock is not None else it.stock)
        return self._item_row(it)

    async def edit_item_prices(self, item: str, cost: float | None = None, retail_price: float | None = None,
                               wholesale_price: float | None = None, distribution_price: float | None = None):
        it = self._find_item(item)
        pick = lambda new, old: old if new is None else new  # noqa: E731
        it = await self.led.edit_item_pricing(it.id, pick(cost, it.cost), pick(wholesale_price, it.price_wholesale),
                                              pick(distribution_price, it.price_distribution), pick(retail_price, it.price))
        return self._item_row(it)

    async def restock_item(self, code: str, qty: int):
        it = await self.led.restock_by_code(code, int(qty))
        return self._item_row(it)

    async def add_customer(self, name: str, phone: str = ""):
        c = await self.led.add_customer(name, phone)
        return {"id": c.id, "name": c.name, "phone": c.phone}

    async def record_customer_payment(self, customer_name: str, amount: float):
        c = self._find_customer(customer_name)
        v = await self.led.record_customer_payment(c.id, amount)
        return {"voucher": v.number, "customer": c.name, "paid": v.amount, "remaining_balance": v.remaining_balance}

    async def add_expense(self, category: str, amount: float, note: str = "", period: str = ""):
        ex = await self.led.add_expense(category, amount, note, period)
        return {"category": ex.category, "amount": ex.amount, "note": ex.note}

    # ---- صور الأصناف (يرسلها المساعد داخل المحادثة) ----
    def _image_store(self):
        if self._images is None and self.home:
            from ..data.images import ImageStore
            self._images = ImageStore(self.store, self.home / "images")
        return self._images

    async def show_item_images(self, query: str = "", item_ids: str = "", limit: int = 12):
        """يعرض للمالك داخل المحادثة صور أصناف المخزون المحفوظة (من الكاش أو السحابة)."""
        store = self._image_store()
        if store is None:
            raise LedgerError("مخزن الصور غير مهيأ في هذه الجلسة.")
        limit = max(1, min(int(limit or 12), MAX_PICTURES))
        ids = [x.strip() for x in re.split(r"[,\s،]+", str(item_ids or "")) if x.strip()]
        if ids:
            found = [i for i in self.led.inventory if i.id in ids]
        elif (query or "").strip():
            found = search_inventory(self.led.inventory, query)
        else:
            raise LedgerError("حدّد الصنف: query (اسم أو كود) أو item_ids.")
        if not found:
            raise LedgerError(f"لا يوجد صنف مطابق لـ «{query or item_ids}».")
        candidates = found[: limit * 3]                 # نبحث بين المطابقات الأولى عمّن لديه صورة فعلاً
        missing_local = [i.id for i in candidates if store.get_local(i.id) is None]
        if missing_local:
            await store.fetch_missing(missing_local)
        shown, without = [], []
        for it in candidates:
            if store.get_local(it.id) is not None:
                if len(shown) < limit:
                    shown.append({"id": it.id, "name": it.name, "code": it.code, "stock": it.stock, "price": it.price})
            else:
                without.append(it.name)
        if not shown:
            raise LedgerError("لا توجد صورة محفوظة لأي من الأصناف المطابقة — تُضاف الصورة من «المخزون ← تعديل».")
        self.pictures.append(shown)
        return {"shown": [{k: v for k, v in x.items() if k != "id"} for x in shown],
                "total_matches": len(found), "matches_without_image": without[:10],
                "note": "ظهرت الصور للمالك في المحادثة. لا تصفها ولا تكتب روابط؛ اذكر فقط ما يلزم (الاسم/الكود/المخزون/السعر)."}

    # ---- تصدير Excel ----
    def _export_dir(self) -> Path:
        d = (self.home / "exports") if self.home else Path.cwd() / "exports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _finish_export(self, filename: str, sheets: list[excel.Sheet]) -> dict[str, Any]:
        data = excel.build_workbook(sheets, self.led.settings.get("businessName", ""))
        base = excel.safe_filename(filename)
        path = self._export_dir() / base
        n = 0
        while path.exists():                    # لا نكتب فوق ملف سابق (وقد يكون مفتوحاً في إكسل)
            n += 1
            path = path.with_name(f"{Path(base).stem}-{datetime.now():%H%M%S}" + (f"-{n}" if n > 1 else "") + Path(base).suffix)
        path.write_bytes(data)
        info = {"file": path.name, "path": str(path), "sheets": [s.name for s in sheets],
                "rows": sum(len(s.rows) for s in sheets), "size_kb": round(len(data) / 1024, 1)}
        self.exports.append(info)
        return {**info, "note": "تم إنشاء الملف وسيظهر للمستخدم في المحادثة بزرّي فتح وحفظ. لا تكتب مساره في ردك."}

    async def export_excel(self, filename: str, sheets: Any):
        """جداول ينشئها المساعد بنفسه (تحليل/مقارنة/توقع) ← ملف Excel."""
        try:
            built = excel.normalize_sheets(sheets)
        except ValueError as e:
            raise LedgerError(str(e)) from e
        if not built:
            raise LedgerError("لا توجد جداول في الطلب — مرّر sheets بها headers وrows.")
        return self._finish_export(filename, built)

    EXPORT_DATASETS = {
        "inventory": "المخزون الكامل (الكمية والتكلفة والأسعار)", "low_stock": "الأصناف منخفضة المخزون",
        "customers": "العملاء وأرصدتهم", "debtors": "العملاء المدينون فقط", "suppliers": "الموردون وما علينا لهم",
        "invoices": "فواتير البيع (مع التكلفة والربح)", "invoice_items": "بنود فواتير البيع (كل صنف في سطر)",
        "purchases": "فواتير الشراء", "expenses": "المصاريف", "vouchers": "سندات القبض والدفع",
        "profit_monthly": "الأرباح شهرياً", "best_sellers": "الأكثر مبيعاً",
    }

    def _dataset_sheet(self, dataset: str, query: str, date_from: str, date_to: str) -> excel.Sheet:
        led = self.led
        d2 = lambda v: float(to_decimal(v))  # noqa: E731
        in_range = lambda day: (not date_from or day >= date_from) and (not date_to or day <= date_to)  # noqa: E731
        if dataset in ("inventory", "low_stock"):
            items = low_stock_items(led.inventory) if dataset == "low_stock" else search_inventory(led.inventory, query)
            return excel.Sheet("المخزون" if dataset == "inventory" else "مخزون منخفض",
                               ["الصنف", "الكود", "الكمية", "رأس المال", "سعر المفرق", "سعر الجملة", "سعر التوزيع", "قيمة المخزون"],
                               [[i.name, i.code, i.stock, d2(i.cost), d2(i.price), d2(i.price_wholesale),
                                 d2(i.price_distribution), d2(i.cost * i.stock)] for i in items],
                               money_cols={3, 4, 5, 6, 7}, totals=True)
        if dataset in ("customers", "debtors"):
            rows = [c for c in led.customers if (not query or fuzzy_match(c.name, query) or fuzzy_match(c.phone, query))
                    and (dataset == "customers" or c.balance > 0)]
            rows.sort(key=lambda c: -c.balance)
            return excel.Sheet("العملاء" if dataset == "customers" else "المدينون", ["الاسم", "الهاتف", "الدين عليه"],
                               [[c.name, c.phone, d2(c.balance)] for c in rows], money_cols={2}, totals=True)
        if dataset == "suppliers":
            return excel.Sheet("الموردون", ["المورد", "الدين علينا"], [[s.name, d2(s.balance)] for s in led.suppliers],
                               money_cols={1}, totals=True)
        if dataset == "invoices":
            rows = []
            for inv in led.invoices:
                day = (inv.date or "")[:10]
                if not in_range(day) or (query and not (fuzzy_match(inv.number, query) or fuzzy_match(inv.customer, query))):
                    continue
                rows.append([inv.number, day, inv.customer, d2(inv.total), d2(inv.paid), d2(inv.remaining),
                             d2(invoice_cost(inv)), d2(inv.total - invoice_cost(inv))])
            return excel.Sheet("الفواتير", ["الرقم", "التاريخ", "الزبون", "الإجمالي", "المدفوع", "المتبقي", "التكلفة", "الربح"],
                               rows, money_cols={3, 4, 5, 6, 7}, totals=True)
        if dataset == "invoice_items":
            rows = []
            for inv in led.invoices:
                day = (inv.date or "")[:10]
                if not in_range(day) or (query and not (fuzzy_match(inv.number, query) or fuzzy_match(inv.customer, query))):
                    continue
                for l in inv.items:
                    rows.append([inv.number, day, inv.customer, l.name, l.code, l.qty, d2(l.price), d2(l.cost),
                                 d2(l.price * l.qty), d2((l.price - l.cost) * l.qty)])
            return excel.Sheet("بنود الفواتير", ["الفاتورة", "التاريخ", "الزبون", "الصنف", "الكود", "الكمية", "سعر البيع",
                                                 "التكلفة", "الإجمالي", "الربح"], rows, money_cols={6, 7, 8, 9}, totals=True)
        if dataset == "purchases":
            rows = [[p.number, (p.date or "")[:10], p.supplier, d2(p.subtotal), d2(p.paid), d2(p.remaining)]
                    for p in led.purchases if in_range((p.date or "")[:10])
                    and (not query or fuzzy_match(p.supplier, query) or fuzzy_match(p.number, query))]
            return excel.Sheet("المشتريات", ["الرقم", "التاريخ", "المورد", "الإجمالي", "المدفوع", "المتبقي"], rows,
                               money_cols={3, 4, 5}, totals=True)
        if dataset == "expenses":
            rows = [[(e.date or "")[:10], EXPENSE_CATEGORIES.get(e.category, e.category), e.note, e.period, d2(e.amount)]
                    for e in led.expenses if in_range((e.date or "")[:10])]
            return excel.Sheet("المصاريف", ["التاريخ", "الفئة", "ملاحظة", "الفترة", "المبلغ"], rows, money_cols={4}, totals=True)
        if dataset == "vouchers":
            rows = [[v.number, (v.date or "")[:10], "قبض" if v.type == "receipt" else "دفع", v.person, d2(v.amount), v.note]
                    for v in led.vouchers if in_range((v.date or "")[:10])
                    and (not query or fuzzy_match(v.person, query))]
            return excel.Sheet("السندات", ["الرقم", "التاريخ", "النوع", "الاسم", "المبلغ", "ملاحظة"], rows, money_cols={4})
        if dataset == "profit_monthly":
            rep = profit_report(led.invoices, led.expenses)
            rows = [[k, b.count, d2(b.revenue), d2(b.profit), d2(b.expenses), d2(b.net_profit)]
                    for k, b in sorted(rep.months.items())]
            return excel.Sheet("الأرباح الشهرية", ["الشهر", "عدد الفواتير", "المبيعات", "الربح", "المصاريف", "الصافي"],
                               rows, money_cols={2, 3, 4, 5}, totals=True)
        if dataset == "best_sellers":
            return excel.Sheet("الأكثر مبيعاً", ["الصنف", "الكمية المباعة"],
                               [[t.name, t.qty] for t in top_selling(led.invoices, 50)])
        raise LedgerError(f"مجموعة بيانات غير معروفة: {dataset}. المتاح: " + "، ".join(self.EXPORT_DATASETS))

    async def export_dataset(self, dataset: str, query: str = "", date_from: str = "", date_to: str = "",
                             filename: str = ""):
        """تصدير بيانات البرنامج الحقيقية كاملة إلى Excel دون المرور بالنموذج (دقيق وبلا حد للصفوف)."""
        sh = self._dataset_sheet(dataset, query, date_from, date_to)
        if not sh.rows:
            raise LedgerError("لا توجد بيانات مطابقة لهذا التصدير.")
        return self._finish_export(filename or f"{sh.name}-{datetime.now():%Y-%m-%d}", [sh])

    async def save_proposal(self, title: str, content: str):
        """يحفظ اقتراح ميزة/كود كملف في مجلد proposals لتراجعه (لا يمس ملفات البرنامج)."""
        if not self.home:
            raise LedgerError("مجلد الحفظ غير مهيأ")
        d = self.home / "proposals"
        d.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w\u0600-\u06FF-]+", "-", title).strip("-")[:60] or "proposal"
        path = d / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.md"
        path.write_text(f"# {title}\n\n{content}\n", encoding="utf-8")
        return {"saved_to": str(path)}


    # ------------------------------------------------------------------ فواتير وسندات (بموافقة)
    TIERS = {"retail": "مفرق", "wholesale": "جملة", "distribution": "توزيع"}

    @staticmethod
    def _money_arg(v: Any, label: str, allow_none: bool = False) -> Decimal | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            if allow_none:
                return None
            raise LedgerError(f"{label}: قيمة مطلوبة")
        try:
            d = q2(to_decimal(v))
        except Exception as e:  # noqa: BLE001
            raise LedgerError(f"{label}: «{v}» ليس رقماً صحيحاً") from e
        if d < 0:
            raise LedgerError(f"{label}: لا يجوز أن يكون سالباً")
        return d

    def _plan_invoice(self, items: list[dict], extra_lines: list[dict] | None, price_tier: str) -> tuple[InvoiceDraft, list[dict]]:
        """يحوّل طلب المساعد إلى مسودة فاتورة بعد التحقق الصارم (صنف واحد محدد، كمية صحيحة، مخزون كافٍ)."""
        if price_tier not in self.TIERS:
            raise LedgerError("price_tier يجب أن يكون retail أو wholesale أو distribution")
        if not items and not extra_lines:
            raise LedgerError("الفاتورة بلا بنود — مرّر صنفاً واحداً على الأقل")
        d, rows = InvoiceDraft(), []
        wanted: dict[str, int] = {}
        for raw in items or []:
            it = self._find_item(str(raw.get("item", "")))
            try:
                qty = int(raw.get("qty", 0))
            except (TypeError, ValueError) as e:
                raise LedgerError(f"كمية «{it.name}» غير صحيحة") from e
            if qty <= 0:
                raise LedgerError(f"كمية «{it.name}» يجب أن تكون أكبر من صفر")
            wanted[it.id] = wanted.get(it.id, 0) + qty
            if wanted[it.id] > it.stock:
                raise LedgerError(f"المتوفر من «{it.name}» {it.stock} فقط والمطلوب {wanted[it.id]} — لم تُنشأ الفاتورة")
            tier = str(raw.get("tier") or price_tier)
            if tier not in self.TIERS:
                raise LedgerError(f"شريحة سعر غير معروفة للصنف «{it.name}»: {tier}")
            d.cart[it.id] = wanted[it.id]
            d.select_tier(it, tier)
            price = self._money_arg(raw.get("price"), f"سعر «{it.name}»", allow_none=True)
            if price is not None and price != it.price_for_tier(tier):
                d.price_overrides[it.id] = price
            unit = d.price_overrides.get(it.id, it.price)
            rows.append({"item": it.name, "code": it.code, "qty": wanted[it.id], "unit_price": unit, "tier": self.TIERS[tier]})
        for raw in extra_lines or []:
            name = str(raw.get("name", "")).strip()
            if not name:
                raise LedgerError("اسم البند اليدوي مطلوب")
            try:
                qty = int(raw.get("qty", 1) or 1)
            except (TypeError, ValueError) as e:
                raise LedgerError(f"كمية «{name}» غير صحيحة") from e
            price = self._money_arg(raw.get("price"), f"سعر «{name}»")
            if qty <= 0:
                raise LedgerError(f"كمية «{name}» يجب أن تكون أكبر من صفر")
            cost = self._money_arg(raw.get("cost"), "التكلفة", allow_none=True) or ZERO
            d.custom_lines.append(InvoiceLine(id=uid(), name=name, price=price, cost=cost, qty=qty, is_custom=True))
            rows.append({"item": name, "code": "", "qty": qty, "unit_price": price, "tier": "يدوي"})
        return d, rows

    async def create_invoice(self, items: list[dict] | None = None, customer: str = "", discount: float = 0,
                             paid: float | None = None, notes: str = "", price_tier: str = "retail",
                             extra_lines: list[dict] | None = None):
        d, rows = self._plan_invoice(items or [], extra_lines, price_tier)
        d.customer = (customer or "").strip()
        d.discount = self._money_arg(discount or 0, "الخصم") or ZERO
        d.paid = self._money_arg(paid, "المدفوع", allow_none=True)     # None = مدفوعة بالكامل
        d.notes = notes or ""
        inv, warnings = await self.led.finalize_invoice(d)
        self.documents.append({"kind": "invoice", "id": inv.id, "number": inv.number})
        return {"number": inv.number, "customer": inv.customer, "items": rows, "subtotal": inv.subtotal,
                "discount": inv.discount, "total": inv.total, "paid": inv.paid, "remaining": inv.remaining,
                "customer_total_debt_after": inv.customer_total_debt_after if inv.customer else None,
                "warnings": warnings, "note": "تم إنقاص الكميات من المخزون وحُفظت الفاتورة؛ يظهر للمالك زر عرض/طباعة."}

    def _describe_invoice(self, a: dict) -> str:
        try:
            d, rows = self._plan_invoice(a.get("items") or [], a.get("extra_lines"), str(a.get("price_tier") or "retail"))
            paid = self._money_arg(a.get("paid"), "المدفوع", allow_none=True)
            disc = self._money_arg(a.get("discount") or 0, "الخصم") or ZERO
            t = invoice_totals(self.led._build_invoice_lines(d), disc, paid)
            body = "\n".join(f"• {r['item']} × {r['qty']} @ {r['unit_price']} ({r['tier']})" for r in rows)
            who = a.get("customer") or "بدون اسم زبون"
            extra = ""
            if (a.get("customer") or "").strip() and t.remaining > 0 and not self.led._find_customer(a["customer"]):
                extra = "\n⚠ الزبون غير مسجّل — سيُنشأ زبون جديد بدين المتبقي."
            return (f"إنشاء فاتورة بيع — {who}\n{body}\nالخصم {t.discount} · الإجمالي {t.total} · "
                    f"المدفوع {t.paid} · المتبقي {t.remaining}{extra}")
        except LedgerError as e:        # الطلب غير صالح: ستفشل الأداة نفسها برسالة واضحة بعد الموافقة
            return f"إنشاء فاتورة (تنبيه: {e})"

    async def create_voucher(self, voucher_type: str, person: str, amount: float, note: str = "", ref_number: str = ""):
        vt = {"receipt": "receipt", "قبض": "receipt", "payment": "payment", "دفع": "payment", "صرف": "payment"}.get(
            (voucher_type or "").strip().lower())
        if not vt:
            raise LedgerError("voucher_type يجب أن يكون receipt (سند قبض) أو payment (سند دفع)")
        v = await self.led.add_voucher(vt, person, amount, note, ref_number)
        self.documents.append({"kind": "voucher", "id": v.id, "number": v.number})
        return {"number": v.number, "type": "سند قبض" if vt == "receipt" else "سند دفع", "person": v.person,
                "amount": v.amount, "previous_balance": v.previous_balance, "remaining_balance": v.remaining_balance,
                "note": "يظهر للمالك زر عرض/طباعة للسند."}

    def _describe_voucher(self, a: dict) -> str:
        kind = "سند قبض (مال داخل)" if str(a.get("voucher_type", "")).lower() in ("receipt", "قبض") else "سند دفع (مال خارج)"
        return f"إصدار {kind} — {a.get('person')} — المبلغ {a.get('amount')}" + (f" — {a['note']}" if a.get("note") else "")

    # ------------------------------------------------------------------ التسجيل
    def _register(self) -> None:
        S, N, I, B = "STRING", "NUMBER", "INTEGER", "BOOLEAN"
        cats = "، ".join(f"{k}={v}" for k, v in EXPENSE_CATEGORIES.items())

        def add(name, desc, handler, props=None, required=None, write=False, describe=None):
            self.tools[name] = Tool(name, desc, _obj(props or {}, required), handler, write, describe)

        # قراءة
        add("business_summary", "ملخص شامل: عدد الأصناف، رأس المال، الديون، مبيعات وأرباح اليوم والإجمالي.", self.business_summary)
        add("search_items", "ابحث في المخزون بالاسم أو الكود (عربي/إنجليزي). فارغ = كل الأصناف.", self.search_items,
            {"query": (S, "نص البحث"), "limit": (I, "أقصى عدد نتائج (افتراضي 25)")})
        add("list_low_stock", "الأصناف التي كميتها أقل من أو تساوي الحد.", self.list_low_stock,
            {"threshold": (I, "الحد (افتراضي 3)")})
        add("list_customers", "قائمة العملاء وأرصدتهم (الدين). يمكن البحث أو عرض المدينين فقط.", self.list_customers,
            {"query": (S, "اسم أو هاتف"), "only_debtors": (B, "المدينون فقط"), "limit": (I, "أقصى عدد")})
        add("get_customer_statement", "كشف حساب زبون (فواتيره وحركاته).", self.get_customer_statement,
            {"name": (S, "اسم الزبون"), "limit": (I, "عدد آخر الفواتير")}, ["name"])
        add("list_invoices", "فواتير البيع مع تصفية بالبحث أو بالتاريخ (YYYY-MM-DD).", self.list_invoices,
            {"query": (S, "رقم فاتورة أو اسم زبون"), "date_from": (S, "من تاريخ"), "date_to": (S, "إلى تاريخ"),
             "limit": (I, "أقصى عدد")})
        add("get_invoice", "تفاصيل فاتورة كاملة برقمها (بنودها وأسعارها).", self.get_invoice,
            {"number": (S, "رقم الفاتورة مثل INV-1001")}, ["number"])
        add("list_purchases", "فواتير الشراء من الموردين.", self.list_purchases, {"limit": (I, "أقصى عدد")})
        add("list_suppliers", "الموردون وما علينا لهم.", self.list_suppliers)
        add("list_expenses", "المصاريف.", self.list_expenses, {"limit": (I, "أقصى عدد")})
        add("list_vouchers", "سندات القبض والدفع.", self.list_vouchers, {"limit": (I, "أقصى عدد")})
        add("best_sellers", "أكثر الأصناف مبيعاً بالكمية.", self.best_sellers, {"n": (I, "العدد")})
        add("profit_overview", "الأرباح شهرياً + الإجماليات.", self.profit_overview)
        add("list_data_keys", "كل مفاتيح جدول app_data في Supabase.", self.list_data_keys)
        add("read_data_key", "قراءة خام (JSON) لأي سجل في app_data. للقراءة فقط.", self.read_data_key,
            {"key": (S, "اسم المفتاح مثل inventory"), "max_chars": (I, "أقصى حروف")}, ["key"])
        add("list_project_files", "ملفات كود البرنامج (بايثون).", self.list_project_files)
        add("read_project_file", "اقرأ ملف كود من البرنامج (للقراءة فقط) لفهم كيف يعمل قبل اقتراح ميزة.",
            self.read_project_file, {"path": (S, "مثل ledger.py أو ui/tabs/invoice.py"), "max_chars": (I, "أقصى حروف")}, ["path"])
        add("google_search",
            "ابحث في شبكة جوجل (ثم الويب) عن أسعار السوق، مواصفات قطع الغيار، بيانات تقنية، أو أي معلومة ليست في بيانات المتجر. "
            "لا تستخدمها لأسئلة المخزون المحلي — لذلك search_items.",
            self.google_search, {"query": (S, "عبارة البحث بالعربية أو الإنجليزية"), "max_results": (I, "عدد النتائج (1–10)")},
            ["query"])
        add("show_item_images",
            "أرسل للمالك صور أصناف من المخزون داخل المحادثة (حين يطلب «أرني صورة/صور …» أو يسأل كيف يبدو الصنف). "
            "حدّد الصنف باسمه أو كوده في query، أو بمعرّفاته في item_ids. تظهر الصور للمالك مباشرة.",
            self.show_item_images,
            {"query": (S, "اسم الصنف أو كوده"), "item_ids": (S, "معرّفات أصناف مفصولة بفاصلة (من search_items)"),
             "limit": (I, "أقصى عدد صور (افتراضي 12، الأقصى 100) — ارفعه إن طلب المالك كل الصور")})
        add("search_catalogs",
            "ابحث في كتالوجات الفلاتر على الإنترنت (Sardes، MANN، Delsa، FMI …) باسم الفلتر أو رقمه أو رقم المرجع/OEM وأعد مواصفاته "
            "ومرجعياته المتقاطعة والسيارات المناسبة له. استخدمها أولاً لأي سؤال عن فلتر أو رقم قطعة، ثم قارنها بـ search_items "
            "لمعرفة إن كان متوفراً في المخزون.",
            self.search_catalogs,
            {"query": (S, "اسم الفلتر أو رقمه أو مرجعه (مثل HU 7008 z أو OC 1234)"),
             "sources": (S, "اختياري: معرّفات كتالوجات مفصولة بفاصلة (fmi, sardes, sardes-xref, mann, delsa). فارغ = كلها")},
            ["query"])
        add("attach_catalog_image",
            "ضع صورة من صور الكتالوج التي أرسلتها للمالك (بعد search_catalogs) كصورة لصنف موجود في المخزون. استخدمها فقط حين يطلب المالك "
            "صراحةً إضافة الصورة لصنف. تتطلب موافقته. picture_no هو ترتيب الصورة كما ظهرت (1 = الأولى).",
            self.attach_catalog_image,
            {"item": (S, "كود الصنف أو معرّفه أو اسمه (الأفضل الكود)"), "picture_no": (I, "ترتيب الصورة (افتراضي 1)")},
            ["item"], True,
            lambda a: f"إضافة صورة الكتالوج رقم {a.get('picture_no', 1)} كصورة للصنف «{a.get('item', '')}»")
        add("fill_missing_images",
            "اجلب تلقائياً من كتالوج FMI صور الأصناف الموجودة في المخزون والتي ليس لها صورة، وأضفها لها (مطابقة بالكود التام فقط، "
            "ولا تستبدل صورة موجودة). استخدمها حين يطلب المالك «أضف صور الأصناف الناقصة/املأ الصور تلقائياً». تتطلب موافقته. "
            "تعالج دفعة (افتراضي 12، الأقصى 40) وتخبرك بالمتبقي.",
            self.fill_missing_images,
            {"limit": (I, "عدد الأصناف في الدفعة (افتراضي 12، الأقصى 40)"), "in_stock_only": (B, "الأصناف المتوفرة فقط")},
            None, True,
            lambda a: f"جلب صور أصناف المخزون الناقصة من كتالوج FMI وإضافتها (حتى {a.get('limit', 12)} صنف)")
        add("read_webpage",
            "اقرأ نص صفحة ويب (رابط http/https) وجدتها بالبحث لاستخراج المواصفات: الأبعاد، المادة، المرجعيات، الأسعار. للقراءة فقط.",
            self.read_webpage, {"url": (S, "رابط الصفحة"), "max_chars": (I, "أقصى عدد حروف (افتراضي 6000، الأقصى 20000)")}, ["url"])
        add("open_catalog",
            "اعرض للمالك زر فتح موقع كتالوج (افتراضياً FMI) ليتصفحه بنفسه. استخدمها حين يطلب «افتح الكتالوج/الموقع» أو حين يفشل البحث "
            "الآلي في كتالوج له open_url (مثل FMI بلا حساب FileMaker).",
            self.open_catalog,
            {"source": (S, "معرّف الكتالوج: fmi (الافتراضي)، sardes، mann، delsa …")})
        sheet_schema = {"type": "OBJECT", "properties": {
            "name": {"type": "STRING", "description": "اسم الورقة (حتى 31 حرفاً)"},
            "headers": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "عناوين الأعمدة"},
            "rows": {"type": "ARRAY", "description": "الصفوف: كل صف مصفوفة خلايا نصية؛ الأرقام تُكتب كما هي (مثل 27.5 أو $27.5 أو 12%) "
                                                     "وتتحول لأرقام حقيقية في Excel",
                     "items": {"type": "ARRAY", "items": {"type": "STRING"}}}},
            "required": ["name", "headers", "rows"]}
        self.tools["export_excel"] = Tool(
            "export_excel",
            "أنشئ ملف Excel (.xlsx) من جداول كتبتَها أنت (تحليل، مقارنة، توقعات…). استخدمها حين يطلب المالك ملف إكسل لجدول صنعتَه. "
            "للبيانات الموجودة أصلاً في البرنامج استخدم export_dataset بدلاً منها (أدق وبلا حد للصفوف).",
            {"type": "OBJECT", "properties": {
                "filename": {"type": "STRING", "description": "اسم الملف بالعربية بلا امتداد، مثل أكثر-المبيعات"},
                "sheets": {"type": "ARRAY", "items": sheet_schema, "description": "ورقة أو أكثر"}},
             "required": ["filename", "sheets"]}, self.export_excel)
        add("export_dataset", "صدّر بيانات البرنامج إلى Excel مباشرة وكاملة (مع صف إجمالي بمعادلات). المجموعات: "
            + "؛ ".join(f"{k}={v}" for k, v in self.EXPORT_DATASETS.items()), self.export_dataset,
            {"dataset": (S, "اسم المجموعة من القائمة"), "query": (S, "تصفية بالاسم/الرقم (اختياري)"),
             "date_from": (S, "من تاريخ YYYY-MM-DD (للفواتير والمشتريات والمصاريف والسندات)"),
             "date_to": (S, "إلى تاريخ YYYY-MM-DD"), "filename": (S, "اسم الملف (اختياري)")}, ["dataset"])
        add("save_proposal", "احفظ اقتراح ميزة جديدة أو تعديل كود (Markdown فيه الكود الكامل) ليراجعه المالك. لا يعدّل البرنامج.",
            self.save_proposal, {"title": (S, "عنوان الاقتراح"), "content": (S, "الشرح والكود الكامل")}, ["title", "content"])

        # كتابة (تحتاج موافقة)
        add("clean_image_background",
            "أصلح صورة أرسلها المالك (للصنف): أزل خلفيتها واجعلها بيضاء ناصعة مطابقة لأبيض البرنامج، وضع الصنف كاملاً في وسط مربع. "
            "استخدمها حين يرسل صورة ويطلب «زبّطها/نظّفها/اجعل خلفيتها بيضاء». لا تتطلب موافقة وتعرض النتيجة في المحادثة.",
            self.clean_image_background, {"which": (I, "رقم الصورة المرسلة (افتراضي 1)")})
        add("read_excel_rows",
            "اقرأ صفوفاً من ملف Excel/CSV الذي رفعه المالك (العناوين + الصفوف) لتفهم أعمدته قبل التسجيل.",
            self.read_excel_rows,
            {"file_name": (S, "اسم الملف (اختياري)"), "start": (I, "رقم الصف الأول (يبدأ من 0)"), "limit": (I, "عدد الصفوف (الأقصى 100)")})
        add("import_excel_items",
            "سجّل أصنافاً دفعة واحدة في المخزون من ملف Excel/CSV رفعه المالك. حدّد أسماء الأعمدة كما في العناوين "
            "(اقرأها أولاً بـ read_excel_rows). تتخطى الأصناف الموجودة (بنفس الكود أو الاسم) افتراضياً. تتطلب موافقة المالك.",
            self.import_excel_items,
            {"name_col": (S, "عمود اسم الصنف"), "cost_col": (S, "عمود رأس المال/التكلفة"), "price_col": (S, "عمود سعر المفرق"),
             "file_name": (S, "اسم الملف (اختياري)"), "stock_col": (S, "عمود الكمية"), "code_col": (S, "عمود الكود"),
             "wholesale_col": (S, "عمود سعر الجملة"), "distribution_col": (S, "عمود سعر التوزيع"),
             "skip_existing": (B, "تخطَّ الموجود (افتراضي true)")},
            ["name_col", "cost_col", "price_col"], True,
            lambda a: f"تسجيل أصناف من ملف Excel — الاسم: {a.get('name_col')} · التكلفة: {a.get('cost_col')} · السعر: {a.get('price_col')}")
        add("attach_user_image",
            "ضع الصورة التي أرسلها المالك (بعد تنظيفها بـ clean_image_background إن وُجدت) كصورة لصنف في المخزون. "
            "استخدمها فقط حين يطلب إضافتها لصنف. تتطلب موافقته.",
            self.attach_user_image,
            {"item": (S, "كود الصنف أو معرّفه أو اسمه (الأفضل الكود)"), "cleaned": (B, "استعمل النسخة المنظّفة (افتراضي true)")},
            ["item"], True,
            lambda a: f"وضع الصورة المرسلة كصورة للصنف «{a.get('item', '')}»")
        add("add_item", "أضف صنفاً جديداً للمخزون.", self.add_item,
            {"name": (S, "اسم الصنف"), "cost": (N, "رأس المال"), "price": (N, "سعر المفرق"), "stock": (I, "الكمية"),
             "code": (S, "الكود"), "price_wholesale": (N, "سعر الجملة"), "price_distribution": (N, "سعر التوزيع")},
            ["name", "cost", "price"], True,
            lambda a: f"إضافة صنف «{a.get('name')}» — رأس المال {a.get('cost')} · مفرق {a.get('price')} · كمية {a.get('stock', 0)}")
        add("edit_item", "عدّل اسم صنف أو كوده أو كميته.", self.edit_item,
            {"item": (S, "id الصنف أو كوده أو اسمه"), "name": (S, "الاسم الجديد"), "code": (S, "الكود الجديد"),
             "stock": (I, "الكمية الجديدة")}, ["item"], True,
            lambda a: f"تعديل الصنف «{a.get('item')}»: " + "، ".join(f"{k}={v}" for k, v in a.items() if k != "item"))
        add("edit_item_prices", "عدّل أسعار صنف (رأس المال/مفرق/جملة/توزيع). غير المذكور يبقى كما هو.", self.edit_item_prices,
            {"item": (S, "id الصنف أو كوده أو اسمه"), "cost": (N, "رأس المال"), "retail_price": (N, "مفرق"),
             "wholesale_price": (N, "جملة"), "distribution_price": (N, "توزيع")}, ["item"], True,
            lambda a: f"تعديل أسعار «{a.get('item')}»: " + "، ".join(f"{k}={v}" for k, v in a.items() if k != "item"))
        add("restock_item", "أضف كمية لصنف موجود عن طريق كوده.", self.restock_item,
            {"code": (S, "كود الصنف"), "qty": (I, "الكمية المضافة")}, ["code", "qty"], True,
            lambda a: f"إضافة {a.get('qty')} قطعة للصنف ذي الكود {a.get('code')}")
        add("add_customer", "أضف زبوناً جديداً.", self.add_customer,
            {"name": (S, "الاسم"), "phone": (S, "الهاتف")}, ["name"], True,
            lambda a: f"إضافة زبون «{a.get('name')}» {a.get('phone', '')}")
        add("record_customer_payment", "سجّل سند قبض (دفعة) من زبون وأنقِص دينه.", self.record_customer_payment,
            {"customer_name": (S, "اسم الزبون"), "amount": (N, "المبلغ المقبوض")}, ["customer_name", "amount"], True,
            lambda a: f"سند قبض {a.get('amount')} من «{a.get('customer_name')}»")
        item_line = {"type": "OBJECT", "properties": {
            "item": {"type": "STRING", "description": "id الصنف أو كوده أو اسمه (يجب أن يحدد صنفاً واحداً)"},
            "qty": {"type": "INTEGER", "description": "الكمية"},
            "price": {"type": "NUMBER", "description": "سعر الوحدة إن اختلف عن سعر الشريحة (اختياري)"},
            "tier": {"type": "STRING", "description": "retail | wholesale | distribution لهذا البند فقط (اختياري)"}},
            "required": ["item", "qty"]}
        extra_line = {"type": "OBJECT", "properties": {
            "name": {"type": "STRING", "description": "اسم الخدمة أو الصنف اليدوي"},
            "price": {"type": "NUMBER", "description": "سعر البيع"}, "qty": {"type": "INTEGER", "description": "الكمية (افتراضي 1)"},
            "cost": {"type": "NUMBER", "description": "التكلفة لحساب الربح (اختياري)"}},
            "required": ["name", "price"]}
        self.tools["create_invoice"] = Tool(
            "create_invoice",
            "أنشئ فاتورة بيع حقيقية: تنقص المخزون وتسجّل الدين على الزبون إن بقي متبقٍّ. تحقّق من الأصناف أولاً بـ search_items "
            "ومن الزبون بـ list_customers، ولا تخمّن أسعاراً أو كميات. paid فارغ = مدفوعة بالكامل. لا تُنشئ الفاتورة إن كان المخزون غير كافٍ.",
            {"type": "OBJECT", "properties": {
                "items": {"type": "ARRAY", "items": item_line, "description": "أصناف المخزون في الفاتورة"},
                "customer": {"type": "STRING", "description": "اسم الزبون (اختياري)"},
                "discount": {"type": "NUMBER", "description": "خصم بالدولار (اختياري)"},
                "paid": {"type": "NUMBER", "description": "المبلغ المدفوع الآن (اتركه فارغاً إن دفع كل الإجمالي)"},
                "notes": {"type": "STRING", "description": "ملاحظات"},
                "price_tier": {"type": "STRING", "description": "retail (افتراضي) | wholesale | distribution"},
                "extra_lines": {"type": "ARRAY", "items": extra_line, "description": "بنود يدوية غير موجودة بالمخزون (خدمات)"}}},
            self.create_invoice, True, self._describe_invoice)
        self.tools["create_voucher"] = Tool(
            "create_voucher",
            "أصدر سنداً: receipt = سند قبض (استلمنا مالاً من زبون أو جهة) ، payment = سند دفع (دفعنا مالاً لمورد أو جهة). "
            "يخصم من رصيد الزبون/المورد إن كان الاسم مطابقاً لحساب موجود. تأكد من الاسم والمبلغ قبل الإصدار.",
            _obj({"voucher_type": (S, "receipt أو payment"), "person": (S, "اسم الزبون/المورد/الجهة"),
                  "amount": (N, "المبلغ بالدولار"), "note": (S, "ملاحظة أو بيان السند"),
                  "ref_number": (S, "رقم مرجعي (اختياري)")}, ["voucher_type", "person", "amount"]),
            self.create_voucher, True, self._describe_voucher)
        add("add_expense", f"سجّل مصروفاً. الفئات: {cats}.", self.add_expense,
            {"category": (S, "مفتاح الفئة (rent/water/internet/fuel/other)"), "amount": (N, "المبلغ"),
             "note": (S, "ملاحظة"), "period": (S, "الفترة")}, ["category", "amount"], True,
            lambda a: f"تسجيل مصروف {a.get('amount')} ({a.get('category')}) {a.get('note', '')}")
