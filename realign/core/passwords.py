"""Password policy and breach-blocklist (credential stuffing / spraying).

Follows NIST SP 800-63B:
  * length is the primary control (10 characters minimum here)
  * a blocklist of common/breached passwords beats forced composition rules
  * no silent truncation, no maximum below 256, no hinting in error messages
  * a *score* is exposed so the UI can show strength without leaking rules
"""

from __future__ import annotations

import re
import unicodedata

from realign.config import settings

# Top breached / keyboard-walk / Arabic-market passwords.
# Kept small and dependency-free; extend freely — matching is cheap.
COMMON_PASSWORDS: frozenset[str] = frozenset(
    """
    123456 1234567 12345678 123456789 1234567890 000000 111111 11111111 121212 123123
    123456a 123456789a a123456 a12345678 aa12345678 qwerty qwertyuiop qwerty123
    qwerty1 qazwsx asdfgh asdfghjkl zxcvbn zxcvbnm password password1 password12
    password123 password1234 password12345 passw0rd passwd pwd123 p@ssw0rd p@ssword
    pass123 admin admin123 admin1234 administrator root root123 toor letmein
    welcome welcome1 welcome123 iloveyou iloveyou123 abc123 abc123456 1q2w3e4r
    1q2w3e4r5t 1qaz2wsx qazwsx123 master monkey dragon sunshine princess football
    baseball soccer hockey basketball shadow michael jennifer jordan harley
    ranger hunter buster thomas summer winter autumn spring december january
    secret secrets 00000000 0123456789 0123456 123321 654321 987654321 112233
    egypt egypt123 egypt1 cairo cairo123 egypt2024 egypt2025 ahmed ahmed123
    mohamed mohamed123 mahmoud mahmoud123 mustafa mostafa ali ali123 omar omar123
    islam islam123 islam786 mohammad muhammad ahmed2024 1234567891 saudi ksa
    test test123 test1234 demo demo123 demo1234 demo12345 user user123 guest
    guest123 default changeme changeme123 newpassword newpass123 pass pass1234
    trustno1 whatever superman batman eva123 102030 135790 147258 258456
    asdasd asdasd123 qqqqqq qqq111 www123 zzzzzz q1w2e3r4 q1w2e3r4t5
    """.split()
)

_SEQUENCES = ("0123456789", "abcdefghijklmnopqrstuvwxyz", "qwertyuiop", "asdfghjkl", "zxcvbnm")
_KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890")


def normalise(password: str) -> str:
    return unicodedata.normalize("NFKC", password or "")


def _is_sequence(value: str) -> bool:
    lowered = value.lower()
    for seq in _SEQUENCES:
        for size in (4, 5, 6):
            for i in range(len(seq) - size + 1):
                chunk = seq[i : i + size]
                if chunk in lowered or chunk[::-1] in lowered:
                    return True
    return False


def _is_repetition(value: str) -> bool:
    return bool(value) and len(set(value)) <= 2 and len(value) >= 6


def password_problems(password: str) -> list[str]:
    """Return a list of Arabic reasons the password is unacceptable."""
    pwd = normalise(password)
    problems: list[str] = []
    minimum = settings.password_min_length

    if len(pwd) < minimum:
        problems.append(f"كلمة المرور يجب ألا تقل عن {minimum} أحرف")
    if len(pwd) > settings.password_max_length:
        problems.append(f"كلمة المرور يجب ألا تزيد عن {settings.password_max_length} حرفاً")
    if not pwd:
        return problems or ["كلمة المرور مطلوبة"]
    if settings.password_require_digit and not re.search(r"\d", pwd):
        problems.append("كلمة المرور يجب أن تحتوي على رقم واحد على الأقل")
    if settings.password_require_upper and not re.search(r"[A-Z]", pwd):
        problems.append("كلمة المرور يجب أن تحتوي على حرف كبير")
    if settings.password_require_lower and not re.search(r"[a-z]", pwd):
        problems.append("كلمة المرور يجب أن تحتوي على حرف صغير")
    if settings.password_require_symbol and not re.search(r"[^A-Za-z0-9]", pwd):
        problems.append("كلمة المرور يجب أن تحتوي على رمز واحد على الأقل")

    if settings.password_block_common:
        stripped = re.sub(r"[^a-z0-9]", "", pwd.lower())
        if pwd.lower() in COMMON_PASSWORDS or stripped in COMMON_PASSWORDS:
            problems.append("كلمة المرور من أشهر كلمات المرور المسروقة — اختر غيرها")
        elif _is_sequence(pwd) and len(pwd) < 14:
            problems.append("كلمة المرور عبارة عن تسلسل متوقع")
        elif _is_repetition(pwd):
            problems.append("كلمة المرور حروف متكررة")

    return problems


def validate_password(password: str) -> tuple[bool, str]:
    problems = password_problems(password)
    if problems:
        return False, problems[0]
    return True, ""


def password_score(password: str) -> int:
    """0..5 — for the strength meter only; never used to reject a password."""
    pwd = normalise(password)
    if not pwd:
        return 0
    score = 0
    length = len(pwd)
    if length >= 10:
        score += 1
    if length >= 14:
        score += 1
    if length >= 20:
        score += 1
    classes = sum(
        bool(re.search(pattern, pwd))
        for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]", r"[\u0600-\u06FF]")
    )
    if classes >= 2:
        score += 1
    if classes >= 3 and length >= 12 and pwd.lower() not in COMMON_PASSWORDS:
        score += 1
    return max(0, min(5, score))


def password_label(score: int) -> str:
    return {0: "ضعيفة جداً", 1: "ضعيفة", 2: "مقبولة", 3: "جيدة", 4: "قوية", 5: "قوية جداً"}.get(score, "مقبولة")


__all__ = ["validate_password", "password_problems", "password_score", "password_label", "COMMON_PASSWORDS"]
