"""Revenue department: sales and customer support."""

from __future__ import annotations

from sqlalchemy import select

from realign.agents.base import AgentResult, BaseAgent
from realign.agents.context import AgentContext
from realign.agents.helpers import channel_label, enqueue_event
from realign.core.utils import truncate
from realign.models import ContentAsset
from realign.models.operations import AssetStatus

# --------------------------------------------------------------------------- #
# Sales
# --------------------------------------------------------------------------- #


class SalesAgent(BaseAgent):
    key = "sales"
    name = "Sales"
    name_ar = "وكيل المبيعات"
    department = "sales"
    description_ar = (
        "يستقبل العميل المحتمل من أي قناة ويقيّم جاهزيته للشراء، يرد خلال دقيقة، "
        "يحجز الموعد، ويكتب العرض، ويكمل المتابعة حتى يُغلق الملف بالقبول أو الرفض."
    )
    icon = "↗"
    requires_approval = True
    default_tools = ["crm", "calendar", "whatsapp", "email", "telegram"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        lead_message = str(ctx.params.get("message") or ctx.params.get("lead_message") or "")
        lead_name = str(ctx.params.get("lead_name") or "العميل")
        channel = str(ctx.params.get("channel") or "whatsapp")
        stage = str(ctx.params.get("stage") or "inbound")
        ctx.step("inbound", f"{channel_label(channel)} · {stage} · {lead_name}")

        triage = await ctx.decide(
            task="triage",
            prompt=(
                f"قيّم هذه الرسالة الواردة من عميل محتمل:\n«{lead_message[:2000]}»\n\n"
                f"البراند:\n{ctx.brand_brief()}\n\n"
                "أعطِ: buy_intent (٠-١ جاهزية الشراء)، urgency (٠-١)، anger (٠-١)، "
                "route (auto_reply | human | sales_call | spam)، والسبب."
            ),
            schema={
                "type": "object",
                "properties": {
                    "buy_intent": {"type": "number", "minimum": 0, "maximum": 1},
                    "urgency": {"type": "number", "minimum": 0, "maximum": 1},
                    "anger": {"type": "number", "minimum": 0, "maximum": 1},
                    "route": {"type": "string", "enum": ["auto_reply", "human", "sales_call", "spam"]},
                    "reason": {"type": "string"},
                },
                "required": ["buy_intent", "urgency", "anger", "route"],
            },
            # Conservative: an unreadable lead goes to a human, never to auto-reply.
            fallback={"buy_intent": 0.0, "urgency": 1.0, "anger": 0.0, "route": "human", "reason": "تعذّر التقييم"},
        )

        intent = float(triage.get("buy_intent", 0.0))
        route = str(triage.get("route", "human"))
        ctx.step("triage", f"intent={intent} route={route}", **triage)

        reply = await ctx.think(
            (
                f"اكتب رداً فورياً بالعربية على {lead_name} عبر {channel_label(channel)}.\n"
                f"الرسالة: «{lead_message[:1500]}»\n"
                f"جاهزية الشراء: {intent}\n\n{ctx.brand_brief()}\n\n"
                "القواعد: ودود ومختصر (٤-٦ جمل)، سؤال واحد مؤهل، دعوة لحجز موعد واضحة. "
                "لا وعود بأسعار ولا التزامات نهائية."
            ),
            system="أنت مسؤول مبيعات سريع الرد ودقيق الالتزام بصوت البراند.",
            task="reply",
            max_tokens=700,
        )

        asset = await ctx.create_asset(
            kind="reply",
            title=f"رد على {lead_name} — {channel_label(channel)}",
            body=reply,
            summary=truncate(reply, 200),
            channel=channel,
            meta={"lead_name": lead_name, "triage": triage, "stage": stage},
        )
        ctx.step("reply_drafted", asset.title)

        # Anything carrying the brand's name to a real human stops here.
        risk = 0.2 if route == "auto_reply" and intent >= 0.6 else 0.55
        if route == "spam":
            asset.status = AssetStatus.REJECTED
            return AgentResult(
                summary="تم تصنيف الرسالة كعشوائية — لا رد.",
                assets=[asset.id],
                metrics={"triage": triage, **ctx.spend()},
                data={"triage": triage},
                warnings=ctx.warnings + ["تم تجاهل الرسالة (spam)"],
            )

        approval = await ctx.request_approval(
            kind="send",
            title=f"رد مقترح على {lead_name} ({channel_label(channel)})",
            body=reply,
            payload={"asset_id": asset.id, "lead_name": lead_name, "channel": channel, "triage": triage},
            asset=asset,
            channel=channel,
            priority=1 if intent >= 0.7 else 3,
            risk_score=risk,
            risk_flags=[] if risk < 0.3 else ["رد موجه لعميل حقيقي — مراجعة مطلوبة"],
        )

        if intent >= 0.7:
            await enqueue_event(
                ctx,
                type="handoff",
                payload={"from": "sales", "to": "content", "note": "عميل ساخن — حضّر محتوى متابعة", "auto": False},
            )

        return AgentResult(
            summary=f"رد جاهز ({route}) بانتظار الموافقة — جاهزية الشراء {intent}",
            assets=[asset.id],
            approvals=[approval.id],
            metrics={"triage": triage, "intent": intent, **ctx.spend()},
            next_actions=["content"] if intent >= 0.7 else [],
            data={"reply": reply, "triage": triage},
            warnings=ctx.warnings,
        )


# --------------------------------------------------------------------------- #
# Support
# --------------------------------------------------------------------------- #


class SupportAgent(BaseAgent):
    key = "support"
    name = "Support"
    name_ar = "وكيل خدمة العملاء"
    department = "support"
    description_ar = (
        "يصنّف مئات الرسائل في ثوان: الغاضب للإنسان، والمشتري للمبيعات، "
        "والباقي يُجاب عنه تلقائياً بصوت البراند."
    )
    icon = "☺"
    requires_approval = True
    default_tools = ["inbox", "helpdesk", "whatsapp", "telegram", "email"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        message = str(ctx.params.get("message") or "")
        channel = str(ctx.params.get("channel") or "whatsapp")
        customer = str(ctx.params.get("customer_name") or "العميل")
        ctx.step("inbound", f"{channel_label(channel)} · {customer}")

        triage = await ctx.decide(
            task="triage",
            prompt=(
                f"صنّف هذه الرسالة:\n«{message[:2000]}»\n\n"
                "أعطِ anger (٠-١)، urgency (٠-١)، buy_intent (٠-١)، "
                "category (billing|technical|complaint|presale|spam)، "
                "route (auto_reply|human|sales)، وملخص من سطر."
            ),
            schema={
                "type": "object",
                "properties": {
                    "anger": {"type": "number", "minimum": 0, "maximum": 1},
                    "urgency": {"type": "number", "minimum": 0, "maximum": 1},
                    "buy_intent": {"type": "number", "minimum": 0, "maximum": 1},
                    "category": {
                        "type": "string",
                        "enum": ["billing", "technical", "complaint", "presale", "spam"],
                    },
                    "route": {"type": "string", "enum": ["auto_reply", "human", "sales"]},
                    "summary": {"type": "string"},
                },
                "required": ["anger", "urgency", "route", "category"],
            },
            fallback={
                "anger": 0.5,
                "urgency": 0.8,
                "buy_intent": 0.0,
                "category": "complaint",
                "route": "human",
                "summary": "تعذّر التصنيف — تحويل لموظف",
            },
        )

        anger = float(triage.get("anger", 0.5))
        route = str(triage.get("route", "human"))
        category = str(triage.get("category", "complaint"))
        ctx.step("triage", f"{category} → {route}", **triage)

        if category == "spam":
            return AgentResult(
                summary="رسالة عشوائية — تم تجاهلها.",
                metrics={"triage": triage, **ctx.spend()},
                data={"triage": triage},
                warnings=ctx.warnings,
            )

        if route == "sales":
            await enqueue_event(
                ctx,
                type="run_agent",
                payload={
                    "agent_key": "sales",
                    "params": {"message": message, "channel": channel, "lead_name": customer},
                },
            )
            return AgentResult(
                summary="رسالة بيعية — تم تحويلها لوكيل المبيعات.",
                metrics={"triage": triage, **ctx.spend()},
                next_actions=["sales"],
                data={"triage": triage},
                warnings=ctx.warnings,
            )

        reply = await ctx.think(
            (
                f"اكتب رد خدمة عملاء بالعربية على {customer}.\n"
                f"الرسالة: «{message[:1500]}»\n"
                f"التصنيف: {category} · مستوى الغضب: {anger}\n\n{ctx.brand_brief()}\n\n"
                "القواعد: ابدأ بتعاطف واضح، اعتذر إن لزم، قدّم حلاً محدداً أو خطوة تالية، "
                "ولا تعد بما لا نملك. مختصر (٣-٥ جمل)."
            ),
            system="أنت موظف خدمة عملاء متعاطف ودقيق.",
            task="reply",
            max_tokens=700,
        )

        asset = await ctx.create_asset(
            kind="reply",
            title=f"رد دعم — {customer} ({category})",
            body=reply,
            summary=truncate(reply, 200),
            channel=channel,
            meta={"triage": triage, "customer": customer},
        )

        # Angry or unclear messages never get an automatic answer.
        needs_human = route == "human" or anger >= 0.6
        approval = await ctx.request_approval(
            kind="send",
            title=f"رد دعم — {customer} ({category})",
            body=reply,
            payload={"asset_id": asset.id, "triage": triage, "channel": channel},
            asset=asset,
            channel=channel,
            priority=1 if needs_human else 4,
            risk_score=min(anger + 0.2, 1.0),
            risk_flags=["عميل غاضب — الأفضلية لإنسان"] if needs_human else [],
        )
        return AgentResult(
            summary=f"رد دعم جاهز ({route}) بانتظار الموافقة",
            assets=[asset.id],
            approvals=[approval.id],
            metrics={"triage": triage, **ctx.spend()},
            data={"reply": reply, "triage": triage},
            warnings=ctx.warnings,
        )
