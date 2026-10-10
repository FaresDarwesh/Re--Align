#!/usr/bin/env bash
# تشغيل الإنتاج (خلف gunicorn أو uvicorn متعدد العمال)
set -euo pipefail
cd "$(dirname "$0")/.."

: "${APP_SECRET_KEY:?مطلوب}"
: "${CREDENTIALS_ENCRYPTION_KEY:?مطلوب}"

alembic upgrade head
exec uvicorn realign.main:app \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --workers "${WORKERS:-2}" \
  --proxy-headers --forwarded-allow-ips='*' \
  --access-log
