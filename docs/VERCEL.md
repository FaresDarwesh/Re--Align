# النشر على Vercel (مع Neon Console) — بالتفصيل الممل

الكود شغال على **Vercel وRailway معاً** من نفس الريبو. الملف ده بيشرح
طريق Vercel بس. قاعدة البيانات: **Neon Console** — نفس الحساب اللي عندك.

---

## ٠. ليه هيشتغل على Vercel دلوقتي؟

- `api/index.py` = نقطة دخول Vercel (بتطلّع الـ ASGI app).
- `vercel.json` = الـ rewrites (كل الطلبات → الـ function) + الـ cron + كاش الستاتيك.
- `runtime.txt` = بايثون 3.12.
- الكود بيكتشف `VERCEL=1` لوحده → بيشتغل في وضع serverless:
  تشغيلات الوكلاء بتتنفذ جوّه الطلب نفسه (مفيش worker بيموت)،
  وأول تشغيل على قاعدة فاضية بينشئ حساب الأدمن من `ADMIN_EMAIL`/`ADMIN_PASSWORD`.
- اتصال Postgres: بيدعم `DATABASE_URL` **أو** الأربع متغيرات
  `PGHOST/PGUSER/PGPASSWORD/PGDATABASE` (الأضمن ضد رموز كلمة السر) —
  وبيحوّل `postgres://` → `postgresql+asyncpg://` وبيضيف `sslmode=require` لوحده.

## ١. جهّز الأسرار (مرة واحدة على جهازك)

```bash
openssl rand -hex 32        ← APP_SECRET_KEY
openssl rand -hex 24        ← CRON_SECRET
```
ومفتاح التشفير (Fernet) بأي بايثون فيه cryptography.

## ٢. Neon Console

1. افتح مشروعك (أو اعمل واحد جديد).
2. من شاشة **Connect** خد الأربع أجزاء:
   - `PGHOST` = الهوست (`ep-….neon.tech`)
   - `PGUSER` = `neondb_owner`
   - `PGPASSWORD` = الجزء بين `:` و`@` (زي ما ظاهر)
   - `PGDATABASE` = `neondb`
3. لو كلمة السر قديمة/مش متأكد: Reset password وخد سترينج جديد.

## ٣. استورد على Vercel

1. [vercel.com/new](https://vercel.com/new) → **Import** الريبو `re-align`.
2. Framework Preset: **Other**. Root Directory: سيبه فاضي.
   (كل الإعدادات جاية من `vercel.json` — متعدلش حاجة يدوي.)
3. **قبل** ما تدوس Deploy: افتح **Environment Variables** والصق في الـ
   Raw/ Bulk editor:

```
APP_ENV=production
APP_NAME=re-align
APP_BASE_URL=https://هيتحط-بعد-أول-نشر
APP_SECRET_KEY=〈…〉
CREDENTIALS_ENCRYPTION_KEY=〈…〉
CRON_SECRET=〈…〉
PGHOST=ep-xxxxx.eu-central-1.aws.neon.tech
PGUSER=neondb_owner
PGPASSWORD=〈كلمة السر زي ما هي〉
PGDATABASE=neondb
ADMIN_EMAIL=admin@re-align.app
ADMIN_PASSWORD=〈سر قوي〉
ADMIN_ORG_NAME=شركتك
TRUST_PROXY=true
FORCE_HTTPS=true
LLM_PROVIDER=mock
```

4. **Deploy**. أول cold boot بيعدّي على الـ preflight (لو متغير ناقص هيقولك
   اسمه بالعربي في اللوج) وبيينشئ الجداول في Neon + حساب الأدمن.

## ٤. الدومين وAPP_BASE_URL

1. بعد أول نشر ناجح: Vercel بيديك `https://اسم-مشروعك.vercel.app`.
2. رجّع اللينك ده في `APP_BASE_URL` (Settings → Environment Variables) →
   Deploy جديد.

## ٥. جرّب

- افتح اللينك → الصفحة السينمائية.
- دخول بـ `ADMIN_EMAIL`/`ADMIN_PASSWORD`.
- شغّل وكيل → التشغيل بيتنفذ جوّه الطلب (ثواني) → النتيجة في شاشة التشغيلات.

## ٦. الـ cron (تنضيف الطوابير)

- خطة **Pro**: الـ cron اللي في `vercel.json` بيشتغل كل ١٠ دقايق أوتوماتيك.
- خطة **Hobby** المجانية: الـ cron بيشتغل مرة/يوم بس — فاستخدم
  [cron-job.org](https://cron-job.org) (مجاني): مهمة كل ١٠ دقايق على
  `https://دومينك/api/v1/cron/tick` بهيدر `X-Cron-Secret: 〈CRON_SECRET〉`.
- ملحوظة: حتى من غير cron، تشغيلات الوكلاء بتتنفذ فوراً جوّه الطلب —
  الـ cron بس بينضّف أي حاجة اتعلّقت وبيصرّف طابور النشر الخارجي (outbox).

## ٧. حدود لازم تعرفها (Vercel Hobby)

| الحد | معناه | تعاملنا معاه |
|---|---|---|
| مدة الـ function ٦ ثانية | تشغيل تقيل جداً ممكن يقطع | `maxDuration=60` مظبوط في vercel.json، والتشغيلات ليها حد محاولات |
| ديسك مؤقت | الملفات المرفوعة بتروح مع كل deploy | مقبول للانطلاق؛ تخزين دائم (S3/Cloudinary) خطوة لاحقة |
| SQLite ممنوع | لازم Postgres | PG* بتاعتك من Neon — مظبوط |
| cold start أول طلب | أول فتحة بعد سكون بتاخد ثانية-تنين | طبيعي في serverless |

## ٨. مشاكل شائعة وحلها

| العَرَض | الحل |
|---|---|
| `preflight failed` + اسم متغير | المتغير ناقص في Environment Variables — ضيفه وRedeploy |
| `password authentication failed` | استخدم PG* الأربع بدل DATABASE_URL (فقرة ٢) |
| الصفحة الرئيسية 404 | اتأكد إن الـ rewrite موجود (جاي من vercel.json — متنسخش الملف ده في الريبو) |
| اللوج فاضي والصفحة بيضا | شوف **Functions → api/index → Logs** مش صفحة الـ build |

## ٩. والـ Railway؟

لسه شغال بنفس الريبو — `railway.toml` و`Dockerfile` موجودين. اختار منصة
واحدة تستضيف الإنتاج عشان متبقاش عندك قاعدتين بيانات متفرقين؛ التاني
يفضل احتياطي تجريبي لو حبيت.
