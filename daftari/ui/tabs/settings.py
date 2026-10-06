"""الإعدادات: بيانات المحل، كلمة المرور، النسخ الاحتياطي/الاستعادة، حالة المزامنة، الثيمات، مزوّدو الذكاء الاصطناعي، كتالوجات الفلاتر."""
from __future__ import annotations

import asyncio

import json
import os
from datetime import datetime
from pathlib import Path

import flet as ft

from ...ledger import LedgerError
from .. import widgets as w


def _write_env_key(home: Path, key: str, value: str) -> None:
    """يكتب أو يحدّث مفتاحاً في ملف ~/.daftari/.env (أو DAFTARI_HOME/.env)."""
    env_path = home / ".env"
    lines: list[str] = []
    found = False
    if env_path.is_file():
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                if raw.strip().startswith(f"{key}=") or raw.strip().startswith(f"export {key}="):
                    lines.append(f"{key}={value}")
                    found = True
                else:
                    lines.append(raw)
        except OSError:
            pass
    if not found:
        lines.append(f"{key}={value}")
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except OSError:
        pass


def build(app) -> ft.Control:
    led = app.ledger
    s = led.settings
    cloud = led.repo.store is not None
    dirty = sorted(led.repo.dirty)

    # ---- بيانات المحل ----
    def edit_business(_):
        async def submit(v):
            await led.update_settings(business_name=v["name"], tagline=v["tagline"], phone=v["phone"],
                                      company_number=v["company"])
        w.form_dialog(app, "بيانات المحل", [
            ("name", "اسم المحل", s.get("businessName", ""), "text"), ("tagline", "الشعار النصي", s.get("tagline", ""), "text"),
            ("phone", "رقم هاتف المحل", s.get("phone", ""), "text"),
            ("company", "رقم الشركة (السجل التجاري)", s.get("companyNumber", ""), "text")], submit)

    # ---- ترويسة طباعة المحادثة ----
    def edit_chat_print(_):
        from ...ai import chat_export as ce

        async def submit(v):
            led.settings["chatPrintNote"] = v["note"].strip()
            led.settings["chatPrintAiType"] = v["ai"].strip()
            await led.repo.save_settings(led.settings)
            w.toast(app.page, "تم حفظ ترويسة طباعة المحادثة")
        w.form_dialog(app, "ترويسة طباعة المحادثة", [
            ("note", "العبارة أعلى اليسار", s.get("chatPrintNote", ce.DEFAULT_AUDIT_NOTE), "text"),
            ("ai", "نوع الذكاء (تظهر بعد «نوع الذكاء :»)", s.get("chatPrintAiType", ce.DEFAULT_AI_TYPE), "text")],
            submit, subtitle="الشعار الأسود يظهر أعلى اليمين. اترك الحقل فارغاً لإخفاء السطر.")

    from ...ai import chat_export as _ce
    logo_preview = ft.Container(width=90, height=90, bgcolor="#FFFFFF", border_radius=10, border=w.border_all(1, w.LINE),
                                padding=4, alignment=ft.Alignment(0, 0))

    def _show_logo() -> None:
        raw = _ce.logo_bytes(led.settings)
        logo_preview.content = w.image_from_bytes(raw, width=80, height=80, fit=w._contain()) if raw else ft.Text("لا يوجد", size=11, color=w.SOFT)

    _show_logo()

    async def pick_chat_logo(_):
        got = await app.pick_file_bytes(extensions=["png", "jpg", "jpeg", "webp"])
        if not got:
            return
        try:
            png = await asyncio.to_thread(_ce.clean_logo, got[1])       # PNG شفاف مقصوص (يزيل الخلفية البيضاء إن وُجدت)
        except ValueError as e:
            w.toast(app.page, str(e), error=True)
            return
        import base64
        d = app.home / "images"
        d.mkdir(parents=True, exist_ok=True)
        (d / "chat_logo.png").write_bytes(png)
        led.settings["chatPrintLogo"] = str(d / "chat_logo.png")
        led.settings["chatPrintLogoB64"] = base64.b64encode(png).decode("ascii") if len(png) <= 400_000 else ""
        await led.repo.save_settings(led.settings)
        _show_logo()
        w.safe_update(logo_preview)
        w.toast(app.page, "تم حفظ شعار الطباعة")

    async def reset_chat_logo(_):
        led.settings["chatPrintLogo"] = ""
        led.settings["chatPrintLogoB64"] = ""
        await led.repo.save_settings(led.settings)
        _show_logo()
        w.safe_update(logo_preview)
        w.toast(app.page, "عاد الشعار الافتراضي")

    # ---- كلمة المرور ----
    def set_password(_):
        async def submit(v):
            await led.set_password(v["pass"])
            w.toast(app.page, "تم تفعيل القفل" if v["pass"].strip() else "تم إلغاء القفل")
        w.form_dialog(app, "كلمة مرور التطبيق", [("pass", "كلمة المرور (فارغة = إلغاء القفل)", "", "password")], submit,
                      subtitle="تُحفظ مشفّرة، وتُطلب عند كل فتح للتطبيق")

    # ---- النسخ الاحتياطي ----
    async def export(_):
        data = json.dumps(led.export_backup(), ensure_ascii=False, indent=1).encode("utf-8")
        await app.save_with_toast(f"daftari-backup-{datetime.now():%Y-%m-%d}.json", data, "النسخة الاحتياطية")

    async def import_(_):
        got = await app.pick_file_bytes(["json"])
        if not got:
            return
        try:
            payload = json.loads(got[1].decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            w.toast(app.page, "تعذّر قراءة الملف — تأكد أنه ملف نسخة احتياطية صحيح", error=True)
            return
        if not isinstance(payload, dict):
            w.toast(app.page, "الملف ليس نسخة احتياطية صحيحة", error=True)
            return

        async def go():
            counts = await led.import_backup(payload)
            w.toast(app.page, "تم استيراد النسخة: " + "، ".join(f"{k} {v}" for k, v in counts.items()))
        w.confirm_dialog(app, "استيراد نسخة احتياطية",
                         "سيُستبدل كل ما هو موجود الآن (مخزون، فواتير، عملاء…) بمحتوى الملف. متابعة؟", go, "استيراد", True)

    async def sync_now(_):
        done = await led.sync_pending()
        w.toast(app.page, ("تمت مزامنة: " + "، ".join(done)) if done else "لا شيء بحاجة لمزامنة")
        await app.after_change()

    async def toggle_imgs(e):
        led.settings["printImages"] = bool(e.control.value)
        await led.repo.save_settings(led.settings)

    # ---- الثيمات ----
    current = (s.get("theme") or "green").lower()
    if current not in w.THEMES:
        current = "green"

    async def on_theme_change_value(value):
        name = value or "green"
        applied = w.apply_theme(name)
        led.settings["theme"] = applied
        await led.repo.save_settings(led.settings)
        # إعادة تطبيق ألوان الصفحة وإعادة رسم الواجهة
        app.page.bgcolor = w.BG
        app.body.bgcolor = w.BG
        app.bottom.bgcolor = w.PANEL
        app.sidebar.bgcolor = w.PANEL
        try:
            app.sidebar.border = w.border_side(left=True)
            app.bottom.border = w.border_side(top=True)
        except Exception:
            pass
        w.toast(app.page, f"تم تطبيق الثيم: {w.THEME_LABELS.get(applied, applied)}")
        await app.render()

    theme_dd = w.dropdown("ثيم الواجهة", current, [(k, w.THEME_LABELS.get(k, k)) for k in w.THEMES],
                          lambda v: on_theme_change_value(v))

    # ---- مزوّدو الذكاء الاصطناعي (Gemini / OpenAI / Claude / Groq / OpenRouter / محلي …) ----
    import asyncio
    from ...ai import catalogs as cat
    from ...ai.llm import PRESETS, PRESET_ORDER, ProviderSpec, check_provider, load_specs, specs_to_settings

    def _first_id() -> str:
        sp = load_specs(s)
        return next((x.id for x in sp if x.ready), sp[0].id if sp else "gemini")

    cur = {"id": _first_id()}
    key_tf = w.field("مفتاح API", "", kind="password", expand=True)
    model_tf = w.field("اسم النموذج", "", expand=True)
    base_tf = w.field("الرابط (Base URL)", "", expand=True)
    on_sw = ft.Switch(label="مفعّل", value=True)
    test_out = ft.Text("", size=12, selectable=True)
    order_out = ft.Text("", size=12, color=w.SOFT)

    def _spec_for(pid: str) -> ProviderSpec:
        return next((x for x in load_specs(led.settings) if x.id == pid), ProviderSpec.from_dict({"id": pid}))

    def _refresh_order() -> None:
        ready = [x.name for x in load_specs(led.settings) if x.ready]
        order_out.value = ("الترتيب الحالي (الأول أساسي، والبقية احتياط تلقائي عند انتهاء الحصة): " + " ← ".join(ready)
                           if ready else "لا يوجد مزوّد جاهز بعد — أضف مفتاحاً واحداً على الأقل.")

    def _fill(pid: str) -> None:
        sp = _spec_for(pid)
        key_tf.value, model_tf.value, base_tf.value, on_sw.value = sp.api_key, sp.model, sp.base_url, sp.enabled
        base_tf.disabled = sp.kind == "gemini"
        key_tf.label = "مفتاح API" + ("" if PRESETS.get(pid, {}).get("needs_key", True) else " (اختياري لهذا المزوّد)")
        _refresh_order()

    def _collect() -> ProviderSpec:
        pre = PRESETS.get(cur["id"]) or PRESETS["custom"]
        return ProviderSpec(cur["id"], pre["kind"], (key_tf.value or "").strip(), (model_tf.value or "").strip() or pre["model"],
                            (base_tf.value or "").strip().rstrip("/") or pre["base_url"], bool(on_sw.value))

    async def _persist(specs: list[ProviderSpec]) -> None:
        for sp in specs:                                  # Gemini يبقى بمفاتيحه القديمة أيضاً (توافق + .env)
            if sp.id == "gemini":
                led.settings["geminiApiKey"], led.settings["geminiModel"] = sp.api_key, sp.model
                if sp.model:
                    os.environ["GEMINI_MODEL"] = sp.model
                if sp.api_key:
                    os.environ["GEMINI_API_KEY"] = sp.api_key
                    _write_env_key(app.home, "GEMINI_API_KEY", sp.api_key)
                else:
                    os.environ.pop("GEMINI_API_KEY", None)
        led.settings.update(specs_to_settings(specs))
        await led.repo.save_settings(led.settings)
        st_ = getattr(app, "ai_state", None) or {}
        if st_:                                           # أعد بناء المساعد بالمزوّدين الجدد
            from ...ai.gemini import GeminiConfig
            st_["cfg"] = GeminiConfig.from_env()
            st_["assistant"] = None

    async def on_pick_provider(pid: str):
        cur["id"] = pid or "gemini"
        _fill(cur["id"])
        test_out.value = ""
        app.page.update()

    async def save_provider(_):
        sp = _collect()
        specs = [x for x in load_specs(led.settings) if x.id != sp.id]
        old = [x.id for x in load_specs(led.settings)]
        specs.insert(old.index(sp.id) if sp.id in old else len(specs), sp)
        await _persist(specs)
        _refresh_order()
        w.safe_update(order_out)
        w.toast(app.page, f"تم حفظ {sp.name}")

    async def make_primary(_):
        sp = _collect()
        specs = [x for x in load_specs(led.settings) if x.id != sp.id]
        await _persist([sp] + specs)
        _refresh_order()
        w.safe_update(order_out)
        w.toast(app.page, f"{sp.name} أصبح المزوّد الأساسي")

    async def test_provider(_):
        test_out.value, test_out.color = "جارٍ الاختبار…", w.SOFT
        w.safe_update(test_out)
        ok, msg = await asyncio.to_thread(check_provider, _collect())
        test_out.value, test_out.color = msg, (w.GREEN if ok else w.RED)
        w.safe_update(test_out)

    provider_dd = w.dropdown("المزوّد", cur["id"], [(k, PRESETS[k]["label"]) for k in PRESET_ORDER], on_pick_provider)
    _fill(cur["id"])

    # ---- كتالوجات الفلاتر ----
    cat_sources = cat.merged_sources(s)
    cat_rows: dict[str, dict] = {}
    cat_col = ft.Column([], spacing=10)
    auto_img_sw = ft.Switch(label="إضافة صور الكتالوج تلقائياً لأصناف المخزون (تطابق الكود التام، ولا تستبدل صورة موجودة)",
                            value=s.get("autoCatalogImages") is not False)
    cat_col.controls.append(auto_img_sw)
    for src in cat_sources:
        sw = ft.Switch(label=src.name, value=src.enabled)
        url_tf = w.field("الرابط", src.url, expand=True)
        tpl_tf = w.field("رابط بحث فيه {q} (اختياري)", src.search_url, expand=True)
        row: dict = {"sw": sw, "url": url_tf, "tpl": tpl_tf}
        extra: list[ft.Control] = []
        if src.mode == "filemaker":                       # FMI: حساب قراءة FileMaker (Data API) — يعمل على الجوال
            row["user"] = w.field("مستخدم FileMaker (قراءة فقط)", src.user, expand=True)
            row["pw"] = ft.TextField(label="كلمة مرور FileMaker", value=src.password, password=True, can_reveal_password=True,
                                     expand=True)
            row["layout"] = w.field("اسم الشاشة layout (اختياري)", src.layout, expand=True)
            extra = [w.muted("هذا الكتالوج تطبيق FileMaker WebDirect (JavaScript). لقراءته من الجوال أدخل حساب قراءة "
                             "(يتطلب تفعيل Data API في السيرفر)، أو اضغط «فتح الموقع» لتتصفحه بنفسك.", 12),
                     ft.Row([row["user"], row["pw"]], spacing=8), row["layout"]]

        def _opener(u_tf=url_tf):
            async def _go(_):
                if not await w.open_url(app.page, (u_tf.value or "").strip()):
                    w.toast(app.page, "تعذّر فتح الرابط — تأكد أنه يبدأ بـ http:// أو https://", error=True)
            return _go
        row["mode"], row["note"] = src.mode, src.note
        cat_rows[src.id] = row
        cat_col.controls.append(ft.Column(
            [ft.Row([sw, w.btn("فتح الموقع", _opener(), "ghost", small=True, icon=ft.Icons.OPEN_IN_NEW)],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN), url_tf, *extra, tpl_tf], spacing=6))
    new_name = w.field("اسم كتالوج جديد", "", expand=True)
    new_url = w.field("رابطه", "", expand=True)
    cat_q = w.field("جرّب البحث (اسم/رقم فلتر)", "", expand=True)
    cat_out = ft.Text("", size=12, selectable=True)

    def _collect_sources() -> list[cat.CatalogSource]:
        out = []
        for src in cat_sources:
            r = cat_rows[src.id]
            tpl = (r["tpl"].value or "").strip()
            out.append(cat.CatalogSource(
                src.id, src.name, (r["url"].value or "").strip(), "template" if tpl else src.mode, bool(r["sw"].value), tpl,
                src.note, (r["user"].value or "").strip() if "user" in r else src.user,
                (r["pw"].value or "") if "pw" in r else src.password,
                (r["layout"].value or "").strip() if "layout" in r else src.layout, src.db))
        return out

    async def save_catalogs(_):
        srcs = _collect_sources()
        name, url = (new_name.value or "").strip(), (new_url.value or "").strip()
        if name and url:
            srcs.append(cat.CatalogSource.from_dict({"id": name, "name": name, "url": url, "mode": "form"}))
            new_name.value = new_url.value = ""
        led.settings["catalogs"] = [x.to_dict() for x in srcs]
        led.settings["autoCatalogImages"] = bool(auto_img_sw.value)
        await led.repo.save_settings(led.settings)
        w.toast(app.page, "تم حفظ الكتالوجات" + (" — أعد فتح الإعدادات لرؤية المضاف" if name and url else ""))

    async def try_catalogs(_):
        q = (cat_q.value or "").strip()
        if not q:
            w.toast(app.page, "اكتب اسم أو رقم فلتر للتجربة", error=True)
            return
        cat_out.value, cat_out.color = "جارٍ البحث في الكتالوجات…", w.SOFT
        w.safe_update(cat_out)
        try:
            res = await cat.search_catalogs(q, _collect_sources(), None, app.home / "catalog_debug")
        except cat.CatalogError as e:
            cat_out.value, cat_out.color = str(e), w.RED
            w.safe_update(cat_out)
            return
        icon = {"ok": "✔", "empty": "∅", "error": "✖"}
        lines = []
        for r in res["sources"]:
            head = f"{icon.get(r['status'], '?')} {r['name']} ({r['via']})"
            body = (r["text"][:160].replace("\n", " ") if r["status"] == "ok" else r["error"])
            if r.get("matched_as"):
                body += f"  (نجح بصيغة: {r['matched_as']})"
            lines.append(f"{head}: {body}")
        cat_out.value, cat_out.color = "\n".join(lines), w.INK
        w.safe_update(cat_out)

    # ---- باركود المتجر ----
    barcode_preview = ft.Container(width=120, height=60, bgcolor=w.SLATE_TINT, border_radius=8,
                                   border=w.border_all(1, w.LINE), alignment=ft.Alignment(0, 0),
                                   content=ft.Text("لا يوجد", size=11, color=w.SOFT))

    async def pick_store_barcode(_):
        got = await app.pick_file_bytes(extensions=["png", "jpg", "jpeg", "webp", "gif"])
        if not got:
            return
        name, data = got
        try:
            from ...ai.media import compress_for_vision
            jpeg = compress_for_vision(data, max_side=600, quality=85)
        except Exception:
            jpeg = data
        barcode_dir = app.home / "images"
        barcode_dir.mkdir(parents=True, exist_ok=True)
        path = barcode_dir / "store_barcode.jpg"
        path.write_bytes(jpeg)
        led.settings["storeBarcode"] = str(path)
        if len(jpeg) <= 150_000:        # نسخة احتياطية داخل الإعدادات (تبقى حتى لو نُقل الملف)
            import base64
            led.settings["storeBarcodeB64"] = base64.b64encode(jpeg).decode("ascii")
        await led.repo.save_settings(led.settings)
        barcode_preview.content = w.image_from_bytes(jpeg, width=120, height=60, fit=w._contain())
        w.safe_update(barcode_preview)
        w.toast(app.page, "تم حفظ باركود المتجر")

    async def clear_store_barcode(_):
        led.settings["storeBarcode"] = ""
        led.settings["storeBarcodeB64"] = ""
        await led.repo.save_settings(led.settings)
        barcode_preview.content = ft.Text("لا يوجد", size=11, color=w.SOFT)
        w.safe_update(barcode_preview)
        w.toast(app.page, "تم حذف باركود المتجر")

    from ...printing.render import store_barcode_bytes
    _existing = store_barcode_bytes(s)
    if _existing:
        try:
            barcode_preview.content = w.image_from_bytes(_existing, width=120, height=60, fit=w._contain())
        except Exception:  # noqa: BLE001
            pass

    # ---- اتصال Supabase (رابط المشروع + المفتاح) ----
    sb_url = w.field("رابط المشروع (Project URL)", os.getenv("SUPABASE_URL", ""), hint="https://xxxxxxxx.supabase.co")
    sb_key = w.field("المفتاح (anon أو service_role)", os.getenv("SUPABASE_KEY", ""), kind="password")
    sb_out = ft.Text("", size=12)

    def _apply_store(store) -> None:
        app.store = store
        led.repo.store = store
        if getattr(app, "images", None) is not None:
            app.images.store = store

    async def connect_supabase(_):
        url = (sb_url.value or "").strip().rstrip("/")
        key = (sb_key.value or "").strip()
        if not url.startswith("https://") or "." not in url:
            sb_out.value, sb_out.color = "الرابط يجب أن يبدأ بـ https:// (من Supabase ← Project Settings ← API)", w.RED
            w.safe_update(sb_out)
            return
        if len(key) < 20:
            sb_out.value, sb_out.color = "المفتاح قصير أو فارغ — انسخه كاملاً من Project Settings ← API", w.RED
            w.safe_update(sb_out)
            return
        sb_out.value, sb_out.color = "جارٍ الاتصال…", w.SOFT
        w.safe_update(sb_out)
        try:
            from ...data.store import SupabaseStore
            store = await asyncio.wait_for(SupabaseStore.create(url, key), timeout=20)
            await asyncio.wait_for(store.keys(), timeout=20)           # اختبار فعلي للقراءة
        except Exception as e:  # noqa: BLE001
            sb_out.value, sb_out.color = f"تعذّر الاتصال — لم يُحفظ شيء ({str(e)[:160]})", w.RED
            w.safe_update(sb_out)
            return
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"] = url, key
        _write_env_key(app.home, "SUPABASE_URL", url)
        _write_env_key(app.home, "SUPABASE_KEY", key)
        _apply_store(store)
        w.toast(app.page, "تم الاتصال بـ Supabase وحُفظت الإعدادات")
        await app.refresh_cloud()
        await app.render()

    async def disconnect_supabase(_):
        async def go():
            for k in ("SUPABASE_URL", "SUPABASE_KEY"):
                os.environ.pop(k, None)
                _write_env_key(app.home, k, "")
            _apply_store(None)
            w.toast(app.page, "تم الفصل — وضع محلي فقط")
            await app.render()
        w.confirm_dialog(app, "فصل Supabase", "سيعمل البرنامج بالبيانات المحلية فقط حتى تعيد الاتصال. متابعة؟", go, "فصل", True)

    status = (w.banner("☁ متصل بـ Supabase" + (f" — بانتظار المزامنة: {', '.join(dirty)}" if dirty else " — كل شيء متزامن"),
                       "amber" if dirty else "green") if cloud
              else w.banner("وضع محلي فقط — بدون Supabase (أدخل الرابط والمفتاح في بطاقة «اتصال Supabase» أدناه)", "info"))
    return ft.Column([
        status,
        w.card(ft.Column([w.title("بيانات المحل", 15), ft.Text(s.get("businessName", "")), w.muted(s.get("tagline", "")),
                          w.muted(f"هاتف: {s.get('phone') or '—'} · رقم الشركة: {s.get('companyNumber') or '—'}"),
                          w.btn("تعديل", edit_business, "ghost")], spacing=6)),
        w.card(ft.Column([
            w.title("ثيم الواجهة والألوان", 15),
            w.muted("بدّل ألوان البرنامج بالكامل (أخضر، أسود، رمادي، أزرق، بنفسجي)"),
            theme_dd,
        ], spacing=8)),
        w.card(ft.Column([
            w.title("اتصال Supabase (السحابة)", 15),
            w.muted("ضع رابط مشروعك ومفتاحه ليحفظ البرنامج بياناتك على السحابة ويزامنها بين ويندوز والجوال. "
                    "يُفحص الاتصال فعلياً قبل الحفظ، وتبقى القيم على هذا الجهاز فقط."),
            sb_url, sb_key,
            ft.Row([w.btn("حفظ واتصال", connect_supabase, "primary", small=True, icon=ft.Icons.CLOUD_DONE),
                    *([w.btn("فصل", disconnect_supabase, "danger", small=True)] if cloud else [])], spacing=8, wrap=True),
            sb_out,
        ], spacing=8)),
        w.card(ft.Column([
            w.title("مزوّدو الذكاء الاصطناعي", 15),
            w.muted("البرنامج لا يرتبط بنموذج واحد: أضف مفتاح أي مزوّد (Gemini، OpenAI، Claude، Groq، OpenRouter، DeepSeek، Mistral، "
                    "أو نموذج محلي مجاني Ollama/LM Studio). إن انتهت حصة المزوّد الأساسي ينتقل المساعد تلقائياً للتالي."),
            provider_dd, key_tf, model_tf, base_tf, on_sw,
            ft.Row([w.btn("حفظ", save_provider, "primary", small=True),
                    w.btn("اجعله الأساسي", make_primary, "ghost", small=True),
                    w.btn("اختبار الاتصال", test_provider, "ghost", small=True, icon=ft.Icons.WIFI_TETHERING)],
                   spacing=8, wrap=True),
            test_out, order_out,
        ], spacing=8)),
        w.card(ft.Column([
            w.title("كتالوجات الفلاتر", 15),
            w.muted("يبحث المساعد في هذه الكتالوجات حين تسأله عن اسم أو رقم فلتر. فعّل/عطّل أو عدّل الروابط أو أضف كتالوجاً جديداً. "
                    "FMI يُقرأ بحساب FileMaker (Data API) على الجوال والكمبيوتر. MANN وأمثاله من مواقع JavaScript لا تُقرأ على الجوال — "
                    "استخدم زر «فتح الموقع» (على ويندوز يمكن اختيارياً: pip install playwright ثم playwright install chromium)."),
            cat_col,
            ft.Row([new_name, new_url], spacing=8),
            w.btn("حفظ الكتالوجات", save_catalogs, "primary", small=True),
            ft.Row([cat_q, w.btn("جرّب", try_catalogs, "ghost", small=True, icon=ft.Icons.SEARCH)], spacing=8),
            cat_out,
        ], spacing=8)),
        w.card(ft.Column([
            w.title("باركود المتجر", 15),
            w.muted("صورة باركود أو QR للمتجر تظهر تلقائياً في أسفل كل فاتورة بيع تُطبع أو تُصدَّر PDF"),
            ft.Row([barcode_preview,
                    ft.Column([w.btn("رفع باركود", pick_store_barcode, "ghost", small=True),
                               w.btn("حذف", clear_store_barcode, "danger", small=True)], spacing=4)],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=8)),
        w.card(ft.Row([ft.Column([w.title("طباعة الفواتير", 15), w.muted("إظهار صورة الصنف بجانب اسمه في الفاتورة وPDF")],
                                 spacing=2, expand=True),
                       ft.Switch(value=s.get("printImages") is not False, on_change=toggle_imgs)])),
        w.card(ft.Column([w.title("ترويسة طباعة المحادثة", 15),
                          w.muted(s.get("chatPrintNote", "تم التدقيق بشكل ممنهج ومنظم في كتابة المعلومات")),
                          w.muted("نوع الذكاء : " + str(s.get("chatPrintAiType", "مساعد شركة العمر"))),
                          ft.Row([logo_preview, ft.Column([
                              w.muted("شعار الطباعة (أعلى اليمين) — يُفضّل PNG بخلفية شفافة؛ الخلفية البيضاء تُزال تلقائياً", 12),
                              ft.Row([w.btn("رفع شعار", pick_chat_logo, "ghost", small=True),
                                      w.btn("الافتراضي", reset_chat_logo, "ghost", small=True)], spacing=8, wrap=True)],
                              spacing=6, expand=True)], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                          w.btn("تعديل النصوص", edit_chat_print, "ghost", small=True)], spacing=6)),
        w.card(ft.Column([w.title("القفل", 15), w.muted("مفعّل" if led.has_password else "غير مفعّل"),
                          w.btn("تغيير / إلغاء كلمة المرور", set_password, "ghost")], spacing=6)),
        w.card(ft.Column([w.title("النسخ الاحتياطي", 15), w.muted("نسخة كاملة من كل بياناتك بصيغة JSON"),
                          ft.Row([w.btn("⬇ تصدير نسخة", export), w.btn("⬆ استيراد نسخة", import_, "danger")], wrap=True, spacing=6)],
                         spacing=6)),
        *([w.card(ft.Column([w.title("المزامنة", 15), w.btn("🔄 مزامنة الآن", sync_now, "ghost")], spacing=6))] if cloud else []),
    ], scroll=w.smooth_scroll(), expand=True, spacing=10)
