"""طبقة التخزين غير المتزامنة.

تحافظ على نفس مخطط Supabase الحالي تماماً:
    جدول app_data (key text PK, value jsonb, updated_at timestamptz)
فيبقى تطبيق الويب القديم وتطبيق الهاتف يقرآن ويكتبان على نفس البيانات.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Protocol


class StoreError(Exception):
    pass


class AppDataStore(Protocol):
    async def get(self, key: str) -> tuple[Any | None, str | None]:
        """يرجع (value, updated_at) أو (None, None) إن لم يوجد السجل."""

    async def put_if_unchanged(self, key: str, value: Any, expected_updated_at: str | None) -> bool:
        """كتابة مشروطة: تنجح فقط إن لم يغيّر أحد السجل منذ قراءتنا له (تفادي الكتابة فوق عمل جهاز آخر)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RestStore:
    """اتصال Supabase عبر واجهة PostgREST مباشرة (urllib فقط — بلا مكتبة supabase).

    السبب: مكتبة `supabase` تجرّ عشرات الحزم (pydantic-core, websockets, httpx …) وبعضها لا يُبنى على أندرويد،
    بينما البرنامج يحتاج فقط قراءة/كتابة جدول app_data. هذا يعمل بالطريقة نفسها على ويندوز وأندرويد.
    المفاتيح تُمرَّر من الإعدادات/.env — لا تضعها داخل الكود، ولا تنشرها.
    """

    TIMEOUT = 30

    def __init__(self, url: str, key: str, table: str = "app_data"):
        self._base = url.strip().rstrip("/") + "/rest/v1/" + table
        self._key = key.strip()

    @classmethod
    async def create(cls, url: str, key: str) -> "RestStore":
        if not url.strip().startswith(("https://", "http://")):
            raise StoreError("رابط Supabase يجب أن يبدأ بـ https://")
        if not key.strip():
            raise StoreError("مفتاح Supabase فارغ")
        return cls(url, key)

    # ---- نقل HTTP (يعمل بخيط مستقل حتى لا يجمّد الواجهة) ----
    def _request(self, method: str, query: dict[str, str], body: Any = None, prefer: str | None = None) -> Any:
        import json
        import urllib.error
        import urllib.parse
        import urllib.request

        qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe='(),.:*')}" for k, v in query.items())
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        req = urllib.request.Request(self._base + ("?" + qs if qs else ""), data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                j = json.loads(e.read().decode("utf-8", "replace"))
                detail = j.get("message") or j.get("hint") or ""
            except Exception:  # noqa: BLE001
                pass
            hint = {401: " — المفتاح غير صحيح", 403: " — ممنوع (RLS بلا policy؟ جرّب مفتاح service_role)",
                    404: " — الجدول app_data غير موجود (نفّذ schema.sql)"}.get(e.code, "")
            raise StoreError(f"Supabase HTTP {e.code}: {detail}{hint}".strip()) from e
        except urllib.error.URLError as e:
            raise StoreError(f"تعذّر الوصول إلى Supabase ({e.reason})") from e
        except TimeoutError as e:
            raise StoreError("انتهت مهلة الاتصال بـ Supabase") from e
        except OSError as e:
            raise StoreError(f"خطأ شبكة: {e}") from e
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise StoreError("ردّ غير مفهوم من Supabase (تحقق من الرابط)") from e

    async def _call(self, *a, **kw):
        return await asyncio.to_thread(self._request, *a, **kw)

    @staticmethod
    def _q(key: str) -> str:
        return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'

    async def get(self, key):
        rows = await self._call("GET", {"select": "value,updated_at", "key": "eq." + key, "limit": "1"}) or []
        if not rows:
            return None, None
        return rows[0].get("value"), rows[0].get("updated_at")

    async def get_many(self, keys: list[str]) -> dict[str, tuple[Any, str | None]]:
        """قراءة عدة سجلات بطلب شبكة واحد — أسرع بكثير عند بدء التشغيل."""
        if not keys:
            return {}
        rows = await self._call("GET", {"select": "key,value,updated_at",
                                        "key": "in.(" + ",".join(self._q(k) for k in keys) + ")"}) or []
        return {r["key"]: (r.get("value"), r.get("updated_at")) for r in rows if r.get("key")}

    async def keys(self) -> list[str]:
        """كل مفاتيح جدول app_data (للمساعد الذكي وللتشخيص)."""
        rows = await self._call("GET", {"select": "key", "limit": "10000"}) or []
        return sorted(r["key"] for r in rows if r.get("key"))

    async def put_if_unchanged(self, key, value, expected_updated_at):
        if expected_updated_at:
            rows = await self._call("PATCH", {"key": "eq." + key, "updated_at": "eq." + str(expected_updated_at)},
                                    {"value": value, "updated_at": _now()}, prefer="return=representation")
            return bool(rows)
        await self._call("POST", {"on_conflict": "key"}, {"key": key, "value": value, "updated_at": _now()},
                         prefer="resolution=merge-duplicates,return=minimal")
        return True


SupabaseStore = RestStore       # الاسم القديم يبقى صالحاً (main.py والإعدادات)


class MemoryStore:
    """للاختبارات ولوضع عدم الاتصال. يحاكي الكتابة المشروطة بدقة."""

    def __init__(self):
        self._d: dict[str, tuple[Any, str]] = {}
        self._tick = 0
        self.fail = False  # لمحاكاة انقطاع الشبكة

    def _stamp(self) -> str:
        self._tick += 1
        return f"t{self._tick:09d}"

    async def get(self, key):
        if self.fail:
            raise StoreError("offline")
        await asyncio.sleep(0)
        return self._d.get(key, (None, None))

    async def get_many(self, keys):
        if self.fail:
            raise StoreError("offline")
        return {k: self._d[k] for k in keys if k in self._d}

    async def keys(self) -> list[str]:
        if self.fail:
            raise StoreError("offline")
        return sorted(self._d)

    async def put_if_unchanged(self, key, value, expected_updated_at):
        if self.fail:
            raise StoreError("offline")
        await asyncio.sleep(0)
        cur = self._d.get(key)
        if cur is None:
            if expected_updated_at is not None:
                return False
        elif cur[1] != expected_updated_at:
            return False
        self._d[key] = (value, self._stamp())
        return True
