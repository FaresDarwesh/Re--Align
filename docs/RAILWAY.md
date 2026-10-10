# النشر على Railway (من GitHub Desktop) — بالتفصيل الممل

ده الطريق الوحيد اللي هنمشي فيه: **GitHub ← Railway ← Neon**.
كل خطوة هنا مجرّبة على الكود ده بالظبط. اقرأ بالترتيب ومتسبش خطوة.

---

## ٠. اللي إنت عامله خلاص (تمام)

- ✅ حساب Railway مربوط بـ GitHub
- ✅ ريبو فاضي على GitHub (مفروض من غير README — لو عملته بـ README،
  امسحها من صفحة الريبو على GitHub الأول: Settings → General → Danger zone
  → Delete file، أو سيبيه وهنقولك نتصرف في خطوة ٢)
- ✅ حساب Neon مربوط بـ GitHub

## ١. نزّل الملف الجديد وفكّه

- نزّل `re-align-source.zip` من الشات.
- فكه في مجلد اسمه `re-align` في أي مكان على جهازك (سطح المكتب مثلاً).
- المجلد ده فيه **كل حاجة**: الكود، الاختبارات، الدوكس، وحتى مجلد `.git`
  بكل التاريخ — عشان GitHub Desktop يلتقطه مباشرة من غير ما تعمل حاجة يدوي.

## ٢. ارفع على GitHub بـ GitHub Desktop

1. افتح **GitHub Desktop** → قائمة **File** → **Add Local Repository…**
   (أو زرار `+` فوق → **Add**).
2. اختار مجلد `re-align` اللي فكّيته → **Add**.
3. هتلاقي الريبو ظهر في القائمة وعلى الشمال كل الـ commits جاهزة،
   وفرع اسمه `main`.
4. روح **Repository** → **Repository Settings…** → تبويب **Remote**:
   - لو خانة الـ Remote فاضية: حط `https://github.com/اسمك/re-align.git` → **Save**.
5. فوق خالص هتلاقي زرار **Push origin** (أو **Publish repository**) → دوسه.
   - لو طلعلك خطأ إن الـ remote فيه commits (يعني عملت الريبو بـ README):
     امسح الـ README من موقع GitHub الأول وبعدين Push تاني.
6. افتح الريبو على موقع GitHub واتأكد إن الملفات كلها طلعت
   (Dockerfile، railway.toml، realign/، …).

> من هنا ورايح: **أي تعديل تعمله وتعمله Commit + Push من GitHub Desktop
> هيتنشر أوتوماتيك على Railway** — دي ميزة الربط اللي عملته.

## ٣. جب connection string بتاع Neon

1. افتح [neon.tech](https://neon.tech) → مشروعك.
2. زرار **Connect** → انسخ الـ **Connection string** كاملة، شكلها:
   ```
   postgres://USER:PASSWORD@ep-xxxxx.eu-central-1.aws.neon.tech/neondb?sslmode=require
   ```
3. سيبها مفتوحة في notes — هنلصقها في Railway بعد شوية.
   (الكود بيحوّل `postgres://` لصيغة asyncpg لوحده، وملحق `sslmode=require`
   بيضيفه لوحده لو مش موجود — يعني الصقها زي ما هي.)

## ٤. جهّز الأسرار (على جهازك، مرة واحدة)

افتح Terminal (أو PowerShell) ونفّذ:

```bash
# مفتاح الجلسات (٣٢ بايت)
openssl rand -hex 32
# مفتاح تشفير مفاتيح الربطات
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# سر الـ cron
openssl rand -hex 24
```

انسخ التلات نواتج في notes جنب الـ connection string.
(معندكش openssl؟ أي أداة تولّد ٦٤ خانة عشوائية تنفع للأول.)

## ٥. اعمل المشروع على Railway

1. [railway.app](https://railway.app) → **New Project** →
   **Deploy from GitHub repo** → اختار `re-align`.
2. Railway هيشوف الـ `Dockerfile` و`railway.toml` ويبدأ بناء — **ماتستناش**
   البناء يخلص، روح على طول للمتغيرات (البناء الأول هيفشل من غيرهم، عادي،
   أول ما تحطهم هيعيد المحاولة):
3. افتح السيرفس → تبويب **Variables** → **Raw Editor** والصق البلوك ده
   بعد ما تملا الفراغات:

```
APP_ENV=production
APP_NAME=re-align
APP_BASE_URL=https://هتتعدل-بعد-شوية
APP_SECRET_KEY=〈ناتج الأمر الأول〉
CREDENTIALS_ENCRYPTION_KEY=〈ناتج الأمر الثاني〉
CRON_SECRET=〈ناتج الأمر التالت〉
DATABASE_URL=〈Connection string بتاع Neon زي ما هو〉
ADMIN_EMAIL=admin@re-align.app
ADMIN_PASSWORD=〈اختع_سر_قوي_١٢+_خانة〉
ADMIN_ORG_NAME=شركتك
TRUST_PROXY=true
FORCE_HTTPS=true
SECURITY_REPORT_EMAIL=security@yourdomain.com
LLM_PROVIDER=mock
```

> ملاحظات مهمة:
> - `ADMIN_EMAIL`/`ADMIN_PASSWORD`: أول تشغيل على قاعدة فاضية، التطبيق
>   بينشئ بيهم حساب المالك **مرة واحدة** — تدخل بيهم وبعدين تغيّر السر
>   من الإعدادات.
> - `LLM_PROVIDER=mock` معناه الوكلاء شغالين بمولّد تجريبي من غير تكلفة.
>   لما يبقى عندك مفتاح OpenAI ضيف `OPENAI_API_KEY` وغيّر القيمة لـ `openai`
>   وهييعيد النشر لوحده.

4. استنى البناء يخلص (أول مرة ~٣-٥ دقايق). لو فشل شوف **Logs** — أغلب
   الوقت هيبقى متغير ناقص، رسايل الـ preflight بتقول اسم المتغير بالنص.

## ٦. طلّع دومين عام

1. في السيرفس → تبويب **Settings** → **Networking** → **Generate Domain**.
2. هتاخد لينك شكله `https://xxxxxxxx.up.railway.app`.
3. ارجع لـ **Variables** وعدّل `APP_BASE_URL` لللينك ده → Railway هيعيد
   النشر لوحده في ثواني.

## ٧. جرّب من أي جهاز

- افتح اللينك → الصفحة السينمائية تطلع.
- **تسجيل دخول** بـ `ADMIN_EMAIL` و`ADMIN_PASSWORD` اللي اخترتهم → لوحة
  التحكم تشتغل وفيها مؤسسة «شركتك» وباقة النمو.
- جرّب تشغّل وكيل، وتعمل حساب تجريبي تاني من «ابدأ مجاناً».
- **غيّر كلمة سر الأدمن** من الإعدادات فوراً.

## ٨. اللي بيحصل أوتوماتيك من هنا ورايح

| بتعمل إيه | بيحصل إيه |
|---|---|
| Commit + Push من GitHub Desktop | Railway يبني وينشر نسخة جديدة لوحده |
| Push فيه تعديل كود | نفس الحاجة + البيانات سليمة (في Neon مش في السيرفر) |
| الـ worker والـ scheduler | شغالين جوّه العملية نفسها — Railway بيديك process دائم، فمفيش حاجة اسمها «التشغيلات بتتقطع» |
| قاعدة البيانات | Neon — بتفضل حتى لو Railway عمل redeploy مليون مرة |

## ٩. مشاكل ممكن تقابلك وحلها

| العَرَض | السبب والحل |
|---|---|
| البناء بيقول preflight failed + اسم متغير | متغير ناقص في Variables — ضيفه وهييعيد تلقائي |
| `TypeError: connect() got an unexpected keyword argument 'sslmode'` | اتصلح في الكود نفسه: `realign/db.py` بيشيل `sslmode` و`channel_binding` من الـ URL وبيحوّلهم لـ `ssl.SSLContext` حقيقي قبل ما يوصلوا لـ asyncpg. سيب الـ DATABASE_URL بتاع Neon زي ما هو (`?sslmode=require`) — الكود بيتصرف. لو لسه شايف الخطأ، معناه إن Railway بيبني نسخة قديمة: اعمل Redeploy بعد الـ Push الجديد |
| `password authentication failed for user 'neondb_owner'` | كلمة السر جوّه DATABASE_URL مش صح — روح فقرة «لو الباسورد فيه رموز خاصة» تحت |
| `connection refused` لقاعدة البيانات | اتأكد إن الـ DATABASE_URL منسوخ **كامل** بـ `?sslmode=require` |
| الصفحة بتلف redirect loop | `TRUST_PROXY=true` ناقصة |
| الـ healthcheck فشل أول مرة | شوف Logs — غالباً أول build بيخلص قبل ما المتغيرات تتحط؛ اعمل **Redeploy** يدوي بعد ما تحطها |
| الملفات المرفوعة (صور البراند) بتختفي بعد نشر جديد | ديسك Railway مؤقت — مقبول في الانطلاق؛ التخزين الدائم (S3/Cloudinary) خطوة لاحقة اختيارية |

## ٩٫٥. لو الباسورد فيه رموز خاصة (مشكلة «password authentication failed»)

أكتر سبب شائع للرسالة دي: كلمة سر Neon فيها رموز زي `#` أو `@` أو `%` أو `+`
أو `/`، والـ string اتقصّ عند الرمز ده فاتغيرت كلمة السر اللي بتوصل لقاعدة
البيانات. الحل بالترتيب:

1. افتح Neon → مشروعك → **Connect**.
2. جنب كلمة السر في دوس **reset/regenerate** — هتطلع كلمة سر جديدة،
   والـ connection string اللي بيتعرض بيكون **مشفّر صح (percent-encoded)**
   وجاهز للنسخ.
3. انسخ الـ **Connection string كامل في سطر واحد** (بيبدأ بـ
   `postgres://neondb_owner:` وبيخلص بـ `?sslmode=require`).
4. في Railway → Variables: **امسح** قيمة `DATABASE_URL` القديمة والصق
   الجديدة كقيمة واحدة (متستخدمش Raw editor لو هتلصق جوّه أسطر تانية).
5. Railway هيعيد النشر — لوLogs لازم تبقى شايف `database_ready` بدل الـ traceback.

لو عايز تتأكد يدوياً إن كلمة سرك متشفرة صح، شغّل ده على جهازك:
```bash
python -c "import urllib.parse; print(urllib.parse.quote('كلمة_السر_هنا', safe=''))"
```
وحط الناتج مكان كلمة السر جوّه الـ URL.

## ١٠. إمتى تضيف الحاجات التجارية (اختياري، بعد الانطلاق)

- **دومين باسمك**: اشتره من أي مسجّل ووجّهه CNAME على دومين Railway من
  Networking → Custom Domain.
- **دفع حقيقي**: `BILLING_PROVIDER=paymob` أو `kashier` + مفاتيحهم (محتاج
  سجل تجاري).
- **إيميلات حقيقية**: متغيرات SMTP (أي مزوّد).
- **ذكاء حقيقي**: `OPENAI_API_KEY` + `LLM_PROVIDER=openai`.

كلهم متغيرات بتتضاف من نفس شاشة Variables — مفيش سطر كود هيتلمس.
