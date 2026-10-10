#!/usr/bin/env bash
# فحص أمني شامل — شغّله قبل كل إصدار
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
# ابحث عن البيئة الافتراضية في المسار الشائع أو داخل المشروع
if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"
elif [ -x "$HOME/.venv/bin/python" ]; then PY="$HOME/.venv/bin/python"
else PY="python3"; fi
FAIL=0
say(){ printf "%s\n" "$1"; }

say "══════════ فحص أمني — $(date '+%Y-%m-%d %H:%M') ══════════"

say ""
say "١) ثغرات الحزم (pip-audit)"
AUDIT_BIN=""
for candidate in "$ROOT/.venv/bin/pip-audit" "$HOME/.venv/bin/pip-audit" "$(command -v pip-audit 2>/dev/null)"; do
  [ -n "$candidate" ] && [ -x "$candidate" ] && AUDIT_BIN="$candidate" && break
done
if [ -n "$AUDIT_BIN" ]; then
  AUDIT="$("$AUDIT_BIN" --requirement requirements.txt 2>&1 || true)"
  if echo "$AUDIT" | grep -qE 'PYSEC-[0-9]|CVE-[0-9]{4}|GHSA-[a-z0-9]{4}-'; then
    say "  ❌ وُجدت ثغرات:"; echo "$AUDIT" | sed 's/^/     /'; FAIL=1
  else
    say "  ✓ لا ثغرات معروفة في الحزم المثبّتة"
  fi
else
  say "  ⚠ pip-audit غير مثبّت — ثبّته بـ: pip install pip-audit"
fi

say ""
say "٢) أنماط خطيرة في الكود"
HITS="$(grep -rnE 'subprocess|os\.system|os\.popen|shell=True|\beval\(|\bexec\(|pickle\.loads|yaml\.load' app/ --include='*.py' 2>/dev/null | grep -v 'core/sanitize.py' || true)"
if [ -n "$HITS" ]; then say "  ❌"; echo "$HITS" | sed 's/^/     /'; FAIL=1; else say "  ✓ لا يوجد تنفيذ أوامر أو تحميل غير آمن"; fi

say ""
say "٣) أسرار مكتوبة داخل الكود"
SECRETS="$(grep -rnE '(sk-[A-Za-z0-9]{16,}|nxk_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)' app/ scripts/ 2>/dev/null | grep -v 'core/redact.py' | grep -v 'core/security.py' | grep -v 'core/sanitize.py' || true)"
if [ -n "$SECRETS" ]; then say "  ❌"; echo "$SECRETS" | sed 's/^/     /'; FAIL=1; else say "  ✓ لا مفاتيح مكتوبة داخل الكود"; fi

say ""
say "٤) SQL يُبنى يدوياً"
SQL="$(grep -rnE 'execute\(f"|execute\(.*%|\.format\(.*(SELECT|INSERT|UPDATE|DELETE)' app/ --include='*.py' 2>/dev/null || true)"
if [ -n "$SQL" ]; then say "  ❌"; echo "$SQL" | sed 's/^/     /'; FAIL=1; else say "  ✓ كل الاستعلامات مُعاملَة (parameterised)"; fi

say ""
say "٥) صلاحيات الملفات"
[ -f .env ] && { P=$(stat -c '%a' .env 2>/dev/null || echo 0); [ "$P" = "600" ] && say "  ✓ .env = 600" || { say "  ❌ .env = $P (يجب 600)"; FAIL=1; }; }
[ -d .git ] && git ls-files --error-unmatch .env >/dev/null 2>&1 && { say "  ❌ .env متتبّع في git"; FAIL=1; } || say "  ✓ .env خارج git"

say ""
say "٦) الاختبارات الأمنية"
if "$PY" -m pytest tests/test_security.py -q --no-header >/tmp/realign_sec_test.log 2>&1; then
  tail -1 /tmp/realign_sec_test.log | sed 's/^/  /'
else
  say "  ❌ بعض الاختبارات الأمنية فشلت"; tail -20 /tmp/realign_sec_test.log | sed 's/^/     /'; FAIL=1
fi

say ""
if [ "$FAIL" = "0" ]; then say "✅ الفحص الأمني ناجح بالكامل"; else say "❌ يوجد ما يجب إصلاحه"; fi
exit $FAIL
