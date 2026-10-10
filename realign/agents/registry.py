"""Agent registry — the single source of truth for what the platform can run."""

from __future__ import annotations

from typing import Any

from realign.agents.base import BaseAgent
from realign.agents.marketing import (
    AnalyticsAgent,
    AudienceAgent,
    ContentAgent,
    CreativeAgent,
    DistributionAgent,
    ResearchAgent,
)
from realign.agents.ops_core import CIOAgent, FinanceAgent, OrchestratorAgent
from realign.agents.revenue import SalesAgent, SupportAgent

AGENT_CLASSES: list[type[BaseAgent]] = [
    OrchestratorAgent,
    ResearchAgent,
    ContentAgent,
    CreativeAgent,
    DistributionAgent,
    AudienceAgent,
    AnalyticsAgent,
    SalesAgent,
    SupportAgent,
    FinanceAgent,
    CIOAgent,
]

_AGENTS: dict[str, BaseAgent] = {cls.key: cls() for cls in AGENT_CLASSES}


def get_agent(key: str) -> BaseAgent | None:
    return _AGENTS.get(key)


def list_agents() -> list[BaseAgent]:
    return list(_AGENTS.values())


def list_agents_meta() -> list[dict[str, Any]]:
    return [a.meta() for a in _AGENTS.values()]


def agent_keys() -> list[str]:
    return list(_AGENTS.keys())


def departments() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for agent in _AGENTS.values():
        out.setdefault(agent.department, []).append(agent.meta())
    return out


__all__ = [
    "get_agent",
    "list_agents",
    "list_agents_meta",
    "agent_keys",
    "departments",
    "AGENT_CLASSES",
]
