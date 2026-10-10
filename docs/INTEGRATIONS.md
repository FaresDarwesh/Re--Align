<div dir="rtl">

# دليل الربطات — إزاي تظبط كل موصّل (وإيه اللي أقدر أعمله وإيه اللي لأ)

كل موصّل جاهز في الكود ومستني المفاتيح بس.
**أنا مش قادر أفتح حسابات ولا أعدّي تحقّق هوية (KYC)** — دي حاجات لازم تعملها إنت.
بس كل حاجة تانية (الكود، التشفير، شاشة الإعداد، زرّ «اختبر الربط»، معالجة الفشل) **خلصانة**.

---

## ١. الموصلات الإدخالية/الإخراجية (١٢ موصّل)

### ✅ شغال من غير أي إعداد

| الموصل | الحالة | ملاحظة |
|---|---|---|
| `mock_channel` | ✅ شغال دلوقتي | بينشر في «وضع تجريبي» وبيسجّل `dryrun_xxx` |
| `webhook` | ✅ شغال | بيبعت لأي URL إنت تحدده |
| `rss` | ✅ شغال | بيقرا أي خلاصة RSS |
| `email` | ⚙️ محتاج SMTP | `SMTP_HOST/PORT/USER/PASSWORD/FROM` في `.env` |

### 🔑 محتاج مفاتيح منك

| الموصل | منين تجيب المفاتيح | إيه المطلوب |
|---|---|---|
| **Telegram** | [@BotFather](https://t.me/BotFather) | `TELEGRAM_BOT_TOKEN` — أسهل واحد، خدّه في دقيقة |
| **Instagram / Facebook** | [developers.facebook.com](https://developers.facebook.com) | تطبيق + **حساب أعمال (Business)** + صلاحيات `instagram_basic, instagram_content_publish, pages_manage_posts` + مراجعة من Meta (تاخد أيام) |
| **LinkedIn** | [linkedin.com/developers](https://www.linkedin.com/developers/apps) | تطبيق + صلاحية `w_member_social` + **شركة موثّقة** على LinkedIn |
| **X (تويتر)** | [developer.x.com](https://developer.x.com) | تطبيق + مستوى وصول (Basic بيكفي للنشر) |
| **WhatsApp Cloud** | Meta for Developers | رقم تليفون موثّق + حساب أعمال Meta + `WHATSAPP_CLOUD_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID` |
| **Shopify** | إعدادات متجرك → Apps | Custom App + `SHOPIFY_STORE_DOMAIN` + `SHOPIFY_ACCESS_TOKEN` |
| **Google Analytics** | [console.cloud.google.com](https://console.cloud.google.com) | Service Account JSON + `GOOGLE_ANALYTICS_PROPERTY_ID` |

**إزاي تظبط أي واحد منهم:**
١. `/app/integrations` → اختار الموصل → حطّ المفاتيح → **«اختبر الربط»**
٢. المفاتيح بتتشفّر فوراً (Fernet) قبل ما تُحفظ في القاعدة
٣. الواجهة **مش بتعرض المفاتيح تاني** — بتعرضها مقنّعة (`sk_live••••1234`)

---

## ٢. مزوّدات الذكاء الاصطناعي (LLM)

`LLM_THINKING_PROVIDER` و`LLM_DECISION_PROVIDER` كل واحد فيهم يستقبل:
`openai` · `anthropic` · `ollama` · `custom` · `mock` (الافتراضي، بلا تكلفة)

| الخيار | التكلفة التقريبية لكل ١٠٠٠ تشغيل | المطلوب |
|---|---|---|
| **mock** (الافتراضي) | $0 | ولا حاجة — تشتغل بيه دلوقتي |
| **Ollama محلي** | كهرباء بس | ثبّت Ollama وشغّل `llama3.1` أو `qwen2.5` |
| **OpenRouter** | $1–5 | مفتاح واحد يوصّلك بكل الموديلات |
| **OpenAI مباشرة** | $5–30 | `OPENAI_API_KEY` |
| **Anthropic مباشرة** | $10–40 | `ANTHROPIC_API_KEY` |

**إعداد مزوّد متوافق مع OpenAI (زي Ollama / vLLM / LM Studio / OpenRouter):**
```
LLM_THINKING_PROVIDER=custom
CUSTOM_BASE_URL=http://localhost:11434/v1
CUSTOM_THINKING_MODEL=qwen2.5:14b
CUSTOM_DECISION_MODEL=qwen2.5:3b
```

> 💡 **BYOK (مفتاح العميل الخاص):** كل مؤسسة تقدر تحطّ مفتاحها من
> `/app/settings` → «مفتاح الذكاء الاصطناعي». متشفّر ومعزول عن باقي المؤسسات.

---

## ٣. بوابات الدفع ⚠️ — الحاجة الوحيدة اللي مفيش مخرج غيرك

**Stripe مش متاحة للشركات المصرية.** البدائل اللي الكود جاهز ليها:

| البوابة | العمولة | الاشتراكات المتكررة | المطلوب للتقديم |
|---|---|---|---|
| **Paymob** ⭐ | ~2.75% + ٣ ج | ✅ توكين + اشتراكات | سجل تجاري · بطاقة ضريبية · حساب بنكي |
| **Kashier** ⭐ | ~2.75% + ٣ ج | ✅ توكين + اشتراكات | سجل تجاري · بطاقة ضريبية · حساب بنكي |
| **Fawry** | حسب الاتفاق | ✅ (دفع نقدي/مرجعي) | سجل تجاري · بطاقة ضريبية |
| **Moyasar** | 2.75% + ٣ ج | ✅ | سجل تجاري (سعودي/خليجي أسهل) |
| **manual** | $0 | ❌ (فواتير يدوية) | ولا حاجة — تشتغل بيه دلوقتي |

### إيه اللي موجود في الكود دلوقتي

- `BILLING_PROVIDER=none|paymob|kashier|fawry|stripe|manual`
- طبقة فوترة واحدة مش بتتغيّر لو غيّرت البوابة
- **تحقّق توقيع الويبهوك بـ HMAC** في `/api/v1/webhooks/billing/{provider}`
- جدول `subscriptions` + `invoices` + تجديد شهري + إعادة محاولة على الفواتير الفاشلة
- الخطة المجانية والفوترة اليدوية **شغالين من غير أي بوابة**

### إيه اللي لازم تعمله إنت (مؤجّل للنهاية)

١. **الورق:** سجل تجاري + بطاقة ضريبية + حساب بنكي باسم الشركة (أو استخدم
   شركة مسجّلة في الإمارات/السعودية لو عايز Stripe/Moyasar)
٢. قدّم على **Paymob** أو **Kashier** (أسهل إجراء وأسرع قبول في مصر)
٣. بعد القبول هتستلم: `API Key` · `HMAC Secret` · `Integration ID` · `Iframe ID`
٤. حطّهم في `.env`:
   ```
   BILLING_PROVIDER=paymob
   PAYMOB_API_KEY=...
   PAYMOB_HMAC_SECRET=...
   PAYMOB_INTEGRATION_ID=...
   PAYMOB_IFRAME_ID=...
   BILLING_WEBHOOK_SECRET=...
   ```
٥. في لوحة البوابة حطّ رابط الويبهوك:
   `https://yourdomain.com/api/v1/webhooks/billing/paymob`

---

## ٤. البريد (SMTP)

بدونه: مفيش تأكيد حساب ولا دعوات فريق. إعدادات جاهزة لأي مزوّد:

```
SMTP_HOST=smtp.resend.com     # أو Gmail · Mailgun · Brevo · أي مزوّد
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_FROM="RE-ALIGN <no-reply@yourdomain.com>"
```

**خيار مجاني ممتاز:** [Resend](https://resend.com) — ٣٠٠٠ إيميل/شهر ببلاش.

---

## ٥. ترتيب يُنصح بيه (من الأسهل للأصعب)

| الأولوية | الحاجة | الوقت | الأثر |
|---|---|---|---|
| ١ | مفتاح LLM حقيقي | ١٠ د | الوكلاء تطلع نصوص حقيقية |
| ٢ | Telegram bot | ٥ د | نشر فعلي واختبار حقيقي |
| ٣ | SMTP (Resend) | ١٠ د | تسجيل حسابات ودعم |
| ٤ | دومين + استضافة | ٣٠ د | رابط حقيقي للعملاء |
| ٥ | مفتاح Ollama على سيرفر | ساعة | تكلفة LLM شبه صفر |
| ٦ | بوابة الدفع | أيام/أسابيع | فلوس حقيقية |
| ٧ | Meta / LinkedIn | أيام/أسابيع | نشر على السوشيال |

</div>
