<div dir="rtl">

# النشر والتشغيل — من اللابتوب لسيرفر حقيقي

## ١. المتطلبات

- Python 3.11+ (مُختبَر على 3.13) · PostgreSQL 16+ (للإنتاج) · 512 MB RAM كحد أدنى
- SQLite **للتطوير فقط**. الإنتاج لازم Postgres (الكود بيرفض يشتغل بـ SQLite في production).

## ٢. تشغيل محلي (أقل من دقيقة)

```bash
cd realign
cp .env.example .env
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/seed.py      # ينشئ القاعدة + يعبّي بيانات
# أو
make run                              # http://localhost:8000
```

حساب المدير: `admin@re-align.app` / `Re-Align#Admin2026` · حساب تجريبي: `demo@re-align.app` / `Re-Align#Demo2026`

## ٣. أول حاجة قبل أي نشر: ولّد المفاتيح

```bash
.venv/bin/python scripts/generate_keys.py
```

الناتج سطرين — الصقهم في `.env`:

```
APP_SECRET_KEY="..."
CREDENTIALS_ENCRYPTION_KEY="..."
```

> ⚠️ **تحذير قاطع:** لو غيّرت `CREDENTIALS_ENCRYPTION_KEY` بعد ما تخزّن مفاتيح ربطات،
> المفاتيح القديمة **مش هتتقرا تاني**. ولّده مرة واحدة واحتفظ بنسخة منه في مكان آمن.

## ٤. النشر بـ Docker (الأسهل)

```bash
# ١. ملف الإعدادات
cp .env.example .env
# ٢. عدّل: APP_SECRET_KEY · CREDENTIALS_ENCRYPTION_KEY · POSTGRES_PASSWORD · APP_BASE_URL

# ٣. شغّل
docker compose up -d --build

# ٤. تحقّق
curl http://localhost:8000/api/v1/health/ready

# ٥. أول مدير
docker compose exec api python scripts/create_admin.py
```

الـ compose بيشغّل: **Postgres 17** + **API** (مع `alembic upgrade head` تلقائي قبل الإقلاع)
+ فحص صحة مدمج + إعادة تشغيل تلقائية.

## ٥. النشر على سيرفر عادي (Hetzner / DigitalOcean / أي VPS)

```bash
# ١. جهّز السيرفر
apt update && apt install -y python3.13-venv nginx postgresql certbot python3-certbot-nginx

# ٢. خُد الكود
git clone <مستودعك> /opt/realign && cd /opt/realign
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# ٣. الإعدادات
cp .env.example .env
.venv/bin/python scripts/generate_keys.py >> .env
# عدّل APP_ENV=production و DATABASE_URL و APP_BASE_URL

# ٤. القاعدة
sudo -u postgres createdb realign
.venv/bin/alembic upgrade head
.venv/bin/python scripts/create_admin.py

# ٥. خدمة systemd
sudo cp deploy/realign.service /etc/systemd/system/
sudo systemctl enable --now realign

# ٦. Nginx + شهادة مجانية
sudo cp deploy/nginx.conf /etc/nginx/sites-available/realign
sudo ln -s /etc/nginx/sites-available/realign /etc/nginx/sites-enabled/
sudo certbot --nginx -d yourdomain.com
sudo systemctl reload nginx
```

## ٦. خيارات الاستضافة المناسبة لمصر (السعر التقريبي شهرياً)

| الخيار | التكلفة | الملاحظة |
|---|---|---|
| **Hetzner CX22** (ألمانيا) | ~€4–6 | أرخص قيمة مقابل أداء، ٢ vCPU / ٤ GB |
| **DigitalOcean Droplet** | $6–12 | أسهل واجهة، فيه مركز بيانات أوروبا |
| **Railway / Render** | $5–20 | أسهل نشر، بيكبر تلقائياً، أغلى شوية |
| **VPS مصري** | متغيّر | أقل زمن وصول محلي، بس تأكد من استقرار الدعم |
| **Fly.io** | $5–15 | ممتاز لو عايز مناطق متعددة |

> إحنا مش بنقدر ندفع أو نفتح حسابات استضافة — دي حاجة من **الحاجات المؤجّلة**،
> لكن كل حاجة محضّرة: Dockerfile · docker-compose · systemd · Nginx.

## ٧. فحص ما قبل الإقلاع (مبني جوه الكود)

الإنتاج **بيرفض يشتغل** في الحالات دي:

- `APP_SECRET_KEY` سابه على القيمة الافتراضية
- `CREDENTIALS_ENCRYPTION_KEY` فاضي
- `DATABASE_URL` على SQLite و `APP_ENV=production`

الرسالة بتطلع في اللوج وتوضّح بالظبط إيه الناقص.

## ٨. النسخ الاحتياطي

```bash
./scripts/backup.sh                       # SQLite (تلقائي)
USE_PG=1 DATABASE_URL=... ./scripts/backup.sh   # PostgreSQL
BACKUP_DIR=/mnt/backups ./scripts/backup.sh
```

بيحذف النسخ الأقدم من ٣٠ يوم تلقائياً.
**نصيحة:** شغّله كل يوم في `cron` على السيرفر + انسخ المجلد على مكان تاني (S3 / Backblaze).

## ٩. المراقبة

- `GET /api/v1/health` — فحص خفيف (لـ load balancer)
- `GET /api/v1/health/ready` — فحص جوهري: DB + LLM + عدد الوكلاء + عدد الموصلات
- اللوج منظّم (structlog) وبيدعم JSON في الإنتاج: `LOG_JSON=true`
- كل تشغيل وكيل مسجّل: التكلفة · النقاط · المدة · حالة كل خطوة

## ١٠. التوسّع (Scaling)

| المرحلة | الإعداد |
|---|---|
| ٠ – ١٠٠ شركة | سيرفر واحد، `WORKERS=2` |
| ١٠٠ – ١٠٠٠ | Postgres منفصل، `WORKERS=4`، Redis للتخزين المؤقت |
| ١٠٠٠+ | عاملين منفصلين (worker/scheduler في حاويات لوحدهم) + طابور حقيقي |

الحاجة الوحيدة اللي هتحتاج تتعدّل في المرحلة التالتة: استبدال طابور الـ Outbox
الحالي (المبني على قاعدة البيانات) بـ Celery/Redis — وده تغيير محصور في
`app/workers/queue.py` بس.

## ١١. استكشاف الأخطاء

| الدخول يرجّع `/login?error=csrf` | الكوكيز محجوبة (iframe عابر للنطاق أو إعداد المتصفح) | المنصة تكتشف السياق وتحوّل تلقائياً إلى `SameSite=None; Secure` — تأكد إن الموقع على HTTPS |
| الدخول يرجّع `error=locked` | تحديد المعدّل اشتغل | استنى ١٥ دقيقة |
| التشغيل `skipped` | kill switch أو الرصيد أو الحد اليومي | راجع `/app/settings` و`/app/billing` |
| العرض | السبب | الحل |
|---|---|---|
| 500 على كل الصفحات | `TemplateResponse` بالشكل القديم | استخدم `request=request, name=..., context=...` |
| `EmailStr` رافض الإيميل | نطاق محجوز زي `.local` | استخدم نطاق حقيقي زي `.com` |
| المنشور مش بينتشر | الموصل مش مظبوط | `/app/integrations` → اختبر الربط |
| `greenlet` مش موجود | ناقص مع SQLAlchemy async | `pip install greenlet` |
| كلمة سر طويلة مرفوضة | حد bcrypt ٧٢ بايت | محلول بـ pre-hash جوه الكود |
| التشغيل `skipped` | kill switch أو الرصيد أو الحد اليومي | راجع `/app/settings` و`/app/billing` |

</div>
