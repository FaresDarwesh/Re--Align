<div dir="rtl">

# معمارية RE-ALIGN بالتفصيل الممل

## ١. الصورة الكبيرة

```
              ┌─────────────────────────┐
المستخدم ──▶  │  واجهة ويب (Jinja2 RTL) │  /app/...
              └───────────┬─────────────┘
                          │
العميل/الأتمتة ──▶  ┌─────▼─────────────┐
                    │   API v1  (REST)  │  /api/v1/...
                    └─────┬─────────────┘
                          │
        ┌─────────────────▼──────────────────┐
        │   طبقة الخدمات (services)          │
        │   auth · approvals · billing       │
        │   onboarding · audit               │
        └─────┬──────────────────┬───────────┘
              │                  │
     ┌────────▼────────┐  ┌──────▼───────┐
     │  محرك الوكلاء    │  │  طابور Outbox│  worker + scheduler
     │  (agents/engine) │  │  (workers)   │
     └────────┬────────┘  └──────┬───────┘
              │                  │
     ┌────────▼────────┐  ┌──────▼───────┐
     │  موجّه LLM       │  │  الموصلات    │  12 connectors
     │  (llm/router)   │  │  (connectors)│
     └────────┬────────┘  └──────┬───────┘
              │                  │
     ┌────────▼──────────────────▼─────────┐
     │   SQLAlchemy 2.1 (async) + Alembic  │
     │   SQLite (تطوير) / PostgreSQL (إنتاج)│
     └─────────────────────────────────────┘
```

## ٢. مسار التشغيل الواحد (Run lifecycle)

```
enqueue_run()      ينشئ Run(status=pending) ويحجزه
      │
      ▼
execute_run()      ← بيشتغل في الخلفية (worker) أو متزامن (sync)
      │
      ├─ ١. فحص البوابات:  kill_switch عام + kill_switch المؤسسة + الرصيد
      │                     + الحد اليومي + ميزانية التشغيل + حالة الاشتراك
      │                     ↳ لو أي بوابة قفلت → status=skipped (بلا أي إنفاق)
      │
      ├─ ٢. تحميل السياق:  ملف البراند + الوكلاء المثبّتين + إعدادات المؤسسة
      │
      ├─ ٣. تنفيذ الوكيل:  agent.execute(ctx)
      │                     ctx.think()   → موديل التفكير (كبير)
      │                     ctx.decide()  → موديل القرار (صغير، JSON مكتوب)
      │                     ctx.step(...) → تسجيل كل خطوة في Run.metrics
      │
      ├─ ٤. تسجيل التكلفة: persist_usage() → UsageRecord + خصم الرصيد
      │
      ├─ ٥. النتيجة:       succeeded  → خلص
      │                     waiting_approval → أنشأ Approval + أوقف التشغيل
      │                     failed     → error محفوظ + السبب
      ▼
   تدقيق (AuditLog) دايماً
```

## ٣. بوابة الموافقة — أهم قرار معماري في المشروع

**القاعدة:** مفيش وكيل ينشر. النشر بيحصل في مكان واحد بس:

```
POST /api/v1/approvals/{approval_id}/decide   {"decision": "approve"}
        │
        ▼
 services/approvals.py :: decide()
        ├─ يتحقق إن الموافقة pending ومؤسستك
        ├─ يحدّث حالة الموافقة + يسجّل مين وإمتى
        ├─ لو approve:
        │     ├─ يحدّث القطعة → approved
        │     └─ يضيف حدث publish_asset في جدول OutboxEvent
        └─ لو reject: القطعة → rejected (مفيش نشر)
                    │
                    ▼
              workers/queue.py  (كل ثانية)
                    ├─ يستلم الحدث (claim بالقفل)
                    ├─ يجيب الموصل المناسب ويحاول ينشر
                    ├─ نجح  → القطعة published + external_id + external_url
                    ├─ فشل مؤقت → إعادة محاولة بتأخير تصاعدي (1m, 2m, 4m…)
                    └─ استنفد المحاولات → status=DEAD + السبب محفوظ

              لو الموصل مش مظبوط → القطعة failed
              ورسالة واضحة: «الربط 'إنستجرام' غير مُعد — تم حفظ المنشور كمسوّدة»
```

الاختبارات بتحمي المسار ده:
`test_content_requires_human_approval` · `test_approve_then_publish` ·
`test_unconfigured_connector_fails_safely`

## ٤. عزل المستأجرين (Tenancy)

- كل جدول أعمال فيه عمود `org_id` بمفتاح أجنبي حقيقي لـ `organizations.id`.
- كل دوال الوصول للبيانات بتاخد `org_id` إجبارياً، مش اختياري.
- محاولة قراءة بيانات مؤسسة تانية → **404** (مش 403) عشان ما تتسرّبش معلومة
  إن المعرّف ده موجود أساساً.
- مفاتيح الربطات ومفاتيح LLM لكل مؤسسة **متشفّرة** ومعزولة.
- اختبار: `test_tenants_are_isolated`

## ٥. المحاسبة (المال)

| المفهوم | التمثيل | ليه |
|---|---|---|
| تكلفة LLM | **عدد صحيح** (ميكرو-دولار) | مفيش أخطاء فاصلة عائمة |
| الرصيد | **نقاط** (credits) عدد صحيح | سهل يفهمه العميل |
| التحويل | `micros_to_credits()` + `CREDIT_USD_VALUE_CENTS` | إعداد واحد يتحكم في الكل |
| حدود | شهري + لكل تشغيل + عدد تشغيلات يومي | ٣ طبقات حماية |
| الفوترة | `billable` flag + فواتير + ويبهوك HMAC | جاهز لأي بوابة دفع |

مثال من التشغيل التجريبي (بالموديل الوهمي):
`research $0.0012 · content $0.0015 · creative $0.0006 · sales $0.0008 · support $0.0002`
**الإجمالي ل٧ وكلاء ≈ أقل من سنت أمريكي واحد.**

## ٦. طابور الـ Outbox

ليه outbox مش `BackgroundTasks`؟ لأن `BackgroundTasks` بيموت مع العملية.
أي حاجة لازم تحصل **ولا تضيع** لازم تتسجّل في قاعدة البيانات الأول:

```
OutboxEvent(id, type, payload, status, attempts, run_after, last_error)
   status: pending → processing → done
                                → dead  (بعد WORKER_MAX_ATTEMPTS)
```

ستة معالِجات: `run_agent` · `publish_asset` · `send_notification` ·
`deliver_webhook` · `sync_integration` · `handoff`
(+ عامل تنظيف janitor كل ساعة).

## ٧. موجّه LLM (السر الاقتصادي)

```
LLM_THINKING_PROVIDER   openai | anthropic | ollama | custom | mock
LLM_DECISION_PROVIDER   openai | anthropic | ollama | custom | mock
```

- `think()`  → كتابة/تحليل/تلخيص (موديل كبير، تكلفة أعلى)
- `decision()` → قرار مكتوب JSON (موديل صغير، تكلفة شبه صفر)
- كل مزوّد: `openai_compat` (OpenAI + أي سيرفر متوافق زي Ollama/vLLM/LM Studio)،
  `anthropic`، و`mock` للعمل بدون إنترنت.
- تسعير كل موديل في `llm/pricing.py` — لو أضفت موديل جديد تحطّ سعره هناك.
- إعادة محاولة + مهلة زمنية + حد أقصى للـ tokens.
- **BYOK:** كل مؤسسة تقدر تحطّ مفتاحها الخاص (متشفّر) ويكسّر الإعداد العام.

## ٨. الجداول

| المجموعة | الجداول |
|---|---|
| **الهوية** | `organizations` · `users` · `api_keys` · `invitations` |
| **الفوترة** | `plans` · `subscriptions` · `invoices` · `usage_records` |
| **العمليات** | `brand_profiles` · `agents` · `runs` · `approvals` · `content_assets` · `schedules` · `outbox_events` |
| **النظام** | `integrations` · `audit_logs` |

## ٩. نقاط النهاية (Endpoints) — ٥٩ API

**الصحة:** `GET /health` · `GET /health/ready`
**الدخول:** `POST /login` · `POST /register` · `POST /refresh` · `GET /me` · `PATCH /me` · `POST /change-password`
**الوكلاء:** `GET /agents` · `GET /agents/installed` · `POST /agents/{key}/run` · `PATCH /agents/{key}`
**التشغيلات:** `GET /runs` · `GET /runs/{id}` · `POST /runs/{id}/cancel`
**الموافقات:** `GET /approvals` · `POST /approvals/{id}/decide` · `POST /approvals/bulk`
**المحتوى:** `GET /assets` · `GET /assets/{id}` · `POST /assets/{id}/requeue`
**البراند:** `GET /brand` · `PUT /brand`
**الربطات:** `GET /integrations` · `PUT /integrations/{key}` · `DELETE /integrations/{key}` ·
`POST /integrations/{key}/test` · `POST /integrations/{key}/validate` · `POST /integrations/{key}/sync`
**الفوترة:** `GET /plans` · `GET /billing` · `POST /billing/subscribe` · `POST /billing/plan` · `GET /billing/invoices/{id}`
**المؤسسة:** `GET|PATCH /org` · `PUT /org/llm-key` · `GET /overview` ·
`GET|POST /auth/team` · `PATCH|DELETE /auth/team/{id}` ·
`GET|POST /auth/api-keys` · `DELETE /auth/api-keys/{id}`
**الجدولة:** `GET|POST /schedules` · `DELETE /schedules/{id}`
**الإدارة:** `GET /meta` · `GET /admin/config` · `GET /admin/stats` ·
`GET /admin/organizations` · `PATCH /admin/organizations/{id}/status` ·
`POST /admin/organizations/{id}/add-credits` · `POST /kill-switch` · `GET /audit`
**الويبهوكات:** `POST /webhooks/billing/{provider}` · `POST /webhooks/inbox/telegram` ·
`POST /webhooks/inbox/generic/{org_slug}`

**صفحات الويب (٢٦ مسار، ١٦ شاشة):**
`/` · `/login` · `/register` · `/logout` · `/app` · `/app/admin` · `/app/agents` ·
`/app/approvals` · `/app/assets` · `/app/assets/{id}` · `/app/billing` · `/app/brand` ·
`/app/integrations` · `/app/runs` · `/app/runs/{id}` · `/app/settings`

## ١٠. الاختبارات — ١٧ اختبار

| الاختبار | بيحمي إيه |
|---|---|
| `test_health` / `test_readiness` | النظام صاحي والـ DB شغالة و١١ وكيل و١٢ موصّل مسجّلين |
| `test_unauthenticated_is_rejected` | مفيش دخول من غير توكن |
| `test_register_login_me` | التسجيل → الدخول → الملف → كلمة سر غلط = 401 |
| `test_tenants_are_isolated` | **عزل المستأجرين** |
| `test_every_agent_runs` | **كل الـ ١١ وكيل** بيشتغلوا من غير استثناء |
| `test_content_requires_human_approval` | **بوابة الموافقة** |
| `test_approve_then_publish` | الموافقة → النشر → `published` + `external_id` |
| `test_unconfigured_connector_fails_safely` | موصّل ناقص = فشل صريح مش نشر وهمي |
| `test_kill_switch_blocks_execution` | مفتاح الإيقاف يوقف فعلاً |
| `test_exhausted_credits_block_execution` | الرصيد يمنع الإنفاق |
| `test_plans_are_public_listed` | الخطط الثلاث موجودة |
| `test_free_plan_activates_without_payment` | الخطة المجانية تشتغل بلا بوابة دفع |
| `test_brand_file_is_writable_and_readable` | ملف البراند بيُكتب ويتقرا |
| `test_usage_is_recorded` | كل تشغيل بيُسجّل استخدام |
| `test_audit_log_written` | كل تشغيل له أثر تدقيق |
| `test_dashboard_pages_render` | كل الـ ١٦ شاشة بتفتح ٢٠٠ |

```bash
make test     #  17 passed
```

</div>
