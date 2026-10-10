# ارفع المشروع على GitHub بـ GitHub Desktop

الريبو جاهز: الكود كله متعمّل commits، ومفيش أي أسرار ولا قواعد بيانات جوّاه
(`.gitignore` بيستثني `.env` و `data/`).

## ١. نزّل وفكّ
نزّل `re-align-source.zip` وفكّه — هتلاقي مجلد `re-align` فيه كل حاجة
**بما فيها مجلد `.git`** بكل التاريخ.

## ٢. GitHub Desktop
1. **File → Add Local Repository…** → اختار مجلد `re-align` → **Add**.
2. اتأكد إن الفرع `main`.
3. **Repository → Repository Settings → Remote**:
   حط `https://github.com/اسم_حسابك/re-align.git` → **Save**.
4. دوس **Push origin** (أو Publish).
5. لو الريبو على GitHub اتعمل بـ README بالغلط: امسحها من GitHub الأول
   (صفحة الملف → سلة المهملات) وبعدين Push.

## ٣. وبعدين؟
على طول افتح **`docs/RAILWAY.md`** — من خطوة ٣ وهو شايلك لحد ما المنصة
تبقى شغالة على لينك دائم، وكل Push جديد من GitHub Desktop هيتنشر لوحده.
