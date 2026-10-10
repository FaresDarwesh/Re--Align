"""English labels for the agent catalogue.

The agent classes themselves are written Arabic-first (that is the product's
primary locale). This module supplies the English side so the interface can be
switched without touching a single agent implementation, and `BaseAgent.meta()`
merges both languages into every payload the API and the UI consume.
"""

from __future__ import annotations

DEPARTMENTS_EN: dict[str, str] = {
    "research": "Market research",
    "content": "Content",
    "creative": "Creative",
    "distribution": "Distribution",
    "audience": "Audience & email",
    "analytics": "Analytics",
    "sales": "Sales",
    "support": "Customer support",
    "finance": "Finance",
    "core": "Leadership & orchestration",
    "general": "General",
}

AGENTS_EN: dict[str, dict[str, str]] = {
    "orchestrator": {
        "name": "Orchestrator",
        "description": (
            "Your chief of staff: it takes the goal, plans the steps, delegates "
            "them to the specialist agents, watches the execution and escalates "
            "before you have to notice the problem yourself."
        ),
    },
    "research": {
        "name": "Research agent",
        "description": (
            "Watches your market, competitors and demand signals, and turns them "
            "into a research brief with ready-to-use angles. It researches before "
            "anyone asks it to."
        ),
    },
    "content": {
        "name": "Content agent",
        "description": (
            "Turns a research angle into a post, article or script in your brand "
            "voice, then scores it for brand match and SEO before any human sees it."
        ),
    },
    "creative": {
        "name": "Creative agent",
        "description": (
            "Builds the graphic, the short video and the voice-over from the same "
            "draft, under fixed visual rules."
        ),
    },
    "distribution": {
        "name": "Distribution agent",
        "description": (
            "Schedules every piece on every channel at its best-performing time, "
            "and answers comments and messages in your brand voice."
        ),
    },
    "audience": {
        "name": "Audience & email agent",
        "description": (
            "Grows your list, writes the newsletter and the follow-up sequences, "
            "and captures leads from your landing pages."
        ),
    },
    "analytics": {
        "name": "Analytics agent",
        "description": (
            "Reads what worked and what did not, compresses it into a single "
            "decision page, and feeds it back to research."
        ),
    },
    "sales": {
        "name": "Sales agent",
        "description": (
            "Picks up a lead from any channel, scores how ready they are to buy, "
            "replies within a minute, books the call, drafts the offer and keeps "
            "following up until the file is closed — won or lost."
        ),
    },
    "support": {
        "name": "Support agent",
        "description": (
            "Triages hundreds of messages in seconds: the angry one goes to a human, "
            "the buyer goes to sales, and the rest is answered automatically in your "
            "brand voice."
        ),
    },
    "finance": {
        "name": "Finance agent",
        "description": (
            "Watches spend and invoices and catches anomalies before they cost money: "
            "an ad burning budget, an overdue invoice, a unit that is over-consuming. "
            "It prints one page every morning."
        ),
    },
    "cio": {
        "name": "Intelligence agent (CIO)",
        "description": (
            "Holds the single company goal (revenue, margin, budget) and arbitrates "
            "between departments, so no agent improves its own metric at the expense "
            "of the bigger one."
        ),
    },
}


def english(key: str) -> dict[str, str]:
    return AGENTS_EN.get(
        key,
        {"name": key.replace("_", " ").title(), "description": ""},
    )


def department_en(department: str) -> str:
    return DEPARTMENTS_EN.get(department, department.replace("_", " ").title())


__all__ = ["DEPARTMENTS_EN", "AGENTS_EN", "english", "department_en"]
