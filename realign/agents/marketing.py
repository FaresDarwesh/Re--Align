"""Marketing department agents: research → content → creative → distribution
→ audience/email → analytics (the loop closes back into research)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from realign.agents.base import AgentResult, BaseAgent
from realign.agents.context import AgentContext
from realign.agents.helpers import channel_label, enqueue_event, schedule_publish
from realign.core.utils import truncate, utcnow
from realign.models import ContentAsset
from realign.models.operations import AssetStatus

# --------------------------------------------------------------------------- #
# 01 · Research
# --------------------------------------------------------------------------- #


class ResearchAgent(BaseAgent):
    key = "research"
    name = "Research"
    name_ar = "وكيل البحث"
    department = "research"
    description_ar = (
        "يراقب السوق والمنافسين وإشارات الطلب، ويحوّلها إلى ملخص بحثي وزوايا جاهزة "
        "لكتابة المحتوى. يبحث قبل أن يُطلب منه."
    )
    icon = "◎"
    requires_approval = False
    default_tools = ["web_search", "trends", "competitor_ads", "internal_kb"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        topic = ctx.params.get("topic") or ctx.params.get("goal") or "السوق والمنافسين"
        ctx.step("collect_signals", f"الموضوع: {topic}")

        brief = await ctx.think(
            (
                f"أنت باحث سوق محترف. الهدف: {topic}\n\n"
                f"{ctx.brand_brief()}\n\n"
                "اكتب ملخصاً بحثياً منظماً بالعربية يحتوي:\n"
                "1) الفرصة/الفجوة المكتشفة (جملتين)\n"
                "2) ثلاث زوايا قابلة للتنفيذ، كل زاوية: العنوان + لمن توجّه + الدليل\n"
                "3) كلمات مفتاحية مقترحة\n"
                "4) ما يجب تجنّبه\n"
                "5) الخطوة التالية\n"
                "كن محدداً وعملياً، بلا حشو."
            ),
            system="أنت باحث سوق دقيق. لا تخترع أرقاماً؛ إن لم تتوفر أدلة فاذكر أنها غير متوفرة.",
            task="research_brief",
            max_tokens=1800,
        )

        judgement = await ctx.decide(
            task="score",
            prompt=(
                f"قيّم قوة هذا الملخص البحثي من ٠ إلى ١ على ثلاثة أبعاد، "
                f"ثم اقترح أفضل زاوية للبدء.\n\n{brief[:3000]}"
            ),
            schema={
                "type": "object",
                "properties": {
                    "novelty": {"type": "number", "minimum": 0, "maximum": 1, "title": "novelty score"},
                    "actionability": {"type": "number", "minimum": 0, "maximum": 1, "title": "actionability score"},
                    "audience_fit": {"type": "number", "minimum": 0, "maximum": 1, "title": "audience fit score"},
                    "best_angle": {"type": "string", "title": "best angle"},
                    "reason": {"type": "string", "title": "short reason"},
                },
                "required": ["novelty", "actionability", "audience_fit", "best_angle"],
            },
            fallback={"novelty": 0.5, "actionability": 0.5, "audience_fit": 0.5, "best_angle": "", "reason": "تعذّر التقييم"},
        )

        score = round(
            (
                float(judgement.get("novelty", 0.5))
                + float(judgement.get("actionability", 0.5))
                + float(judgement.get("audience_fit", 0.5))
            )
            / 3,
            2,
        )

        asset = await ctx.create_asset(
            kind="research_brief",
            title=truncate(judgement.get("best_angle") or topic, 120) or f"ملخص بحثي — {topic}",
            body=brief,
            summary=truncate(brief, 280),
            channel="internal",
            auto_status=AssetStatus.APPROVED,  # internal artefact, no publication risk
            score=score,
            meta={"topic": topic, "judgement": judgement},
        )
        ctx.step("brief_ready", asset.title, score=score)

        # Feed the loop: tell content what to write next.
        await enqueue_event(
            ctx,
            type="handoff",
            payload={
                "from": "research",
                "to": "content",
                "asset_id": asset.id,
                "angle": judgement.get("best_angle") or topic,
                "auto": bool(ctx.params.get("auto_chain", False)),
            },
        )

        return AgentResult(
            summary=f"ملخص بحثي جاهز: {asset.title}",
            assets=[asset.id],
            metrics={"score": score, "judgement": judgement, **ctx.spend()},
            next_actions=["content"],
            data={"brief": brief[:4000], "angle": judgement.get("best_angle")},
            warnings=ctx.warnings,
        )


# --------------------------------------------------------------------------- #
# 02 · Content
# --------------------------------------------------------------------------- #


class ContentAgent(BaseAgent):
    key = "content"
    name = "Content"
    name_ar = "وكيل المحتوى"
    department = "content"
    description_ar = (
        "يحوّل الزاوية البحثية إلى منشور أو مقال أو سكريبت بصوت البراند، ويقيّمه "
        "للمطابقة وللسيو قبل أن يراه أي إنسان."
    )
    icon = "✎"
    requires_approval = True
    default_tools = ["llm", "seo_scorer", "brand_gate"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        angle = ctx.params.get("angle") or ctx.params.get("topic") or "أهم فائدة لعملائنا"
        kind = ctx.params.get("kind", "post")
        channel = ctx.params.get("channel", "instagram")
        locale = ctx.params.get("locale", ctx.org.locale)

        source = ctx.params.get("source_brief") or ""
        ctx.step("draft", f"{kind} · {channel_label(channel)} · {angle}")

        draft = await ctx.think(
            (
                f"اكتب {kind} باللغة {'العربية' if locale.startswith('ar') else 'الإنجليزية'} "
                f"لقناة {channel_label(channel)}.\n\n"
                f"الزاوية: {angle}\n\n"
                f"{ctx.brand_brief()}\n\n"
                f"السياق البحثي:\n{source[:2500]}\n\n"
                "القواعد: افتح بخشّاف قوي، جمل قصيرة، فقرة واحدة لكل فكرة، "
                "ودعوة واضحة لاتخاذ إجراء في النهاية، وهاشتاقات مناسبة."
            ),
            system="أنت كاتب محتوى محترف يلتزم حرفياً بصوت البراند وقواعده.",
            task="content_draft",
            max_tokens=1500,
        )

        # --- cheap model gate: on-brand / overpromise / sensitive -------------
        gate = await ctx.decide(
            task="gate",
            prompt=(
                "افحص هذه المسوّدة قبل النشر. أجب بـ JSON فقط:\n"
                "- on_brand: هل تطابق صوت البراند وقواعده؟\n"
                "- overpromises: هل فيها وعود مبالغ فيها أو نتائج مضمونة؟\n"
                "- sensitive: هل فيها محتوى حساس أو ادعاءات قانونية/طبية/مالية؟\n"
                "- issues: قائمة مشاكل محددة (إن وجدت)\n"
                "- risk_score: من ٠ إلى ١\n\n"
                f"قواعد البراند:\n{ctx.brand_brief()}\n\nالمسوّدة:\n{draft[:3000]}"
            ),
            schema={
                "type": "object",
                "properties": {
                    "on_brand": {"type": "boolean"},
                    "overpromises": {"type": "boolean"},
                    "sensitive": {"type": "boolean"},
                    "issues": {"type": "array", "items": {"type": "string"}},
                    "risk_score": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["on_brand", "overpromises", "sensitive", "risk_score"],
            },
            # Conservative default: if the gate itself fails, treat as risky.
            fallback={"on_brand": False, "overpromises": True, "sensitive": True, "issues": ["تعذّر الفحص"], "risk_score": 1.0},
        )

        seo = await ctx.decide(
            task="seo_score",
            prompt=f"قيّم هذه المسوّدة للسيو وللقراءة (٠ إلى ١) مع ٣ تحسينات.\n\n{draft[:2500]}",
            schema={
                "type": "object",
                "properties": {
                    "seo_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "readability": {"type": "number", "minimum": 0, "maximum": 1},
                    "suggestions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["seo_score", "readability", "suggestions"],
            },
            fallback={"seo_score": 0.5, "readability": 0.6, "suggestions": []},
        )

        risk = float(gate.get("risk_score", 0.5))
        flags: list[str] = []
        if gate.get("overpromises"):
            flags.append("وعود مبالغ فيها")
        if gate.get("sensitive"):
            flags.append("محتوى حساس")
        if not gate.get("on_brand"):
            flags.append("خروج عن صوت البراند")
        flags.extend([f"تحسين: {s}" for s in seo.get("suggestions", [])[:3]])

        asset = await ctx.create_asset(
            kind=kind,
            title=truncate(draft.splitlines()[0] if draft else angle, 120),
            body=draft,
            summary=truncate(draft, 240),
            channel=channel,
            locale=locale,
            score=round(float(seo.get("seo_score", 0.5)), 2),
            seo_score=round(float(seo.get("seo_score", 0.5)), 2),
            brand_score=round(1.0 - risk, 2),
            meta={"angle": angle, "gate": gate, "seo": seo},
        )
        ctx.step("draft_scored", asset.title, risk=risk, seo=asset.seo_score)

        # --- the human gate --------------------------------------------------
        auto_ok = (
            ctx.org.auto_publish
            and not ctx.dry_run
            and gate.get("on_brand")
            and not gate.get("overpromises")
            and not gate.get("sensitive")
            and risk < 0.25
        )

        if auto_ok:
            asset.status = AssetStatus.APPROVED
            await schedule_publish(ctx, asset=asset, channel=channel)
            ctx.step("auto_approved", "مطابق للبراند ومنخفض المخاطر")
            return AgentResult(
                summary=f"مسوّدة جاهزة ونُشرت تلقائياً: {asset.title}",
                assets=[asset.id],
                metrics={"risk": risk, "seo": asset.seo_score, **ctx.spend()},
                next_actions=["analytics"],
                data={"draft": draft, "gate": gate},
                warnings=ctx.warnings,
            )

        approval = await ctx.request_approval(
            kind="publish",
            title=f"مسوّدة {kind} — {channel_label(channel)}: {truncate(asset.title, 60)}",
            body=draft,
            payload={"asset_id": asset.id, "channel": channel, "kind": kind, "gate": gate},
            asset=asset,
            channel=channel,
            priority=2 if risk > 0.6 else 5,
            risk_score=risk,
            risk_flags=flags,
        )
        return AgentResult(
            summary=f"مسوّدة جاهزة وتنتظر الموافقة: {asset.title}",
            assets=[asset.id],
            approvals=[approval.id],
            metrics={"risk": risk, "seo": asset.seo_score, **ctx.spend()},
            next_actions=["distribution"],
            data={"draft": draft, "gate": gate, "seo": seo},
            warnings=ctx.warnings,
        )


# --------------------------------------------------------------------------- #
# 03 · Creative
# --------------------------------------------------------------------------- #


class CreativeAgent(BaseAgent):
    key = "creative"
    name = "Creative"
    name_ar = "وكيل الإبداع"
    department = "creative"
    description_ar = "يبني الجرافيك والفيديو القصير والتعليق الصوتي من نفس المسوّدة، بقواعد بصرية ثابتة."
    icon = "❖"
    requires_approval = True
    default_tools = ["image_gen", "video_render", "tts"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        source_id = ctx.params.get("asset_id")
        source = None
        if source_id:
            source = await ctx.load_asset(source_id)
        if source is None:
            result = await ctx.session.execute(
                select(ContentAsset)
                .where(
                    ContentAsset.org_id == ctx.org.id,
                    ContentAsset.kind.in_(["post", "article", "ad"]),
                )
                .order_by(ContentAsset.created_at.desc())
                .limit(1)
            )
            source = result.scalars().first()
        if source is None:
            ctx.warn("لا توجد مسوّدة مصدر؛ سأعمل من الزاوية المعطاة.")

        base_text = (source.body if source else "") or str(ctx.params.get("angle", ""))
        ctx.step("creative_from", truncate(base_text, 80))

        concepts = await ctx.think(
            (
                "حوّل هذا المحتوى إلى ٣ مفاهيم بصرية. لكل مفهوم أعطِ:\n"
                "1) وصف المشهد بالتفصيل (لقطة، ألوان، مزاج)\n"
                "2) النص على التصميم (عنوان قصير + سطر فرعي)\n"
                "3) سكريبت فيديو قصير ٣٠ ثانية (مقسّم لقطات مع الوقت)\n"
                "4) نص التعليق الصوتي (٣٠-٤٥ كلمة)\n\n"
                f"القواعد البصرية للبراند:\n{ctx.brand_brief()}\n\n"
                f"المحتوى:\n{base_text[:2500]}"
            ),
            system="أنت مخرج إبداعي مختصر وعملي.",
            task="creative_concepts",
            max_tokens=1800,
        )

        asset = await ctx.create_asset(
            kind="creative_concept",
            title=f"مفاهيم إبداعية — {truncate(source.title if source else ctx.params.get('angle', ''), 60)}",
            body=concepts,
            summary=truncate(concepts, 240),
            channel=ctx.params.get("channel", "multi"),
            meta={"source_asset_id": source.id if source else None},
        )
        ctx.step("concepts_ready", asset.title)

        approval = await ctx.request_approval(
            kind="publish",
            title=f"٣ مفاهيم إبداعية: {truncate(asset.title, 70)}",
            body=concepts,
            payload={"asset_id": asset.id, "kind": "creative_concept"},
            asset=asset,
            channel="multi",
            priority=5,
            risk_score=0.2,
            risk_flags=["مراجعة بصرية مطلوبة"],
        )
        return AgentResult(
            summary=f"٣ مفاهيم إبداعية بانتظار الموافقة",
            assets=[asset.id],
            approvals=[approval.id],
            metrics=ctx.spend(),
            next_actions=["distribution"],
            data={"concepts": concepts[:4000]},
            warnings=ctx.warnings,
        )


# --------------------------------------------------------------------------- #
# 04 · Distribution
# --------------------------------------------------------------------------- #


class DistributionAgent(BaseAgent):
    key = "distribution"
    name = "Distribution"
    name_ar = "وكيل التوزيع"
    department = "distribution"
    description_ar = "يجدول كل قطعة على كل قناة في أفضل وقت أداء، ويرد على التعليقات والرسائل بصوت البراند."
    icon = "⇅"
    requires_approval = False
    default_tools = ["scheduler", "inbox", "metricool", "manychat"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        result = await ctx.session.execute(
            select(ContentAsset)
            .where(
                ContentAsset.org_id == ctx.org.id,
                ContentAsset.status == AssetStatus.APPROVED,
            )
            .order_by(ContentAsset.created_at.asc())
            .limit(int(ctx.params.get("limit", 10)))
        )
        approved = list(result.scalars().all())
        ctx.step("scan_approved", f"{len(approved)} قطعة معتمدة")

        if not approved:
            return AgentResult(
                summary="لا توجد مواد معتمدة بانتظار النشر.",
                metrics=ctx.spend(),
                next_actions=[],
                warnings=ctx.warnings,
            )

        plan = await ctx.decide(
            task="route",
            prompt=(
                "حدّد أفضل قناة وأفضل ساعة نشر (٠-٢٣) لكل قطعة.\n"
                + "\n".join(f"- {a.id[:8]} | {a.kind} | {a.channel} | {truncate(a.title,60)}" for a in approved[:10])
            ),
            schema={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "asset_id": {"type": "string"},
                                "channel": {"type": "string"},
                                "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                                "reason": {"type": "string"},
                            },
                            "required": ["asset_id", "channel", "hour"],
                        },
                    }
                },
                "required": ["items"],
            },
            fallback={"items": []},
        )

        scheduled = 0
        by_id = {a.id: a for a in approved}
        for item in plan.get("items", [])[:10]:
            asset = by_id.get(str(item.get("asset_id", ""))) if item.get("asset_id") in by_id else None
            if asset is None:
                # model returned a truncated id — resolve by prefix
                prefix = str(item.get("asset_id", ""))
                asset = next((a for a in approved if a.id.startswith(prefix)), None)
            if asset is None:
                continue
            hour = int(item.get("hour", 10))
            when = utcnow().replace(hour=min(max(hour, 0), 23), minute=0, second=0, microsecond=0, tzinfo=None)
            if when < utcnow().replace(tzinfo=None):
                when = when.replace(day=when.day)  # today; worker will publish immediately
            await schedule_publish(ctx, asset=asset, channel=str(item.get("channel") or asset.channel))
            scheduled += 1

        # Inbox: answer comments/DMs in the brand voice (queued, not sent blind)
        inbox = await ctx.decide(
            task="triage",
            prompt="قيّم حِمل صندوق الرسائل الحالي واقترح سياسة الرد (تلقائي/بشري/تجاهل).",
            schema={
                "type": "object",
                "properties": {
                    "auto_reply_share": {"type": "number", "minimum": 0, "maximum": 1},
                    "human_share": {"type": "number", "minimum": 0, "maximum": 1},
                    "note": {"type": "string"},
                },
                "required": ["auto_reply_share", "human_share"],
            },
            fallback={"auto_reply_share": 0.7, "human_share": 0.3, "note": "سياسة افتراضية"},
        )

        return AgentResult(
            summary=f"تم جدولة {scheduled} قطعة للنشر",
            assets=[a.id for a in approved[:scheduled]],
            metrics={"scheduled": scheduled, "inbox_policy": inbox, **ctx.spend()},
            next_actions=["analytics"],
            data={"plan": plan, "inbox": inbox},
            warnings=ctx.warnings,
        )


# --------------------------------------------------------------------------- #
# 05 · Audience & Email
# --------------------------------------------------------------------------- #


class AudienceAgent(BaseAgent):
    key = "audience"
    name = "Audience & Email"
    name_ar = "وكيل الجمهور والبريد"
    department = "audience"
    description_ar = "يكبّر القائمة البريدية، يكتب النشرة وسلاسل المتابعة، ويلتقط العملاء من صفحات الهبوط."
    icon = "✉"
    requires_approval = True
    default_tools = ["kit", "beehiiv", "mailerlite", "typeform"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        topic = ctx.params.get("topic") or ctx.params.get("angle") or "آخر ما نشرناه"
        sequence_len = int(ctx.params.get("emails", 3))
        ctx.step("sequence_plan", f"{sequence_len} رسائل حول: {topic}")

        sequence = await ctx.think(
            (
                f"اكتب سلسلة بريد من {sequence_len} رسائل بالعربية.\n"
                f"الموضوع العام: {topic}\n\n{ctx.brand_brief()}\n\n"
                "لكل رسالة: سطر الموضوع | الفقرة الافتتاحية | القيمة | دعوة لاتخاذ إجراء.\n"
                "اجعل سطر الموضوع أقل من ٥٠ حرفاً."
            ),
            system="أنت كاتب بريد إلكتروني يلتزم بصوت البراند.",
            task="email",
            max_tokens=1600,
        )

        asset = await ctx.create_asset(
            kind="email_sequence",
            title=f"سلسلة بريد ({sequence_len}) — {truncate(topic, 60)}",
            body=sequence,
            summary=truncate(sequence, 240),
            channel="email",
        )
        ctx.step("sequence_ready", asset.title)

        approval = await ctx.request_approval(
            kind="send",
            title=f"سلسلة بريد جاهزة للإرسال: {truncate(topic, 60)}",
            body=sequence,
            payload={"asset_id": asset.id, "kind": "email_sequence", "count": sequence_len},
            asset=asset,
            channel="email",
            priority=4,
            risk_score=0.3,
            risk_flags=["إرسال لقائمة حقيقية"],
        )
        return AgentResult(
            summary=f"سلسلة بريد من {sequence_len} رسائل بانتظار الموافقة",
            assets=[asset.id],
            approvals=[approval.id],
            metrics=ctx.spend(),
            next_actions=["analytics"],
            data={"sequence": sequence[:4000]},
            warnings=ctx.warnings,
        )


# --------------------------------------------------------------------------- #
# 06 · Analytics
# --------------------------------------------------------------------------- #


class AnalyticsAgent(BaseAgent):
    key = "analytics"
    name = "Analytics"
    name_ar = "وكيل التحليلات"
    department = "analytics"
    description_ar = "يقرأ ما عمل وما لم يعمل، ويحوّله إلى صفحة قرار واحدة، ويعيد التغذية للبحث."
    icon = "◫"
    requires_approval = False
    default_tools = ["ga4", "metabase", "looker_studio", "bigquery"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        from sqlalchemy import func as sa_func
        from realign.models import Run

        period_days = int(ctx.params.get("days", 7))
        ctx.step("collect", f"آخر {period_days} يوم")

        rows = (
            await ctx.session.execute(
                select(ContentAsset.kind, sa_func.count(ContentAsset.id))
                .where(ContentAsset.org_id == ctx.org.id)
                .group_by(ContentAsset.kind)
            )
        ).all()
        counts = {kind: int(n) for kind, n in rows}

        run_stats = (
            await ctx.session.execute(
                select(Run.status, sa_func.count(Run.id), sa_func.coalesce(sa_func.sum(Run.cost_micros), 0))
                .where(Run.org_id == ctx.org.id)
                .group_by(Run.status)
            )
        ).all()
        runs = {status: {"count": int(n), "cost_micros": int(c)} for status, n, c in run_stats}

        facts = json.dumps(
            {"assets_by_kind": counts, "runs": runs, "period_days": period_days},
            ensure_ascii=False,
            indent=2,
        )

        report = await ctx.think(
            (
                f"اكتب تقريراً قرار-based من صفحة واحدة بالعربية بناءً على هذه الحقائق:\n{facts}\n\n"
                "البنية المطلوبة:\n"
                "1) أفضل أداء (وما يدل عليه)\n"
                "2) أضعف أداء (والتفسير المحتمل)\n"
                "3) مبلغ مالي: إجمالي تكلفة التشغيل بالدولار وعدد الوحدات المستهلكة\n"
                "4) توصيتان قابلتان للتنفيذ هذا الأسبوع\n"
                "5) ما يجب إيقافه فوراً\n"
                "بلا حشو، بلا عبارات عامة."
            ),
            system="أنت محلل أداء يكتب لصاحب قرار لا يملك وقتاً.",
            task="report",
            max_tokens=1400,
        )

        asset = await ctx.create_asset(
            kind="report",
            title=f"تقرير أداء — آخر {period_days} يوم",
            body=report,
            summary=truncate(report, 240),
            channel="internal",
            auto_status=AssetStatus.APPROVED,
            meta={"facts": json.loads(facts)},
        )
        ctx.step("report_ready", asset.title)

        # Close the loop: feed findings back into research.
        await enqueue_event(
            ctx,
            type="handoff",
            payload={"from": "analytics", "to": "research", "asset_id": asset.id, "auto": bool(ctx.params.get("auto_chain"))},
        )

        total_cost_usd = round(sum(v["cost_micros"] for v in runs.values()) / 1_000_000, 4)
        return AgentResult(
            summary=f"تقرير أداء جاهز — تكلفة التشغيل الكلية ${total_cost_usd}",
            assets=[asset.id],
            metrics={"assets": counts, "runs": runs, "total_cost_usd": total_cost_usd, **ctx.spend()},
            next_actions=["research"],
            data={"report": report[:4000]},
            warnings=ctx.warnings,
        )
