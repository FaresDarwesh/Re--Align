#!/usr/bin/env bash
# تقوية صلاحيات الملفات — شغّله بعد كل نشر
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
echo "تقوية الصلاحيات في $ROOT"

# 1) ملف الأسرار: للـ owner فقط
if [ -f .env ]; then
  chmod 600 .env
  echo "  + .env -> 600"
fi

# 2) قاعدة البيانات والنسخ الاحتياطية
if [ -d data ]; then
  chmod 700 data
  find data -type f -exec chmod 600 {} \; 2>/dev/null
  echo "  + data/ -> 700 (والملفات 600)"
fi
if [ -d backups ]; then
  chmod 700 backups
  echo "  + backups/ -> 700"
fi

# 3) الكود: لا كتابة للمجموعة أو للجميع
find app scripts db_migrations tests -type f -name '*.py' -exec chmod 640 {} \; 2>/dev/null
find app scripts db_migrations tests -type d -exec chmod 750 {} \; 2>/dev/null
echo "  + كود التطبيق -> 640/750"

# 4) السكربتات التنفيذية
for f in scripts/backup.sh scripts/run_prod.sh scripts/harden.sh scripts/security_scan.sh; do
  [ -f "$f" ] && chmod 750 "$f"
done

# 5) لا ملفات قابلة للكتابة من الجميع
BAD=$(find . -maxdepth 3 -perm -o+w -not -path './.venv/*' -not -path './.git/*' 2>/dev/null)
if [ -n "$BAD" ]; then
  echo "  ! ملفات قابلة للكتابة من الجميع:"
  echo "$BAD" | sed 's/^/     /'
else
  echo "  + لا ملفات قابلة للكتابة من الجميع"
fi

# 6) تأكد إن .env مش متتبّع في git
TRACKED=0
if [ -d .git ]; then
  if git ls-files --error-unmatch .env >/dev/null 2>&1; then
    TRACKED=1
  fi
fi
if [ "$TRACKED" = "1" ]; then
  echo "  خطير: .env متتبع في git - احذفه فورا: git rm --cached .env"
  exit 1
else
  echo "  + .env غير متتبع في git"
fi

echo "تم"
