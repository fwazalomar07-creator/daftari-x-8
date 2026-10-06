-- نفس الجدول المستخدم حالياً في نسخة الويب (لا حاجة لتغييره إن كان موجوداً)
create table if not exists app_data (
  key        text primary key,
  value      jsonb not null,
  updated_at timestamptz not null default now()
);

-- مهم: فعّل Row Level Security. بدونه أي شخص يملك المفتاح العام (وهو ظاهر في ملف HTML القديم) يقرأ كل حساباتك ويعدّلها.
alter table app_data enable row level security;

-- مثال: السماح لمستخدم واحد مسجّل الدخول فقط (ضع UUID حسابك بدل القيمة).
-- create policy "owner only" on app_data for all to authenticated
--   using (auth.uid() = '00000000-0000-0000-0000-000000000000')
--   with check (auth.uid() = '00000000-0000-0000-0000-000000000000');

-- ملاحظة: المفتاح العام (anon) لا يقرأ شيئاً بعد تفعيل RLS ما لم تضف policy، فتظهر الأصناف فارغة.
-- الخيار الأفضل لبرنامج سطح المكتب: ضع مفتاح service_role في .env على جهازك فقط (يتجاوز RLS ولا يُكشف للعامة).
-- أو أضف policy لمستخدم مسجّل الدخول كما في المثال أعلاه.
