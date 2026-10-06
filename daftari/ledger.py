"""طبقة الخدمات (Async): كل العمليات المحاسبية الأساسية.

مبدأ عام مختلف عن النسخة الأصلية: **تحقّق أولاً ثم عدّل**. في الأصل كان finalizeInvoice
يخصم المخزون ويحفظه ثم يكتشف أن رقم الفاتورة مكرر فيتوقف والمخزون قد نقص فعلاً.
هنا كل الفحوص قبل أي تعديل، وكل العمليات تُنفَّذ تحت قفل واحد (لا تداخل بين ضغطتين).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal

from .core.calc import (
    ProfitReport, invoice_totals, next_number, profit_report, purchase_totals, total_capital,
)
from .core.models import (
    Customer, EXPENSE_CATEGORIES, Expense, HistoryEntry, InventoryItem, Invoice, InvoiceLine,
    Purchase, PurchaseLine, Supplier, Voucher, now_iso, uid,
)
from .core.money import ZERO, q2, to_decimal
from .core.security import hash_password, verify_password
from .core.search import ImportResult, PendingItem, match_import_rows, normalize_str, RawRow
from .data.repository import Repository

DEFAULT_SETTINGS = {
    "businessName": "شركة العمر التجارية للزيوت والفلاتر",
    "tagline": "جودة ممتازة لحماية سيارتك",
    "phone": "",
    "companyNumber": "",
    "appPassword": "",
    "theme": "green",           # green | black | gray | blue | purple
    "geminiApiKey": "",         # يُفضّل حفظه محلياً؛ يُستخدم إن وُجد بدل .env
    "geminiModel": "",          # اسم النموذج (فارغ = من .env أو الافتراضي)
    "storeBarcode": "",         # مسار صورة باركود المتجر (اختياري)
    "storeBarcodeB64": "",      # نسخة احتياطية من الصورة داخل الإعدادات (base64)
}


class LedgerError(ValueError):
    """خطأ تحقق يُعرض للمستخدم كما هو (رسالة عربية)."""


def _key(name: str) -> str:
    return (name or "").strip().lower()


# ---- المسودّات (ما يبنيه المستخدم قبل الضغط على "تم") ------------------------------------
@dataclass
class InvoiceDraft:
    cart: dict[str, int] = field(default_factory=dict)
    price_overrides: dict[str, Decimal] = field(default_factory=dict)
    custom_lines: list[InvoiceLine] = field(default_factory=list)
    customer: str = ""
    discount: Decimal = ZERO
    notes: str = ""
    paid: Decimal | None = None  # None = مدفوعة بالكامل
    custom_number: str = ""
    editing_id: str | None = None
    editing_number: str | None = None
    editing_date: str | None = None
    price_tier: dict[str, str] = field(default_factory=dict)  # item_id -> retail|wholesale|distribution
    expiry: float | None = None  # مؤقت الإقفال التلقائي (epoch ثوانٍ)
    barcode_image: bytes | None = None  # صورة باركود مخصصة لهذه الفاتورة (اختياري)

    # ---- عمليات السلة (مطابقة لـ incQty/decQty/selectPriceTier) ----
    def inc(self, item: "InventoryItem") -> bool:
        cur = self.cart.get(item.id, 0)
        if cur < item.stock:
            self.cart[item.id] = cur + 1
            return True
        return False

    def dec(self, item_id: str) -> None:
        cur = self.cart.get(item_id, 0)
        if cur > 0:
            if cur == 1:
                del self.cart[item_id]
            else:
                self.cart[item_id] = cur - 1

    def select_tier(self, item: "InventoryItem", tier: str) -> None:
        price = item.price_for_tier(tier) or item.price
        self.price_tier[item.id] = tier
        if price == item.price:
            self.price_overrides.pop(item.id, None)
        else:
            self.price_overrides[item.id] = price

    def is_expired(self, now: float) -> bool:
        return self.expiry is not None and now >= self.expiry

    def remaining_text(self, now: float) -> str:
        if self.expiry is None:
            return ""
        diff = self.expiry - now
        if diff <= 0:
            return "جاري الإغلاق..."
        total_min = -(-int(diff) // 60)
        h, m = divmod(total_min, 60)
        return (f"{h}س " if h else "") + f"{m}د متبقية"

    def clear(self) -> None:
        self.cart.clear(); self.price_overrides.clear(); self.custom_lines.clear(); self.price_tier.clear()
        self.customer = self.notes = self.custom_number = ""
        self.discount = ZERO; self.paid = None; self.expiry = None
        self.editing_id = self.editing_number = self.editing_date = None
        self.barcode_image = None


@dataclass
class PurchaseDraft:
    cart: dict[str, int] = field(default_factory=dict)
    costs: dict[str, Decimal] = field(default_factory=dict)
    supplier: str = ""
    supplier_invoice_number: str = ""
    paid: Decimal | None = None
    notes: str = ""
    pending: list[PendingItem] = field(default_factory=list)
    editing_id: str | None = None
    editing_number: str | None = None
    editing_date: str | None = None
    # بنود كانت في الفاتورة ثم حُذف صنفها من المخزون: تبقى كما هي (اسم/كود/كمية/تكلفة) ولا تمسّ المخزون
    orphans: list = field(default_factory=list)


class Ledger:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.inventory: list[InventoryItem] = []
        self.invoices: list[Invoice] = []
        self.customers: list[Customer] = []
        self.suppliers: list[Supplier] = []
        self.expenses: list[Expense] = []
        self.purchases: list[Purchase] = []
        self.vouchers: list[Voucher] = []
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self.last_loaded: float = 0.0
        self._lock = asyncio.Lock()

    # ---- تحميل/حفظ -----------------------------------------------------------------------
    async def load(self, cloud: bool = True) -> None:
        """cloud=False: يقرأ الكاش المحلي فقط (فوري) — يُستخدم عند فتح البرنامج ثم نحدّث من السحابة بالخلفية."""
        if cloud and self.repo.store is not None and hasattr(self.repo.store, "get_many"):
            # طلب شبكة واحد لكل الجداول (بدل 8 طلبات) — أسرع بكثير
            t = await self.repo.load_many([InventoryItem, Invoice, Supplier, Customer, Expense, Purchase, Voucher])
            settings = await self.repo.load_settings(DEFAULT_SETTINGS) if self.repo.load_errors else \
                self.repo._cache_read("settings", DEFAULT_SETTINGS)
            self.inventory, self.invoices, self.suppliers = t["inventory"], t["invoices"], t["suppliers"]
            self.customers, self.expenses, self.purchases, self.vouchers = (
                t["customers"], t["expenses"], t["purchases"], t["vouchers"])
        elif not cloud:
            t = await self.repo.load_many([InventoryItem, Invoice, Supplier, Customer, Expense, Purchase, Voucher],
                                          cloud=False)
            settings = self.repo._cache_read("settings", DEFAULT_SETTINGS)
            self.inventory, self.invoices, self.suppliers = t["inventory"], t["invoices"], t["suppliers"]
            self.customers, self.expenses, self.purchases, self.vouchers = (
                t["customers"], t["expenses"], t["purchases"], t["vouchers"])
        else:
            (self.inventory, self.invoices, self.suppliers, self.customers,
             self.expenses, self.purchases, self.vouchers, settings) = await asyncio.gather(
                self.repo.load(InventoryItem), self.repo.load(Invoice), self.repo.load(Supplier),
                self.repo.load(Customer), self.repo.load(Expense), self.repo.load(Purchase),
                self.repo.load(Voucher), self.repo.load_settings(DEFAULT_SETTINGS),
            )
        if not isinstance(settings, dict):
            settings = {}
        self.settings = {**DEFAULT_SETTINGS, **settings}
        if cloud:
            await self._migrate_they_owe()
            self.last_loaded = time.time()

    async def reload(self, force: bool = False) -> bool:
        """يعيد قراءة كل البيانات من السحابة (لإظهار ما أضافه جهاز آخر).

        لا يُنفَّذ إن كانت هناك تغييرات محلية لم تُرفع بعد (حتى لا تضيع)، إلا مع force.
        يرجع True إن حدثت إعادة قراءة فعلية.
        """
        if self.repo.dirty and not force:
            return False
        async with self._lock:
            await self.load()
        return True

    def health_notice(self) -> str | None:
        """رسالة تشخيص عربية إن كان تحميل البيانات من Supabase فاشلاً أو فارغاً."""
        repo = self.repo
        if repo.store is None:
            return None
        if repo.load_errors:
            first_key, err = next(iter(repo.load_errors.items()))
            return (f"تعذّر قراءة «{first_key}» من Supabase: {err[:160]} — تحقق من المفتاح، ومن سياسة RLS "
                    f"على جدول app_data (بدون policy للقراءة تظهر القوائم فارغة).")
        if "inventory" in repo.missing_keys and not self.inventory:
            return ("اتصل البرنامج بـ Supabase لكن لا يوجد سجل باسم «inventory» في جدول app_data — "
                    "تأكد أن المشروع والجدول هما نفسهما اللذان كانت تحفظ فيهما النسخة القديمة.")
        return None

    async def _migrate_they_owe(self) -> None:
        """هجرة قديمة: سجلات type=they_owe في الموردين تنتقل للعملاء (كما في init الأصلي)."""
        legacy = [s for s in self.suppliers if s.type == "they_owe"]
        if not legacy:
            return
        for s in legacy:
            cust = self._find_customer(s.name)
            if cust:
                cust.balance += s.balance
                cust.history += s.history
            else:
                self.customers.append(Customer(name=s.name, balance=s.balance, history=list(s.history)))
            self.repo.mark_deleted("suppliers", s.id)
        self.suppliers = [s for s in self.suppliers if s.type != "they_owe"]
        await self._save_suppliers()
        await self._save_customers()

    async def _save_inventory(self): self.inventory = await self.repo.save(InventoryItem, self.inventory)
    async def _save_invoices(self): self.invoices = await self.repo.save(Invoice, self.invoices)
    async def _save_customers(self): self.customers = await self.repo.save(Customer, self.customers)
    async def _save_suppliers(self): self.suppliers = await self.repo.save(Supplier, self.suppliers)
    async def _save_expenses(self): self.expenses = await self.repo.save(Expense, self.expenses)
    async def _save_purchases(self): self.purchases = await self.repo.save(Purchase, self.purchases)
    async def _save_vouchers(self): self.vouchers = await self.repo.save(Voucher, self.vouchers)

    # ---- بحث داخلي ---------------------------------------------------------------------------
    def _item(self, item_id: str) -> InventoryItem | None:
        return next((i for i in self.inventory if i.id == item_id), None)

    def _find_customer(self, name: str) -> Customer | None:
        k = _key(name)
        return next((c for c in self.customers if _key(c.name) == k), None) if k else None

    def _find_supplier(self, name: str) -> Supplier | None:
        k = _key(name)
        return next((s for s in self.suppliers if s.type == "we_owe" and _key(s.name) == k), None) if k else None

    # ---- أسماء الحسابات للسندات: مطابقة تامة، ثم مطابقة بعد توحيد الحروف، ثم اقتراحات مقاربة ------------------
    def _accounts_for(self, vtype: str):
        return list(self.customers) if vtype == "receipt" else [s for s in self.suppliers if s.type == "we_owe"]

    def match_account(self, vtype: str, name: str):
        """الحساب المطابق للاسم: تطابق تام (بلا حساسية لحالة الأحرف) ثم تطابق بعد توحيد (أ/ا، ة/ه، ى/ي، التشكيل) إن كان وحيداً."""
        from .core.search import normalize_str
        k = _key(name)
        if not k:
            return None
        pool = self._accounts_for(vtype)
        hit = next((a for a in pool if _key(a.name) == k), None)
        if hit:
            return hit
        nk = normalize_str(name)
        cands = [a for a in pool if nk and normalize_str(a.name) == nk]
        return cands[0] if len(cands) == 1 else None

    def suggest_accounts(self, vtype: str, text: str, limit: int = 6) -> list[tuple[str, Decimal]]:
        """أسماء عملاء (للقبض) أو موردين (للدفع) تقارب ما كُتب، الأقرب أولاً. يرجع [(الاسم، الرصيد)]."""
        from .core.search import fuzzy_match, normalize_str
        t = (text or "").strip()
        if not t:
            return []
        nt = normalize_str(t)
        scored = []
        for a in self._accounts_for(vtype):
            if not fuzzy_match(a.name, t):
                continue
            na = normalize_str(a.name)
            rank = 0 if na == nt else 1 if na.startswith(nt) else 2 if nt in na else 3
            scored.append((rank, -a.balance, a.name, a.balance))
        scored.sort()
        return [(n, b) for _, _, n, b in scored[:limit]]

    # ===================================== المخزون =============================================
    async def add_item(self, name: str, code: str, cost, price, stock,
                       price_wholesale=None, price_distribution=None, img: str | None = None) -> InventoryItem:
        name = name.strip()
        cost, price = to_decimal(cost), to_decimal(price)
        if not name:
            raise LedgerError("اكتب اسم الصنف")
        if cost < 0:
            raise LedgerError("أدخل رأس مال صحيح")
        if price < 0:
            raise LedgerError("أدخل سعر مفرق صحيح")
        if int(stock) < 0:
            raise LedgerError("أدخل كمية صحيحة")
        item = InventoryItem(
            name=name, code=code.strip(), cost=q2(cost), price=q2(price), stock=int(stock),
            price_wholesale=q2(price if price_wholesale in (None, "") else price_wholesale),
            price_distribution=q2(price if price_distribution in (None, "") else price_distribution),
            img=img,
        )
        async with self._lock:
            self.inventory.append(item)
            await self._save_inventory()
        return item

    async def restock_by_code(self, code: str, qty: int) -> InventoryItem:
        if not code.strip():
            raise LedgerError("اكتب الكود")
        if qty <= 0:
            raise LedgerError("أدخل كمية صحيحة")
        nc = normalize_str(code)
        async with self._lock:
            it = next((i for i in self.inventory if (i.code or "").strip().lower() == code.strip().lower()), None) \
                or next((i for i in self.inventory if normalize_str(i.code) == nc), None)
            if not it:
                raise LedgerError("لم يتم العثور على صنف بهذا الكود")
            it.stock += qty
            await self._save_inventory()
            return it

    async def delete_item(self, item_id: str) -> None:
        async with self._lock:
            self.inventory = [i for i in self.inventory if i.id != item_id]
            self.repo.mark_deleted("inventory", item_id)
            await self._save_inventory()

    def capital(self) -> Decimal:
        return total_capital(self.inventory)

    # ===================================== فواتير البيع ==========================================
    def _build_invoice_lines(self, d: InvoiceDraft) -> list[InvoiceLine]:
        lines: list[InvoiceLine] = []
        for iid, qty in d.cart.items():
            if qty <= 0:
                continue
            it = self._item(iid)
            if not it:
                raise LedgerError("أحد أصناف الفاتورة لم يعد موجوداً بالمخزون")
            price = d.price_overrides.get(iid, it.price)
            lines.append(InvoiceLine(id=it.id, name=it.name, code=it.code, price=q2(price), cost=it.cost, qty=qty))
        for cl in d.custom_lines:
            lines.append(InvoiceLine(id=cl.id, name=cl.name, price=q2(cl.price), cost=q2(cl.cost),
                                     qty=cl.qty, is_custom=True))
        return lines

    async def finalize_invoice(self, d: InvoiceDraft) -> tuple[Invoice, list[str]]:
        """يرجع (الفاتورة، تحذيرات). التحذيرات مثل: الكمية المطلوبة أكبر من المخزون."""
        async with self._lock:
            lines = self._build_invoice_lines(d)
            if not lines:
                raise LedgerError("الفاتورة فارغة")
            # --- كل الفحوص قبل أي تعديل ---
            if d.editing_id:
                number = d.editing_number or ""
            else:
                custom = d.custom_number.strip()
                if custom:
                    if any(i.number.lower() == custom.lower() for i in self.invoices):
                        raise LedgerError("رقم الفاتورة هذا مستخدم من قبل — اختر رقمًا آخر")
                    number = custom
                else:
                    number = next_number("INV-", (i.number for i in self.invoices), 1001)

            totals = invoice_totals(lines, d.discount, d.paid)
            warnings: list[str] = []
            for ln in lines:
                if ln.is_custom:
                    continue
                it = self._item(ln.id)
                if it and ln.qty > it.stock:
                    warnings.append(f"«{it.name}»: المطلوب {ln.qty} والمتوفر {it.stock} — سيصبح المخزون 0")

            cust = self._find_customer(d.customer) if d.customer.strip() else None
            prior = cust.balance if cust else ZERO

            # --- التنفيذ ---
            for ln in lines:
                it = None if ln.is_custom else self._item(ln.id)
                if it:
                    it.stock = max(0, it.stock - ln.qty)

            inv = Invoice(
                id=d.editing_id or uid(), number=number,
                date=d.editing_date if d.editing_id else now_iso(),
                edited_at=now_iso() if d.editing_id else None,
                customer=d.customer.strip(), items=lines, subtotal=totals.subtotal,
                discount=totals.discount, notes=d.notes.strip(), total=totals.total,
                paid=totals.paid, remaining=totals.remaining,
                customer_prior_debt=prior, customer_total_debt_after=prior + totals.remaining,
            )
            # احفظ باركود الفاتورة (إن وُجد) كـ base64 في extra ليظهر عند إعادة الطباعة
            if d.barcode_image:
                import base64
                inv.extra["barcode"] = base64.b64encode(d.barcode_image).decode("ascii")
            if d.editing_id:
                self.invoices = [inv] + [i for i in self.invoices if i.id != d.editing_id]
            else:
                self.invoices.insert(0, inv)
            # الجداول الثلاثة مستقلة (لكلٍّ قفله): نحفظها بالتوازي بدل التتابع — كل حفظ سحابي رحلتا شبكة (قراءة ثم كتابة)،
            # فيقلّ زمن «حفظ الفاتورة» إلى زمن أبطأ جدول بدل مجموع الثلاثة.
            jobs = [self._save_inventory(), self._save_invoices()]
            if inv.remaining > 0 and inv.customer:
                jobs.append(self._add_customer_debt(inv.customer, inv.remaining, invoice_number=number))
            await asyncio.gather(*jobs)
            return inv, warnings

    async def _add_customer_debt(self, name: str, amount: Decimal, invoice_number: str) -> None:
        entry = HistoryEntry(date=now_iso(), type="invoice_debt", amount=amount, invoice_number=invoice_number)
        c = self._find_customer(name)
        if c:
            c.balance += amount
            c.history.append(entry)
        else:
            self.customers.append(Customer(name=name.strip(), balance=amount, history=[entry]))
        await self._save_customers()

    async def _reverse_customer_debt(self, inv: Invoice) -> None:
        if inv.remaining > 0 and inv.customer:
            c = self._find_customer(inv.customer)
            if c:
                c.balance = max(ZERO, c.balance - inv.remaining)
                idx = next((i for i, h in enumerate(c.history) if h.invoice_number == inv.number), -1)
                if idx > -1:
                    self.repo.mark_history_deleted("customers", c.history[idx].signature())
                    c.history.pop(idx)
                await self._save_customers()

    async def delete_invoice(self, invoice_id: str) -> None:
        async with self._lock:
            inv = next((i for i in self.invoices if i.id == invoice_id), None)
            if not inv:
                raise LedgerError("الفاتورة غير موجودة")
            for ln in inv.items:
                it = self._item(ln.id)
                if it and not ln.is_custom:
                    it.stock += ln.qty
            await self._save_inventory()
            await self._reverse_customer_debt(inv)
            self.invoices = [i for i in self.invoices if i.id != invoice_id]
            self.repo.mark_deleted("invoices", invoice_id)
            await self._save_invoices()

    async def begin_edit_invoice(self, invoice_id: str) -> tuple[InvoiceDraft, list[str]]:
        """يفتح فاتورة للتعديل: يعيد كمياتها للمخزون ويلغي دينها مؤقتاً، ويرجع مسودة.

        إن تراجع المستخدم استدعِ cancel_edit_invoice(draft) — الأصل كان يترك المخزون
        والدين معدَّلين بدون فاتورة مقابلة إن أُغلقت الشاشة بدون حفظ.
        """
        async with self._lock:
            inv = next((i for i in self.invoices if i.id == invoice_id), None)
            if not inv:
                raise LedgerError("الفاتورة غير موجودة")
            for ln in inv.items:
                it = self._item(ln.id)
                if it and not ln.is_custom:
                    it.stock += ln.qty
            await self._save_inventory()
            await self._reverse_customer_debt(inv)

            d = InvoiceDraft(customer=inv.customer, discount=inv.discount, notes=inv.notes,
                             paid=inv.paid if inv.remaining > 0 else None,
                             editing_id=inv.id, editing_number=inv.number, editing_date=inv.date)
            missing: list[str] = []
            for ln in inv.items:
                if ln.is_custom:
                    d.custom_lines.append(InvoiceLine(name=ln.name, price=ln.price, cost=ln.cost, qty=ln.qty, is_custom=True))
                    continue
                it = self._item(ln.id)
                if not it:
                    missing.append(ln.name)
                    continue
                d.cart[ln.id] = ln.qty
                if ln.price != it.price:
                    d.price_overrides[ln.id] = ln.price
            warnings = [f"الصنف «{n}» لم يعد موجوداً بالمخزون وتم تجاهله" for n in missing]
            return d, warnings

    async def cancel_edit_invoice(self, d: InvoiceDraft) -> None:
        """تراجع عن التعديل: أعد خصم المخزون وقيد الدين للفاتورة الأصلية كما كانت."""
        async with self._lock:
            inv = next((i for i in self.invoices if i.id == d.editing_id), None)
            if not inv:
                return
            for ln in inv.items:
                it = self._item(ln.id)
                if it and not ln.is_custom:
                    it.stock = max(0, it.stock - ln.qty)
            await self._save_inventory()
            if inv.remaining > 0 and inv.customer:
                await self._add_customer_debt(inv.customer, inv.remaining, invoice_number=inv.number)

    # ===================================== فواتير الشراء =========================================
    async def finalize_purchase(self, d: PurchaseDraft) -> Purchase:
        async with self._lock:
            lines: list[PurchaseLine] = []
            for iid, qty in d.cart.items():
                if qty <= 0:
                    continue
                it = self._item(iid)
                if not it:
                    raise LedgerError("أحد أصناف فاتورة الشراء لم يعد موجوداً بالمخزون")
                lines.append(PurchaseLine(id=it.id, name=it.name, code=it.code, qty=qty,
                                          cost=q2(d.costs.get(iid, it.cost))))
            lines.extend(PurchaseLine(id=o.id, name=o.name, code=o.code, qty=o.qty, cost=o.cost)
                         for o in d.orphans)          # أصناف حُذفت من المخزون: تبقى في الفاتورة كما كانت
            if not lines:
                raise LedgerError("فاتورة الشراء فارغة")
            if not d.supplier.strip():
                raise LedgerError("اكتب اسم المورد قبل إنشاء فاتورة الشراء")

            totals = purchase_totals(lines, d.paid)
            number = d.editing_number if d.editing_id else next_number("PUR-", (p.number for p in self.purchases), 1001)

            for ln in lines:
                it = self._item(ln.id)
                if it:
                    it.stock += ln.qty
                    it.cost = ln.cost  # آخر تكلفة شراء تصبح تكلفة الصنف
            await self._save_inventory()

            p = Purchase(
                id=d.editing_id or uid(), number=number or "", supplier=d.supplier.strip(),
                supplier_invoice_number=d.supplier_invoice_number.strip(),
                date=d.editing_date if d.editing_id else now_iso(),
                edited_at=now_iso() if d.editing_id else None, items=lines,
                subtotal=totals.subtotal, paid=totals.paid, remaining=totals.remaining, notes=d.notes.strip(),
            )
            if d.editing_id:
                self.purchases = [p] + [x for x in self.purchases if x.id != d.editing_id]
            else:
                self.purchases.insert(0, p)
            await self._save_purchases()

            if p.remaining > 0 and p.supplier:
                entry = HistoryEntry(date=now_iso(), type="purchase_debt", amount=p.remaining, purchase_number=p.number)
                s = self._find_supplier(p.supplier)
                if s:
                    s.balance += p.remaining
                    s.history.append(entry)
                else:
                    self.suppliers.append(Supplier(name=p.supplier, type="we_owe", balance=p.remaining, history=[entry]))
                await self._save_suppliers()
            return p

    async def _reverse_purchase(self, p: Purchase) -> None:
        for ln in p.items:
            it = self._item(ln.id)
            if it:
                it.stock = max(0, it.stock - ln.qty)
        await self._save_inventory()
        if p.remaining > 0 and p.supplier:
            s = self._find_supplier(p.supplier)
            if s:
                s.balance = max(ZERO, s.balance - p.remaining)
                idx = next((i for i, h in enumerate(s.history) if h.purchase_number == p.number), -1)
                if idx > -1:
                    self.repo.mark_history_deleted("suppliers", s.history[idx].signature())
                    s.history.pop(idx)
                await self._save_suppliers()

    async def delete_purchase(self, purchase_id: str) -> None:
        async with self._lock:
            p = next((x for x in self.purchases if x.id == purchase_id), None)
            if not p:
                raise LedgerError("فاتورة الشراء غير موجودة")
            await self._reverse_purchase(p)
            self.purchases = [x for x in self.purchases if x.id != purchase_id]
            self.repo.mark_deleted("purchases", purchase_id)
            await self._save_purchases()

    async def begin_edit_purchase(self, purchase_id: str) -> tuple[PurchaseDraft, list[str]]:
        async with self._lock:
            p = next((x for x in self.purchases if x.id == purchase_id), None)
            if not p:
                raise LedgerError("فاتورة الشراء غير موجودة")
            await self._reverse_purchase(p)
            d = PurchaseDraft(supplier=p.supplier, supplier_invoice_number=p.supplier_invoice_number,
                              paid=p.paid if p.remaining > 0 else None, notes=p.notes,
                              editing_id=p.id, editing_number=p.number, editing_date=p.date)
            warnings = []
            for ln in p.items:
                if not self._item(ln.id):
                    d.orphans.append(PurchaseLine(id=ln.id, name=ln.name, code=ln.code, qty=ln.qty, cost=ln.cost))
                    warnings.append(f"الصنف «{ln.name}» محذوف من المخزون — بقي في الفاتورة كما هو ولن يؤثر على المخزون")
                    continue
                d.cart[ln.id] = ln.qty
                d.costs[ln.id] = ln.cost
            return d, warnings

    async def add_new_item_to_purchase(self, d: PurchaseDraft, name: str, code: str, qty: int, cost,
                                       price=None, price_wholesale=None, price_distribution=None,
                                       img: str | None = None) -> InventoryItem:
        """صنف جديد أثناء الشراء، مع حارس التكرار (بالاسم أو الكود)."""
        name, code = name.strip(), code.strip()
        cost = to_decimal(cost)
        if not name:
            raise LedgerError("اكتب اسم الصنف")
        if qty <= 0:
            raise LedgerError("أدخل كمية صحيحة")
        if cost < 0:
            raise LedgerError("أدخل تكلفة شراء صحيحة")
        nn, nc = normalize_str(name), normalize_str(code)
        dup = next((i for i in self.inventory if normalize_str(i.name) == nn or (nc and normalize_str(i.code) == nc)), None)
        if dup:
            raise LedgerError(f"الصنف «{dup.name}» موجود أصلاً بالمخزون — استخدم البحث بدل إضافته كصنف جديد")
        price_v = cost if price in (None, "") else to_decimal(price)
        async with self._lock:
            item = InventoryItem(
                name=name, code=code, cost=q2(cost), price=q2(price_v), stock=0,
                price_wholesale=q2(price_v if price_wholesale in (None, "") else price_wholesale),
                price_distribution=q2(price_v if price_distribution in (None, "") else price_distribution),
                img=img,
            )
            self.inventory.append(item)
            await self._save_inventory()
        d.cart[item.id], d.costs[item.id] = qty, q2(cost)
        return item

    # ---- استيراد (Excel / OCR) -------------------------------------------------------------------
    def apply_import(self, d: PurchaseDraft, rows: list[RawRow]) -> ImportResult:
        """الأصناف المطابقة تدخل السلة مباشرة؛ الجديدة تنتظر في d.pending حتى تُسعَّر."""
        res = match_import_rows(rows, self.inventory)
        for item, qty, cost in res.matched:
            d.cart[item.id] = d.cart.get(item.id, 0) + qty
            d.costs[item.id] = q2(cost)
        d.pending.extend(res.pending)
        return res

    async def confirm_pending(self, d: PurchaseDraft) -> int:
        """يحوّل الأصناف الجديدة (بعد تسعيرها) إلى مخزون + سلة الشراء."""
        for p in d.pending:
            if not p.name.strip() or p.qty <= 0 or p.cost < 0:
                raise LedgerError("في صنف ناقص بيانات (اسم/كمية/تكلفة) — كمّل الخانات قبل التأكيد")
            if p.price is None or p.price < 0:
                raise LedgerError(f"أدخل سعر المفرق للصنف «{p.name}» قبل التأكيد")
        async with self._lock:
            for p in d.pending:
                item = InventoryItem(
                    name=p.name.strip(), code=p.code.strip(), cost=q2(p.cost), price=q2(p.price), stock=0,
                    price_wholesale=q2(p.price if p.price_wholesale is None else p.price_wholesale),
                    price_distribution=q2(p.price if p.price_distribution is None else p.price_distribution),
                )
                self.inventory.append(item)
                d.cart[item.id], d.costs[item.id] = p.qty, q2(p.cost)
            n = len(d.pending)
            d.pending.clear()
            await self._save_inventory()
            return n

    # ===================================== العملاء والموردون =====================================
    async def add_customer(self, name: str, phone: str = "") -> Customer:
        if not name.strip():
            raise LedgerError("اكتب اسم الزبون")
        async with self._lock:
            c = Customer(name=name.strip(), phone=phone.strip())
            self.customers.append(c)
            await self._save_customers()
            return c

    async def add_supplier(self, name: str, amount) -> Supplier:
        amount = to_decimal(amount)
        if not name.strip():
            raise LedgerError("اكتب الاسم")
        if amount < 0:
            raise LedgerError("أدخل مبلغ صحيح")
        async with self._lock:
            s = Supplier(name=name.strip(), type="we_owe", balance=q2(amount),
                         history=[HistoryEntry(date=now_iso(), type="initial", amount=q2(amount))])
            self.suppliers.append(s)
            await self._save_suppliers()
            return s

    async def add_manual_customer_debt(self, customer_id: str, amount, note: str = "") -> Customer:
        amount = to_decimal(amount)
        if amount <= 0:
            raise LedgerError("أدخل مبلغ صحيح")
        async with self._lock:
            c = next((x for x in self.customers if x.id == customer_id), None)
            if not c:
                raise LedgerError("العميل غير موجود")
            c.balance += q2(amount)
            c.history.append(HistoryEntry(date=now_iso(), type="manual_debt", amount=q2(amount), note=note.strip() or None))
            await self._save_customers()
            return c

    async def record_customer_payment(self, customer_id: str, amount) -> Voucher:
        """سند قبض من عميل."""
        return await self._record_payment("receipt", customer_id, amount)

    async def record_supplier_payment(self, supplier_id: str, amount) -> Voucher:
        """سند دفع لمورد."""
        return await self._record_payment("payment", supplier_id, amount)

    async def _record_payment(self, vtype: str, account_id: str, amount) -> Voucher:
        amount = q2(to_decimal(amount))
        if amount <= 0:
            raise LedgerError("أدخل مبلغ صحيح")
        async with self._lock:
            acc = next((x for x in (self.customers if vtype == "receipt" else self.suppliers) if x.id == account_id), None)
            if not acc:
                raise LedgerError("الحساب غير موجود")
            number = self._voucher_number(vtype)
            prev = acc.balance
            acc.balance = max(ZERO, acc.balance - amount)
            acc.history.append(HistoryEntry(date=now_iso(), type="payment", amount=amount, voucher_number=number))
            v = Voucher(number=number, type=vtype, person=acc.name, amount=amount, previous_balance=prev,
                        remaining_balance=acc.balance, date=now_iso(), applied_to_id=acc.id)
            self.vouchers.insert(0, v)
            await (self._save_customers() if vtype == "receipt" else self._save_suppliers())
            await self._save_vouchers()
            return v

    def _voucher_number(self, vtype: str) -> str:
        prefix = "RCV-" if vtype == "receipt" else "PAY-"
        return next_number(prefix, (v.number for v in self.vouchers if v.type == vtype), 1001)

    async def add_voucher(self, vtype: str, person: str, amount, note: str = "", ref_number: str = "",
                          previous_balance=None) -> Voucher:
        """سند يدوي (قبض/صرف). يُخصم من رصيد العميل/المورد إن وُجد اسم مطابق."""
        amount = q2(to_decimal(amount))
        if vtype not in ("receipt", "payment"):
            raise LedgerError("نوع السند غير صحيح")
        if not person.strip():
            raise LedgerError("اكتب الاسم")
        if amount <= 0:
            raise LedgerError("أدخل مبلغ صحيح")
        async with self._lock:
            number = self._voucher_number(vtype)
            prev = None if previous_balance in (None, "") else to_decimal(previous_balance)
            applied, remaining = None, None
            acc = self.match_account(vtype, person)
            if acc:
                if prev is None:
                    prev = acc.balance
                if acc.balance > 0:
                    acc.balance = max(ZERO, acc.balance - amount)
                    acc.history.append(HistoryEntry(date=now_iso(), type="payment", amount=amount, voucher_number=number))
                    applied = acc.id
                    await (self._save_customers() if vtype == "receipt" else self._save_suppliers())
                remaining = acc.balance
            if applied is None and prev is not None:
                remaining = prev - amount
            v = Voucher(number=number, type=vtype, person=person.strip(), amount=amount, note=note.strip(),
                        ref_number=ref_number.strip() or None, previous_balance=prev, remaining_balance=remaining,
                        date=now_iso(), applied_to_id=applied)
            self.vouchers.insert(0, v)
            await self._save_vouchers()
            return v

    async def delete_voucher(self, voucher_id: str) -> None:
        async with self._lock:
            v = next((x for x in self.vouchers if x.id == voucher_id), None)
            if not v:
                raise LedgerError("السند غير موجود")
            if v.applied_to_id:
                pool = self.customers if v.type == "receipt" else self.suppliers
                acc = next((x for x in pool if x.id == v.applied_to_id), None)
                if acc:
                    acc.balance += v.amount
                    idx = next((i for i, h in enumerate(acc.history) if h.voucher_number == v.number), -1)
                    if idx > -1:
                        self.repo.mark_history_deleted("customers" if v.type == "receipt" else "suppliers",
                                                       acc.history[idx].signature())
                        acc.history.pop(idx)
                    await (self._save_customers() if v.type == "receipt" else self._save_suppliers())
            self.vouchers = [x for x in self.vouchers if x.id != voucher_id]
            self.repo.mark_deleted("vouchers", voucher_id)
            await self._save_vouchers()

    # ===================================== مصاريف وتقارير ===========================================
    async def add_expense(self, category: str, amount, note: str = "", period: str = "") -> Expense:
        amount = q2(to_decimal(amount))
        if amount <= 0:
            raise LedgerError("أدخل مبلغ صحيح")
        if category not in EXPENSE_CATEGORIES:
            raise LedgerError("فئة المصروف غير معروفة")
        async with self._lock:
            ex = Expense(category=category, amount=amount, note=note.strip(), period=period.strip(), date=now_iso())
            self.expenses.insert(0, ex)
            await self._save_expenses()
            return ex

    async def delete_expense(self, expense_id: str) -> None:
        async with self._lock:
            self.expenses = [e for e in self.expenses if e.id != expense_id]
            self.repo.mark_deleted("expenses", expense_id)
            await self._save_expenses()

    def profit(self) -> ProfitReport:
        return profit_report(self.invoices, self.expenses)

    # ===================================== إكمال العمليات الإدارية ====================================
    async def discard_editing_invoice(self, d: InvoiceDraft) -> None:
        """«تفريغ» فاتورة قيد التعديل = حذفها نهائياً.

        مخزونها ودينها عُكسا أصلاً عند فتحها للتعديل، فلا نلمسهما هنا. (استدعاء delete_invoice
        كان سيعيد الكميات للمخزون مرة ثانية.) مطابق لـ clearCart() في الأصل بعد إصلاح الخطأ فيه.
        """
        if not d.editing_id:
            return
        async with self._lock:
            self.invoices = [i for i in self.invoices if i.id != d.editing_id]
            self.repo.mark_deleted("invoices", d.editing_id)
            await self._save_invoices()

    async def discard_editing_purchase(self, d: PurchaseDraft) -> None:
        if not d.editing_id:
            return
        async with self._lock:
            self.purchases = [p for p in self.purchases if p.id != d.editing_id]
            self.repo.mark_deleted("purchases", d.editing_id)
            await self._save_purchases()

    async def set_invoice_payment_status(self, invoice_id: str, status: str) -> None:
        if status not in ("paid", "unpaid"):
            raise LedgerError("حالة غير صحيحة")
        async with self._lock:
            inv = next((i for i in self.invoices if i.id == invoice_id), None)
            if not inv:
                raise LedgerError("الفاتورة غير موجودة")
            inv.payment_status_manual = status
            await self._save_invoices()

    # ---- المخزون: تعديل ------------------------------------------------------------------------
    async def edit_item(self, item_id: str, name: str, code: str, stock) -> InventoryItem:
        name = name.strip()
        try:
            stock_i = int(to_decimal(stock))
        except Exception:  # noqa: BLE001
            stock_i = -1
        if not name or stock_i < 0:
            raise LedgerError("بيانات غير صحيحة")
        async with self._lock:
            it = self._item(item_id)
            if not it:
                raise LedgerError("الصنف غير موجود")
            it.name, it.code, it.stock = name, code.strip(), stock_i
            await self._save_inventory()
            return it

    async def set_item_image(self, item_id: str, img: str | None) -> InventoryItem:
        """يحدّث بصمة صورة الصنف فقط (الصورة تُحفظ عبر ImageStore)."""
        async with self._lock:
            it = self._item(item_id)
            if not it:
                raise LedgerError("الصنف غير موجود")
            it.img = img
            await self._save_inventory()
            return it

    async def edit_item_pricing(self, item_id: str, cost, wholesale, distribution, price) -> InventoryItem:
        vals = [to_decimal(v, default=Decimal(-1)) if v not in (None, "") else Decimal(-1)
                for v in (cost, wholesale, distribution, price)]
        if any(v < 0 for v in vals):
            raise LedgerError("بيانات غير صحيحة")
        async with self._lock:
            it = self._item(item_id)
            if not it:
                raise LedgerError("الصنف غير موجود")
            it.cost, it.price_wholesale, it.price_distribution, it.price = (q2(v) for v in vals)
            await self._save_inventory()
            return it

    async def update_purchase_pricing(self, purchase_id: str, rows: dict[str, tuple]) -> tuple[int, int]:
        """تعديل أسعار البيع لأصناف فاتورة شراء. rows[item_id] = (retail, wholesale|None, distribution|None).
        يرجع (المحدَّث، الذي لم يعد موجوداً). سعر المفرق مطلوب؛ الجملة والتوزيع افتراضيهما = المفرق."""
        p = next((x for x in self.purchases if x.id == purchase_id), None)
        if not p:
            raise LedgerError("فاتورة الشراء غير موجودة")
        for ln in p.items:
            row = rows.get(ln.id)
            if row and (row[0] in (None, "") or to_decimal(row[0], default=Decimal(-1)) < 0):
                raise LedgerError(f"أدخل سعر مفرق صحيح للصنف «{ln.name}»")
        updated = missing = 0
        async with self._lock:
            for ln in p.items:
                row = rows.get(ln.id)
                if not row:
                    continue
                it = self._item(ln.id)
                if not it:
                    missing += 1
                    continue
                retail = q2(row[0])
                it.price = retail
                it.price_wholesale = q2(row[1]) if row[1] not in (None, "") else retail
                it.price_distribution = q2(row[2]) if row[2] not in (None, "") else retail
                updated += 1
            await self._save_inventory()
        return updated, missing

    # ---- العملاء والموردون: تعديل/حذف -----------------------------------------------------------
    async def edit_customer(self, customer_id: str, name: str, phone: str, balance) -> Customer:
        return await self._edit_account(self.customers, customer_id, name, balance, phone, self._save_customers)

    async def edit_supplier(self, supplier_id: str, name: str, balance) -> Supplier:
        return await self._edit_account(self.suppliers, supplier_id, name, balance, None, self._save_suppliers)

    async def _edit_account(self, pool, acc_id, name, balance, phone, saver):
        name = name.strip()
        bal = to_decimal(balance, default=Decimal(-1)) if balance not in (None, "") else Decimal(-1)
        if not name:
            raise LedgerError("الاسم مطلوب")
        if bal < 0:
            raise LedgerError("أدخل رصيد صحيح")
        async with self._lock:
            acc = next((x for x in pool if x.id == acc_id), None)
            if not acc:
                raise LedgerError("الحساب غير موجود")
            bal = q2(bal)
            diff = bal - acc.balance
            acc.name = name
            if phone is not None:
                acc.phone = phone.strip()
            if diff != 0:  # التعديل اليدوي يُسجَّل فرقاً موقَّعاً حتى يبقى الرصيد قابلاً لإعادة الحساب
                acc.history.append(HistoryEntry(date=now_iso(), type="manual_adjustment", amount=diff))
            acc.balance = bal
            await saver()
            return acc

    async def delete_customer(self, customer_id: str) -> None:
        async with self._lock:
            self.customers = [c for c in self.customers if c.id != customer_id]
            self.repo.mark_deleted("customers", customer_id)
            await self._save_customers()

    async def delete_supplier(self, supplier_id: str) -> None:
        async with self._lock:
            self.suppliers = [s for s in self.suppliers if s.id != supplier_id]
            self.repo.mark_deleted("suppliers", supplier_id)
            await self._save_suppliers()

    # ---- الإعدادات وكلمة المرور ------------------------------------------------------------------
    async def update_settings(self, *, business_name: str, tagline: str, phone: str, company_number: str) -> None:
        self.settings.update({
            "businessName": business_name.strip() or "دفتري", "tagline": tagline.strip(),
            "phone": phone.strip(), "companyNumber": company_number.strip(),
        })
        await self.repo.save_settings(self.settings)

    @property
    def has_password(self) -> bool:
        return bool(self.settings.get("appPasswordHash") or self.settings.get("appPassword"))

    async def set_password(self, password: str) -> None:
        """كلمة مرور فارغة = إلغاء القفل. تُحفظ مشفّرة، ويُحذف النص الصريح القديم إن وُجد.
        تنبيه توافق: نسخة الويب القديمة تقرأ appPassword (نص صريح) فلن تقفل بعد هذا التغيير."""
        password = password.strip()
        self.settings.pop("appPassword", None)
        if password:
            self.settings["appPasswordHash"] = hash_password(password)
        else:
            self.settings.pop("appPasswordHash", None)
        await self.repo.save_settings(self.settings)

    def check_password(self, password: str) -> bool:
        if not password:
            return False
        h = self.settings.get("appPasswordHash")
        if h:
            return verify_password(password, h)
        legacy = self.settings.get("appPassword")  # حساب قديم من نسخة الويب: نقبله ثم نرقّيه عند أول دخول
        return bool(legacy) and password == legacy

    async def upgrade_legacy_password(self, password: str) -> None:
        if self.settings.get("appPassword") and not self.settings.get("appPasswordHash"):
            await self.set_password(password)

    # ---- النسخ الاحتياطي ------------------------------------------------------------------------------
    def export_backup(self) -> dict:
        # لا نصدّر كلمة المرور النصية (ملفات النسخ تُرسَل وتُخزَّن في أماكن غير آمنة)؛ المشفّرة تبقى
        settings = {k: v for k, v in self.settings.items() if k != "appPassword"}
        return {
            "inventory": [x.to_dict() for x in self.inventory], "invoices": [x.to_dict() for x in self.invoices],
            "suppliers": [x.to_dict() for x in self.suppliers], "customers": [x.to_dict() for x in self.customers],
            "expenses": [x.to_dict() for x in self.expenses], "purchases": [x.to_dict() for x in self.purchases],
            "vouchers": [x.to_dict() for x in self.vouchers], "settings": settings, "savedAt": now_iso(),
        }

    async def import_backup(self, data: dict) -> dict[str, int]:
        """يستبدل كل البيانات الحالية بالنسخة (نفس سلوك handleImportFile). يرجع عدد السجلات المستوردة."""
        if not isinstance(data, dict) or not any(k in data for k in ("inventory", "invoices", "customers", "settings")):
            raise LedgerError("الملف ليس نسخة احتياطية صحيحة")
        counts: dict[str, int] = {}
        async with self._lock:
            def take(key, cls):
                if isinstance(data.get(key), list):
                    rows = [cls.from_dict(x) for x in data[key] if isinstance(x, dict)]
                    counts[key] = len(rows)
                    return rows
                return None
            for key, cls, attr in (("inventory", InventoryItem, "inventory"), ("invoices", Invoice, "invoices"),
                                   ("suppliers", Supplier, "suppliers"), ("customers", Customer, "customers"),
                                   ("expenses", Expense, "expenses"), ("purchases", Purchase, "purchases"),
                                   ("vouchers", Voucher, "vouchers")):
                rows = take(key, cls)
                if rows is not None:
                    # السجلات التي لم تعد في النسخة تُعلَّم محذوفة حتى لا يعيدها دمج السحابة
                    keep = {r.id for r in rows}  # type: ignore[attr-defined]
                    for old in getattr(self, attr):
                        if old.id not in keep:  # type: ignore[attr-defined]
                            self.repo.mark_deleted(cls.TABLE, old.id)  # type: ignore[attr-defined]
                    setattr(self, attr, rows)
            if isinstance(data.get("settings"), dict):
                self.settings = {**DEFAULT_SETTINGS, **data["settings"]}
            await asyncio.gather(self._save_inventory(), self._save_invoices(), self._save_suppliers(),
                                 self._save_customers(), self._save_expenses(), self._save_purchases(),
                                 self._save_vouchers())
            await self.repo.save_settings(self.settings)
        return counts

    # ---- مزامنة ما حُفظ محلياً فقط -------------------------------------------------------------------
    async def sync_pending(self) -> list[str]:
        """يعيد رفع الجداول التي فشل رفعها (انقطاع إنترنت). يُستدعى دورياً من مهمة الخلفية."""
        done: list[str] = []
        if "settings" in self.repo.dirty:
            self.repo.dirty.discard("settings")
            await self.repo.save_settings(self.settings)
            if "settings" not in self.repo.dirty:
                done.append("settings")
        lists = {"inventory": self.inventory, "invoices": self.invoices, "customers": self.customers,
                 "suppliers": self.suppliers, "expenses": self.expenses, "purchases": self.purchases,
                 "vouchers": self.vouchers}
        async with self._lock:
            done += await self.repo.sync_dirty(lists)
            # بعد المزامنة قد يتغير المحتوى (دمج مع جهاز آخر): حدّث القوائم من المستودع
            for key in done:
                if key in lists:
                    setattr(self, key, await self.repo.load(type(lists[key][0])) if lists[key] else lists[key])
        return done

    async def cancel_edit_purchase(self, d: PurchaseDraft) -> None:
        """تراجع عن تعديل فاتورة شراء: أعد زيادة المخزون وقيد دين المورد كما كانت."""
        async with self._lock:
            p = next((x for x in self.purchases if x.id == d.editing_id), None)
            if not p:
                return
            for ln in p.items:
                it = self._item(ln.id)
                if it:
                    it.stock += ln.qty
            await self._save_inventory()
            if p.remaining > 0 and p.supplier:
                entry = HistoryEntry(date=now_iso(), type="purchase_debt", amount=p.remaining, purchase_number=p.number)
                s = self._find_supplier(p.supplier)
                if s:
                    s.balance += p.remaining
                    s.history.append(entry)
                else:
                    self.suppliers.append(Supplier(name=p.supplier, type="we_owe", balance=p.remaining, history=[entry]))
                await self._save_suppliers()
