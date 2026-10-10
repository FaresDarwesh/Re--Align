"""Deterministic offline provider.

This is what lets the whole platform run — and be demoed end to end — with
**zero API keys and zero cost**. Output is deterministic for a given input
(hashed seed), shaped by `request.metadata["task"]` so every agent still gets
realistic, structured output instead of lorem ipsum.

It is clearly labelled in the UI and in every audit record as `provider=mock`,
so nobody mistakes it for a real model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from typing import Any

from realign.llm.base import LLMProvider, LLMRequest, LLMResponse

_AR_TOPICS = [
    "تحسين تجربة العميل", "خفض تكلفة الاستحواذ", "أتمتة المتابعة",
    "بناء الثقة عبر المحتوى", "التوسع في قنوات جديدة",
]
_HOOKS = [
    "٣ أخطاء بتكلّفك عملاء كل أسبوع وأنت مش واخد بالك",
    "السر مش في السعر... السر في التوقيت",
    "إزاي وفّرنا ٤٠٪ من الميزانية بنفس النتائج",
    "ليه ٨ من كل ١٠ شركات بتفشل في الخطوة دي",
]
_CTAS = [
    "احجز استشارة مجانية النهارده",
    "جرّب النظام ١٤ يوم ببلاش",
    "ابعثلنا رسالة وهنرد خلال ساعة",
    "حمّل الدليل الكامل من اللينك في البايو",
]


def _seed(*parts: str) -> random.Random:
    h = hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()
    return random.Random(int(h[:16], 16))


def _pick(rng: random.Random, seq: list[str]) -> str:
    return seq[rng.randrange(len(seq))]


def _keywords(text: str, limit: int = 6) -> list[str]:
    stop = set("من في على إلى عن مع بدون كل بعض هذه هذا الذي التي هو هي أن إن كان تكون كيف لماذا متى".split())
    words = re.findall(r"[\u0600-\u06FFA-Za-z]{4,}", (text or "").lower())
    seen: list[str] = []
    for w in words:
        if w in stop:
            continue
        if w not in seen:
            seen.append(w)
        if len(seen) >= limit:
            break
    return seen or ["السوق", "العملاء", "الجودة", "النمو"]


class MockProvider(LLMProvider):
    name = "mock"
    supports_json_mode = True

    def __init__(self, *, latency_ms: int = 120, **_: Any) -> None:
        super().__init__(timeout=1, max_retries=1)
        self.latency_ms = latency_ms

    async def complete(self, request: LLMRequest) -> LLMResponse:
        await asyncio.sleep(min(self.latency_ms, 400) / 1000)

        prompt = "\n".join(m.content for m in request.messages if m.content)
        system = request.system or ""
        task = (request.metadata or {}).get("task", "generic")
        rng = _seed(system, prompt, task)

        if request.json_mode:
            data = self._json_for(task, prompt, system, rng, request.json_schema)
            text = json.dumps(data, ensure_ascii=False, indent=2)
        else:
            text = self._text_for(task, prompt, system, rng)
            data = None

        tokens_in = max(len(system + prompt) // 4, 1)
        tokens_out = max(len(text) // 4, 1)
        return LLMResponse(
            text=text,
            json_data=data,
            model=f"mock-{task}",
            provider=self.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_micros=0,
            latency_ms=self.latency_ms,
            finish_reason="stop",
            raw={"mock": True},
        )

    # ------------------------------------------------------------- json ----

    def _json_for(
        self, task: str, prompt: str, system: str, rng: random.Random, schema: dict | None
    ) -> Any:
        if schema:
            return self._fill_schema(schema, prompt, rng)

        handlers = {
            "score": lambda: {"score": round(rng.uniform(0.55, 0.95), 2), "label": _pick(rng, ["good", "average"]), "reason": "تقييم توضيحي (mock)"},
            "route": lambda: {"route": _pick(rng, ["research", "content", "sales", "human"]), "confidence": round(rng.uniform(0.6, 0.98), 2), "reason": "توجيه توضيحي (mock)"},
            "gate": lambda: {
                "on_brand": rng.random() > 0.15,
                "overpromises": rng.random() < 0.12,
                "sensitive": rng.random() < 0.08,
                "issues": [] if rng.random() > 0.2 else ["صياغة مبالغ فيها في العنوان"],
                "risk_score": round(rng.uniform(0.0, 0.45), 2),
            },
            "triage": lambda: {
                "anger": round(rng.uniform(0, 0.4), 2),
                "urgency": round(rng.uniform(0.2, 0.9), 2),
                "buy_intent": round(rng.uniform(0.1, 0.95), 2),
                "route": _pick(rng, ["auto_reply", "human", "spam"]),
            },
            "classify": lambda: {"label": _pick(rng, ["lead", "support", "spam", "fan"]), "confidence": round(rng.uniform(0.6, 0.99), 2)},
        }
        return handlers.get(task, lambda: {
            "ok": True,
            "summary": "مخرج توضيحي من الموفّر التجريبي (mock provider).",
            "keywords": _keywords(prompt, 4),
        })()

    def _fill_schema(self, schema: dict, prompt: str, rng: random.Random) -> Any:
        """Produce a plausible value for a JSON-Schema-ish dict."""
        stype = schema.get("type")
        if "enum" in schema:
            return rng.choice(schema["enum"])
        if stype == "object" or "properties" in schema:
            props: dict[str, Any] = schema.get("properties") or {}
            out: dict[str, Any] = {}
            for key, sub in props.items():
                out[key] = self._fill_schema(sub, prompt, rng)
            return out
        if stype == "array":
            item = schema.get("items") or {"type": "string"}
            return [self._fill_schema(item, prompt, rng) for _ in range(min(schema.get("minItems", 2), 4))]
        if stype == "number":
            lo = schema.get("minimum", 0.0)
            hi = schema.get("maximum", 1.0)
            return round(rng.uniform(float(lo), float(hi)), 2)
        if stype == "integer":
            lo = int(schema.get("minimum", 1))
            hi = int(schema.get("maximum", 10))
            return rng.randint(lo, max(hi, lo))
        if stype == "boolean":
            return rng.random() > 0.25

        key = str(schema.get("title") or schema.get("description") or "")
        low = key.lower()
        if "score" in low:
            return round(rng.uniform(0.5, 0.95), 2)
        if "reason" in low or "note" in low or "why" in low:
            return "سبب توضيحي من الموفّر التجريبي."
        if "url" in low:
            return "https://example.com/resource"
        kws = _keywords(prompt, 3)
        return " ".join(kws) if kws else "نص توضيحي"

    # ------------------------------------------------------------- text ----

    def _text_for(self, task: str, prompt: str, system: str, rng: random.Random) -> str:
        kws = _keywords(prompt, 5)
        topic = _pick(rng, _AR_TOPICS)

        if task == "research_brief":
            return "\n".join([
                f"# ملخص بحثي — {topic}",
                "",
                "## الفرصة المكتشفة",
                f"الفجوة الأكبر في السوق حالياً تدور حول: {', '.join(kws[:4])}. "
                "المنافسون يركزون على السعر بينما يغيب المحتوى القائم على إثبات النتائج.",
                "",
                "## الزوايا المقترحة",
                *(f"{i}. زاوية حول «{k}» — تستهدف العميل في مرحلة المقارنة." for i, k in enumerate(kws[:3], 1)),
                "",
                "## الأدلة",
                "- إشارات طلب بحث متزايدة على الكلمات المفتاحية أعلاه.",
                "- فجوة واضحة في الرسائل: لا أحد يقود بإثبات النتائج.",
                "",
                "## الخطوة التالية",
                "تحويل الزاوية الأولى إلى مسوّدة محتوى وإرسالها لطابور الموافقة.",
            ])

        if task in ("content_draft", "content_rewrite"):
            hook = _pick(rng, _HOOKS)
            return "\n".join([
                hook,
                "",
                f"كتير من الشركات بتشتغل على {kws[0] if kws else 'التسويق'} بنفس الطريقة القديمة، "
                "والنتيجة: ميزانية بتتصرف ونتائج مش واضحة.",
                "",
                f"اللي بنعمله مختلف: بنبني نظام يربط {', '.join(kws[:3] if len(kws) > 3 else kws)} "
                "ببعض، عشان كل خطوة تغذّي اللي بعدها.",
                "",
                "✅ نتائج واضحة وقابلة للقياس",
                "✅ وقت أقل في التنفيذ",
                "✅ تحكّم كامل في كل اللي بينشر",
                "",
                _pick(rng, _CTAS),
                "",
                " ".join(f"#{k.replace(' ', '_')}" for k in kws[:4]),
            ])

        if task == "article":
            return "\n".join([
                f"# {topic}: دليل عملي",
                "",
                "## المقدمة",
                f"في السوق اليوم، {kws[0] if kws else 'المنافسة'} بقت أهم من أي وقت. "
                "الشركات اللي بتكسب مش اللي بتصرف أكتر، بل اللي بتقيس أفضل.",
                "",
                "## المشكلة",
                "أغلب الفرق بتشتغل في جزر منعزلة: فريق بيبحث، فريق بيكتب، وفريق بينشر — "
                "من غير مصدر واحد للحقيقة.",
                "",
                "## الحل",
                "بناء طبقة بيانات مشتركة + وكلاء متخصصين + بوابة موافقة بشرية.",
                "",
                "## خطوات التنفيذ",
                "1. حدد الوظائف قبل الأدوات.",
                "2. اربط كل أداة بمصدر بيانات واحد.",
                "3. حط بوابة موافقة على كل ما يُنشر.",
                "4. قِس النتائج وأرجعها للبحث.",
                "",
                "## الخلاصة",
                "النظام الجيد مش مجموعة أدوات — النظام الجيد دايرة بتقفل على نفسها.",
            ])

        if task == "ad_copy":
            return "\n".join([
                f"عنوان: {_pick(rng, _HOOKS)}",
                f"نص أساسي: وفّر وقتك وخلّي النظام يشتغل على {kws[0] if kws else 'النمو'}.",
                f"دعوة لاتخاذ إجراء: {_pick(rng, _CTAS)}",
            ])

        if task == "email":
            return "\n".join([
                f"الموضوع: {_pick(rng, _HOOKS)}",
                "",
                "مرحباً 👋",
                f"كتبنا لك لأن عندك اهتمام بـ {kws[0] if kws else 'تحسين النتائج'}.",
                "",
                "٣ حاجات هتفرق معاك هذا الأسبوع:",
                "• طريقة أوضح لقياس العائد",
                "• أتمتة للخطوات المتكررة",
                "• تحكّم كامل في اللي بينشر",
                "",
                _pick(rng, _CTAS),
            ])

        if task == "reply":
            return (
                "شكراً لتواصلك معنا 🙏 يسعدنا نساعدك. "
                "ممكن توضح لنا تفاصيل أكتر عن طلبك وهنرد عليك خلال دقائق."
            )

        if task == "seo_score":
            return json.dumps(
                {
                    "seo_score": round(rng.uniform(0.55, 0.92), 2),
                    "readability": round(rng.uniform(0.6, 0.95), 2),
                    "keyword_density": round(rng.uniform(0.01, 0.04), 3),
                    "suggestions": ["أضف عنوان فرعي", "اختصر الجملة الافتتاحية", "أضف دعوة لاتخاذ إجراء"],
                },
                ensure_ascii=False,
                indent=2,
            )

        if task == "report":
            return "\n".join([
                "# تقرير يومي",
                "",
                "## الأداء",
                f"- أعلى موضوع تفاعلاً: {topic}",
                f"- أفضل قناة: {_pick(rng, ['إنستجرام', 'لينكدإن', 'X', 'تيك توك'])}",
                f"- عدد المسوّدات المنتظرة للموافقة: {rng.randint(1, 6)}",
                "",
                "## ملاحظات",
                "- فرصة: إعادة استخدام أفضل بوست في صيغة فيديو قصير.",
                "- تحذير: تراجع طفيف في نسبة فتح الإيميل.",
                "",
                "## التوصية",
                "التركيز على الزاوية القائمة على إثبات النتائج الأسبوع القادم.",
            ])

        if task == "plan":
            return "\n".join([
                "1. تحليل المدخلات وتحديد الهدف.",
                "2. توجيه المهمة للوكيل المختص.",
                "3. إنتاج المسوّدة.",
                "4. إرسالها لبوابة الموافقة.",
                "5. النشر بعد الموافقة.",
                "6. قياس النتيجة وإرجاعها للبحث.",
            ])

        return "\n".join([
            "ملاحظة: هذا مخرج من الموفّر التجريبي (mock).",
            "",
            "ما فهمته من الطلب:",
            *(f"- {k}" for k in kws),
            "",
            "للحصول على مخرجات حقيقية، أضف مفتاح API من صفحة الإعدادات.",
        ])

    async def health(self) -> dict[str, Any]:
        return {"provider": self.name, "ok": True, "configured": True, "note": "offline demo provider"}
