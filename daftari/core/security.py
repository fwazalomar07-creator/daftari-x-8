"""كلمة مرور القفل: PBKDF2-SHA256 بدل النص الصريح.

النسخة الأصلية كانت تحفظ appPassword نصاً صريحاً داخل settings على Supabase، فأي شخص
يقرأ الجدول يرى كلمة المرور. هنا تُخزَّن مشفّرة بملح عشوائي، وتُقارَن بزمن ثابت.
(تنبيه: هذا قفل واجهة لحماية الجهاز من الفضول، وليس حماية للبيانات في السحابة — هذه مهمة RLS.)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_ITER = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITER)
    return "pbkdf2$%d$%s$%s" % (_ITER, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_b64, dk_b64 = stored.split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False
