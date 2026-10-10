#!/usr/bin/env bash
# تشغيل re-align — بينشئ البيئة لو مش موجودة، ويهاجر قاعدة البيانات، ويشغّل السيرفر
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# ١) البيئة الافتراضية (مش محفوظة في النسخة الاحتياطية، فبنعيد إنشاءها تلقائياً)
if [ ! -x "$ROOT/.venv/bin/python" ] && [ ! -x "$HOME/.venv/bin/python" ]; then
  echo "▸ بإنشاء البيئة وتثبيت الحزم (أول مرة بس)..."
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q --upgrade pip
  "$ROOT/.venv/bin/pip" install -q -r requirements.txt
fi

if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"
elif [ -x "$HOME/.venv/bin/python" ]; then PY="$HOME/.venv/bin/python"
else PY="python3"; fi

# ٢) ترقية قاعدة البيانات
"$PY" -m alembic upgrade head 2>/dev/null || true

# ٣) بيانات تجريبية لو قاعدة البيانات فاضية
if [ ! -f "$ROOT/data/realign.db" ]; then
  echo "▸ بإنشاء بيانات تجريبية..."
  "$PY" scripts/seed.py >/dev/null 2>&1 || true
fi

PORT="${PORT:-8000}"
echo "▸ re-align على http://0.0.0.0:$PORT"
exec "$PY" -m uvicorn realign.main:app --host 0.0.0.0 --port "$PORT" ${RELOAD:+--reload --reload-dir app}
