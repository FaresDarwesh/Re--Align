"""Finance (ops) + the two leadership agents: Orchestrator and CIO."""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func as sa_func
from sqlalchemy import select

from realign.agents.base import AgentResult, BaseAgent
from realign.agents.context import AgentContext
from realign.agents.helpers import enqueue_event
from realign.core.utils import micros_to_usd, truncate, utcnow
from realign.models import ContentAsset, Invoice, Integration, UsageRecord
from realign.models.operations import AssetStatus
from realign.models.system import IntegrationStatus

# --------------------------------------------------------------------------- #
# Finance
# --------------------------------------------------------------------------- #


class FinanceAgent(BaseAgent):
    key = "finance"
    name = "Finance"
    name_ar = "وكيل المالية"
    department = "finance"
    description_ar = (
        "يراقب الإنفاق والفواتير ويكتشف الشذوذ قبل أن يكلّف مالاً: إعلان بيولّع ميزانية، "
        "فاتورة متأخرة، وحدة مستهلكة زيادة. يخرج صفحة واحدة كل صباح."
    )
    icon = "∑"
    requires_approval = True
    default_tools = ["invoicing", "stripe", "paymob", "ledger"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        days = int(ctx.params.get("days", 30))
        since = utcnow().replace(tzinfo=None) - timedelta(days=days)
        ctx.step("collect", f"آخر {days} يوم")

        usage = (
            await ctx.session.execute(
                select(
                    sa_func.coalesce(sa_func.sum(UsageRecord.cost_micros), 0),
                    sa_func.coalesce(sa_func.sum(UsageRecord.credits), 0),
                    sa_func.coalesce(sa_func.count(UsageRecord.id), 0),
                ).where(UsageRecord.org_id == ctx.org.id, UsageRecord.created_at >= since)
            )
        ).one()
        cost_micros, credits, calls = int(usage[0]), int(usage[1]), int(usage[2])

        top_agents = (
            await ctx.session.execute(
                select(
                    UsageRecord.agent_key,
                    sa_func.coalesce(sa_func.sum(UsageRecord.cost_micros), 0).label("c"),
                    sa_func.count(UsageRecord.id).label("n"),
                )
                .where(UsageRecord.org_id == ctx.org.id, UsageRecord.created_at >= since)
                .group_by(UsageRecord.agent_key)
                .order_by(sa_func.sum(UsageRecord.cost_micros).desc())
                .limit(5)
            )
        ).all()

        invoices = (
            await ctx.session.execute(
                select(Invoice)
                .where(Invoice.org_id == ctx.org.id, Invoice.status.in_(["open", "draft"]))
                .order_by(Invoice.created_at.desc())
                .limit(10)
            )
        ).scalars().all()

        open_amount = sum(int(i.amount_cents or 0) for i in invoices)
        overdue = [i for i in invoices if i.due_at and i.due_at.replace(tzinfo=None) < utcnow().replace(tzinfo=None)]

        facts = {
            "period_days": days,
            "spend_usd": float(micros_to_usd(cost_micros)),
            "credits": credits,
            "llm_calls": calls,
            "top_agents": [{"agent": a or "-", "usd": float(micros_to_usd(int(c))), "calls": int(n)} for a, c, n in top_agents],
            "open_invoices": len(invoices),
            "open_amount_cents": open_amount,
            "overdue_invoices": len(overdue),
            "credits_monthly": ctx.org.credits_monthly,
            "credits_used": ctx.org.credits_used,
            "credits_remaining": ctx.org.credits_remaining,
            "monthly_budget_cents": ctx.org.monthly_budget_cents,
        }

        report = await ctx.think(
            (
                "اكتب تقريراً مالياً من صفحة واحدة بالعربية.\n\n"
                f"الحقائق:\n{json.dumps(facts, ensure_ascii=False, indent=2)}\n\n"
                "البنية:\n"
                "1) أين ذهبت الأموال (ترتيب تنازلي)\n"
                "2) شذوذ يجب إيقافه اليوم (إن وُجد)\n"
                "3) الفواتير المتأخرة والإجراء المقترح\n"
                "4) مقارنة الاستهلاك بالميزانية الشهرية\n"
                "5) قرار واحد أتخذه اليوم\n"
                "أرقام واضحة، بلا عبارات عامة."
            ),
            system="أنت مدير مالي يكتب لصاحب الشركة صفحة واحدة تكفيه عن لوحة بيانات.",
            task="report",
            max_tokens=1200,
        )

        asset = await ctx.create_asset(
            kind="report",
            title=f"تقرير مالي — آخر {days} يوم",
            body=report,
            summary=truncate(report, 240),
            channel="internal",
            auto_status=AssetStatus.APPROVED,
            meta={"facts": facts},
        )
        ctx.step("report_ready", asset.title)

        approvals = []
        if overdue:
            approval = await ctx.request_approval(
                kind="send",
                title=f"إرسال تذكير تحصيل لـ {len(overdue)} فاتورة متأخرة",
                body="\n".join(f"- {i.number} · {i.amount_cents/100:.2f} {i.currency}" for i in overdue),
                payload={"invoice_ids": [i.id for i in overdue], "action": "dunning"},
                priority=3,
                risk_score=0.35,
                risk_flags=["تواصل مع عملاء حقيقيين"],
            )
            approvals.append(approval.id)

        if ctx.org.credits_remaining <= max(int(ctx.org.credits_monthly * 0.1), 20):
            ctx.warn("الرصيد المتبقي أقل من ١٠٪ من الخطة — اشحن أو خفّض الاستخدام.")

        return AgentResult(
            summary=f"تقرير مالي جاهز — إنفاق ${float(micros_to_usd(cost_micros)):.4f} و{len(overdue)} فاتورة متأخرة",
            assets=[asset.id],
            approvals=approvals,
            metrics={**facts, **ctx.spend()},
            next_actions=[],
            data={"report": report[:4000], "facts": facts},
            warnings=ctx.warnings,
        )


# --------------------------------------------------------------------------- #
# Orchestrator — the chief of staff
# --------------------------------------------------------------------------- #


class OrchestratorAgent(BaseAgent):
    key = "orchestrator"
    name = "Orchestrator"
    name_ar = "وكيل التنسيق"
    department = "core"
    description_ar = (
        "رئيس الأركان: يستلم الهدف ويخطط الخطة ويوزّعها على الوكلاء المختصين، "
        "ويراقب التنفيذ ويصعّد قبل أن تكتشف المشكلة بنفسك."
    )
    icon = "◉"
    requires_approval = False
    default_tools = ["planner", "queue", "mcp"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        goal = str(ctx.params.get("goal") or ctx.params.get("topic") or "حسّن نتائج التسويق هذا الأسبوع")
        execute = bool(ctx.params.get("execute", False))
        ctx.step("plan", truncate(goal, 120))

        plan = await ctx.decide(
            task="route",
            prompt=(
                "أنت منسّق فريق وكلاء. الهدف:\n"
                f"«{goal}»\n\n"
                "الوكلاء المتاحون: research, content, creative, distribution, audience, "
                "analytics, sales, support, finance.\n\n"
                "اختر من ٢ إلى ٤ وكلاء بالترتيب الصحيح، مع سبب سطر واحد لكل واحد، "
                "ومعطيات (params) موجزة لكل واحد.\n"
                "القاعدة: research قبل content، وcontent قبل distribution، وanalytics في الآخر."
            ),
            schema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": {"type": "string"},
                                "reason": {"type": "string"},
                                "params": {"type": "object"},
                            },
                            "required": ["agent"],
                        },
                    },
                    "objective": {"type": "string"},
                    "risk": {"type": "string"},
                },
                "required": ["steps", "objective"],
            },
            fallback={"steps": [{"agent": "research", "reason": "خطة احتياطية", "params": {"topic": goal}}], "objective": goal},
        )

        allowed = {"research", "content", "creative", "distribution", "audience", "analytics", "sales", "support", "finance"}
        steps = [s for s in plan.get("steps", []) if str(s.get("agent", "")) in allowed][:4]
        if not steps:
            steps = [{"agent": "research", "reason": "لا خطة واضحة — ابدأ بالبحث", "params": {"topic": goal}}]

        for s in steps:
            s.setdefault("params", {})
            s["params"].setdefault("goal", goal)
        ctx.step("planned", " → ".join(str(s["agent"]) for s in steps))

        queued = 0
        if execute:
            for s in steps:
                await enqueue_event(
                    ctx,
                    type="run_agent",
                    payload={"agent_key": s["agent"], "params": s.get("params", {}), "parent_run_id": ctx.run.id},
                )
                queued += 1
            ctx.step("queued", f"{queued} مهمة فرعية")

        asset = await ctx.create_asset(
            kind="plan",
            title=f"خطة تنفيذ — {truncate(goal, 60)}",
            body=json.dumps(plan, ensure_ascii=False, indent=2),
            summary=" → ".join(str(s["agent"]) for s in steps),
            channel="internal",
            auto_status=AssetStatus.APPROVED,
            meta={"goal": goal, "executed": execute},
        )

        return AgentResult(
            summary=("تم إطلاق " if execute else "خطة جاهزة: ") + " → ".join(str(s["agent"]) for s in steps),
            assets=[asset.id],
            metrics={"steps": len(steps), "queued": queued, **ctx.spend()},
            next_actions=[str(s["agent"]) for s in steps],
            data={"plan": plan, "executed": execute},
            warnings=ctx.warnings,
        )


# --------------------------------------------------------------------------- #
# CIO — the intelligence layer
# --------------------------------------------------------------------------- #


class CIOAgent(BaseAgent):
    key = "cio"
    name = "Chief Intelligence Officer"
    name_ar = "وكيل الذكاء (CIO)"
    department = "core"
    description_ar = (
        "يحمل الهدف الواحد للشركة (إيراد، هامش، ميزانية) ويحكّم بين الأقسام، "
        "فلا يحسّن وكيل مقياسه على حساب الهدف الأكبر."
    )
    icon = "✦"
    requires_approval = False
    default_tools = ["ledger", "analytics", "planning"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        objective = str(
            ctx.params.get("objective")
            or ctx.params.get("goal")
            or (ctx.brand.offer if ctx.brand and ctx.brand.offer else "زيادة الإيراد مع الحفاظ على الهامش")
        )
        days = int(ctx.params.get("days", 7))
        since = utcnow().replace(tzinfo=None) - timedelta(days=days)
        ctx.step("arbitrate", truncate(objective, 100))

        per_agent = (
            await ctx.session.execute(
                select(
                    UsageRecord.agent_key,
                    sa_func.count(UsageRecord.id).label("n"),
                    sa_func.coalesce(sa_func.sum(UsageRecord.cost_micros), 0).label("c"),
                )
                .where(UsageRecord.org_id == ctx.org.id, UsageRecord.created_at >= since)
                .group_by(UsageRecord.agent_key)
            )
        ).all()
        spend_by_agent = {a or "-": {"calls": int(n), "usd": float(micros_to_usd(int(c)))} for a, n, c in per_agent}

        assets_by_kind = dict(
            (
                await ctx.session.execute(
                    select(ContentAsset.kind, sa_func.count(ContentAsset.id))
                    .where(ContentAsset.org_id == ctx.org.id, ContentAsset.created_at >= since)
                    .group_by(ContentAsset.kind)
                )
            ).all()
        )

        integrations = (
            await ctx.session.execute(
                select(Integration.connector_key, Integration.status).where(Integration.org_id == ctx.org.id)
            )
        ).all()
        gaps = [k for k, s in integrations if s == IntegrationStatus.NOT_CONFIGURED]

        facts = {
            "objective": objective,
            "period_days": days,
            "spend_by_agent": spend_by_agent,
            "assets_by_kind": {k: int(v) for k, v in assets_by_kind.items()},
            "credits_used": ctx.org.credits_used,
            "credits_monthly": ctx.org.credits_monthly,
            "integration_gaps": gaps,
        }

        verdict = await ctx.think(
            (
                f"أنت كبير مسؤولي الذكاء. الهدف الواحد للشركة: {objective}\n\n"
                f"الحقائق:\n{json.dumps(facts, ensure_ascii=False, indent=2)}\n\n"
                "اكتب صفحة واحدة بالعربية:\n"
                "1) هل العمل الحالي يخدم الهدف أم يحسّن مقاييس جانبية؟\n"
                "2) تضارب بين الأقسام يجب حله (إن وجد)\n"
                "3) أعلى قرار أثراً هذا الأسبوع\n"
                "4) ما يجب إيقافه\n"
                "5) الثغرات (ربطات غير مُعدة) التي تعيق القياس"
            ),
            system="أنت تحكم بين الأقسام لصالح هدف واحد واضح. كن حاسماً.",
            task="report",
            max_tokens=1200,
        )

        asset = await ctx.create_asset(
            kind="report",
            title=f"تقرير الذكاء — {truncate(objective, 60)}",
            body=verdict,
            summary=truncate(verdict, 240),
            channel="internal",
            auto_status=AssetStatus.APPROVED,
            meta={"facts": facts},
        )
        ctx.step("verdict_ready", asset.title)

        return AgentResult(
            summary=f"تقرير الذكاء جاهز — الهدف: {truncate(objective, 60)}",
            assets=[asset.id],
            metrics={**facts, **ctx.spend()},
            next_actions=["research"] if gaps else [],
            data={"verdict": verdict[:4000], "facts": facts},
            warnings=ctx.warnings,
        )
