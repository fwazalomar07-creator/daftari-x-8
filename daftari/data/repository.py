"""المستودع: دمج بالمعرّف + كتابة آمنة من التعارض + كاش محلي للعمل بدون إنترنت.

نفس فلسفة saveRecordList في النسخة الأصلية:
  1) اقرأ آخر نسخة سحابية قبل الحفظ مباشرة.
  2) ادمج بالمعرّف: ما أضافه جهاز آخر يبقى، ونسختنا تفوز فيما لمسناه،
     والمحذوف صراحةً فقط هو ما يُحذف.
  3) لكشوف العملاء/الموردين: اتحاد سجل الحركات وإعادة حساب الرصيد منه.
  4) الكتابة مشروطة بعدم تغيّر updated_at، وإلا أعد القراءة/الدمج (حتى 5 مرات).
  5) عند انقطاع الاتصال: احفظ محلياً وعلّم الجدول "غير متزامن" لإعادة المحاولة لاحقاً.
"""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any, TypeVar

from ..core.calc import CUSTOMER_SIGN, SUPPLIER_SIGN, merge_account, _parse_dt
from ..core.models import Customer, Record, Supplier, TABLES
from .store import AppDataStore, StoreError

R = TypeVar("R", bound=Record)
_DATE_DESC = {"invoices", "vouchers", "purchases", "expenses"}  # تظهر الأحدث أولاً


class Repository:
    MAX_ATTEMPTS = 5

    def __init__(self, store: AppDataStore | None, cache_dir: Path | None = None):
        self.store = store
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.dirty: set[str] = set()
        # أخطاء آخر تحميل من السحابة (key -> رسالة) وما لم يُوجد له سجل أصلاً — لتشخيص «لا تظهر الأصناف»
        self.load_errors: dict[str, str] = {}
        self.missing_keys: set[str] = set()
        # معرّفات حُذفت محلياً ولم تُحفظ بعد، لكل جدول (+ سطور كشف الحساب)
        self.pending_deletes: dict[str, set[str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- الكاش المحلي ----------------------------------------------------------------
    def _cache_path(self, key: str) -> Path | None:
        return self.cache_dir / f"daftari_{key}.json" if self.cache_dir else None

    def _cache_write(self, key: str, value: Any) -> None:
        p = self._cache_path(key)
        if p:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)  # كتابة ذرّية: لا ملف نصف مكتوب عند انقطاع الكهرباء

    def _cache_read(self, key: str, default: Any) -> Any:
        p = self._cache_path(key)
        if p and p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        return default

    # ---- القراءة -----------------------------------------------------------------------
    async def load(self, cls: type[R]) -> list[R]:
        key = cls.TABLE  # type: ignore[attr-defined]
        raw: Any = None
        if self.store:
            try:
                raw, _ = await self.store.get(key)
                self.load_errors.pop(key, None)
                if raw is not None:
                    self.missing_keys.discard(key)
                    self._cache_write(key, raw)
                else:
                    self.missing_keys.add(key)
            except StoreError as e:
                self.load_errors[key] = str(e)
                raw = None
        if raw is None:
            raw = self._cache_read(key, [])
        return [cls.from_dict(x) for x in raw if isinstance(x, dict)]

    async def load_many(self, classes: list[type[R]], cloud: bool = True) -> dict[str, list[R]]:
        """يحمّل عدة جداول بطلب شبكة واحد. cloud=False: من الكاش المحلي فقط (فوري — لبدء تشغيل سريع)."""
        keys = [c.TABLE for c in classes]  # type: ignore[attr-defined]
        rows: dict[str, Any] = {}
        fetched = False
        if cloud and self.store is not None and hasattr(self.store, "get_many"):
            try:
                rows = await self.store.get_many(keys + ["settings"])
                fetched = True
                for k in keys:
                    self.load_errors.pop(k, None)
            except StoreError as e:
                for k in keys:
                    self.load_errors[k] = str(e)
        out: dict[str, list[R]] = {}
        for cls in classes:
            key = cls.TABLE  # type: ignore[attr-defined]
            raw = None
            if fetched:
                raw = (rows.get(key) or (None, None))[0]
                if raw is not None:
                    self.missing_keys.discard(key)
                    self._cache_write(key, raw)
                else:
                    self.missing_keys.add(key)
            if raw is None:
                raw = self._cache_read(key, [])
            out[key] = [cls.from_dict(x) for x in raw if isinstance(x, dict)]
        if fetched and isinstance(((rows.get("settings") or (None, None))[0]), dict):
            self._cache_write("settings", rows["settings"][0])
        return out

    def has_cache(self, key: str) -> bool:
        p = self._cache_path(key)
        return bool(p and p.exists())

    async def load_settings(self, default: dict[str, Any]) -> dict[str, Any]:
        raw = None
        if self.store:
            try:
                raw, _ = await self.store.get("settings")
            except StoreError:
                raw = None
        return raw if isinstance(raw, dict) else self._cache_read("settings", default)

    async def save_settings(self, settings: dict[str, Any]) -> None:
        self._cache_write("settings", settings)
        if self.store:
            try:
                _, ts = await self.store.get("settings")
                await self.store.put_if_unchanged("settings", settings, ts)
            except StoreError:
                self.dirty.add("settings")

    # ---- الحذف المؤجَّل ------------------------------------------------------------------
    def mark_deleted(self, table: str, record_id: str) -> None:
        self.pending_deletes.setdefault(table, set()).add(record_id)

    def mark_history_deleted(self, table: str, signature: str) -> None:
        self.pending_deletes.setdefault(table + "_history", set()).add(signature)

    # ---- الحفظ -------------------------------------------------------------------------
    async def save(self, cls: type[R], local: list[R]) -> list[R]:
        key = cls.TABLE  # type: ignore[attr-defined]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            merged = local
            if self.store:
                merged = await self._save_with_merge(cls, key, local)
            if not self.store:  # بلا سحابة: طبّق الحذف محلياً
                deleted = self.pending_deletes.get(key, set())
                merged = [r for r in local if getattr(r, "id", None) not in deleted]
            if key not in self.dirty:
                # نجح الرفع: انتهى دور قوائم الحذف. وإن فشل الرفع نُبقيها حتى
                # لا يعيد الدمج القادم سجلاً حذفناه محلياً من السحابة.
                self.pending_deletes.pop(key, None)
                self.pending_deletes.pop(key + "_history", None)
            self._cache_write(key, [r.to_dict() for r in merged])
            return merged

    async def _save_with_merge(self, cls: type[R], key: str, local: list[R]) -> list[R]:
        sign_map = CUSTOMER_SIGN if cls is Customer else SUPPLIER_SIGN if cls is Supplier else None
        deleted_ids = self.pending_deletes.get(key, set())
        deleted_sigs = self.pending_deletes.get(key + "_history", set()) if sign_map else set()
        merged = local
        for _ in range(self.MAX_ATTEMPTS):
            try:
                raw, ts = await self.store.get(key)  # type: ignore[union-attr]
                cloud = [cls.from_dict(x) for x in (raw or []) if isinstance(x, dict)]
                by_id: dict[str, R] = {r.id: r for r in cloud}  # type: ignore[attr-defined]
                cloud_by_id = dict(by_id)
                for rec in local:
                    rid = rec.id  # type: ignore[attr-defined]
                    by_id[rid] = (
                        merge_account(cloud_by_id.get(rid), rec, sign_map, deleted_sigs) if sign_map else rec
                    )
                for rid in deleted_ids:
                    by_id.pop(rid, None)
                merged = list(by_id.values())
                if key in _DATE_DESC:
                    merged.sort(key=lambda r: _parse_dt(getattr(r, "date", "")), reverse=True)
                ok = await self.store.put_if_unchanged(key, [r.to_dict() for r in merged], ts)  # type: ignore[union-attr]
                if ok:
                    self.dirty.discard(key)
                    return merged
                await asyncio.sleep(0.12 + random.random() * 0.15)  # تعارض: أعد القراءة والدمج
            except StoreError:
                self.dirty.add(key)  # لا اتصال: النسخة المحلية محفوظة وستُزامَن لاحقاً
                return local
        self.dirty.add(key)
        return merged

    async def sync_dirty(self, state_lists: dict[str, list[Record]]) -> list[str]:
        """أعد محاولة رفع الجداول التي حُفظت محلياً فقط (استدعِها عند عودة الإنترنت)."""
        done = []
        for key in list(self.dirty):
            cls = TABLES.get(key)
            if cls and key in state_lists:
                await self.save(cls, state_lists[key])
                if key not in self.dirty:
                    done.append(key)
        return done
