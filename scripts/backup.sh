#!/usr/bin/env bash
# نسخ احتياطي — SQLite (افتراضي) أو PostgreSQL (عند USE_PG=1)
set -euo pipefail

DIR="${BACKUP_DIR:-$(cd "$(dirname "$0")/.." && pwd)/backups}"
mkdir -p "$DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"

if [[ "${USE_PG:-0}" == "1" ]]; then
  OUT="$DIR/realign-pg-$STAMP.sql"
  pg_dump "${DATABASE_URL:?DATABASE_URL مطلوب}" > "$OUT"
  gzip "$OUT"
  echo "✓ $OUT.gz"
else
  DB="${SQLITE_PATH:-$(cd "$(dirname "$0")/.." && pwd)/data/realign.db}"
  [[ -f "$DB" ]] || { echo "✗ لا توجد قاعدة بيانات في $DB"; exit 1; }
  OUT="$DIR/realign-sqlite-$STAMP.db"
  sqlite3 "$DB" ".backup '$OUT'" 2>/dev/null || cp "$DB" "$OUT"
  gzip "$OUT"
  echo "✓ $OUT.gz"
fi

# حذف النسخ الأقدم من ٣٠ يوم
find "$DIR" -name 'realign-*' -type f -mtime +30 -delete 2>/dev/null || true
