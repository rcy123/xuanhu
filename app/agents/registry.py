"""Agent 注册表。

支持按阶段注册 Agent，便于 Supervisor 按阶段路由。
P4-3 使用 fake agents 测试；后续 Phase 5/6 替换为真实业务 Agent。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.agents.base import BaseAgent
from app.schemas.types import Stage


class AgentRegistry:
    """阶段到 Agent 的映射注册表。"""

    def __init__(self, agents: Mapping[Stage, BaseAgent] | None = None) -> None:
        self._agents: dict[Stage, BaseAgent] = dict(agents) if agents else {}

    def register(self, stage: Stage, agent: BaseAgent) -> None:
        """注册阶段 Agent。"""
        self._agents[stage] = agent

    def get(self, stage: Stage) -> BaseAgent | None:
        """获取阶段对应的 Agent。"""
        return self._agents.get(stage)

    def __contains__(self, stage: Stage) -> bool:
        return stage in self._agents

    def __getitem__(self, stage: Stage) -> BaseAgent:
        return self._agents[stage]

    def copy(self) -> AgentRegistry:
        """返回浅拷贝，便于测试时基于原型修改。"""
        return AgentRegistry(self._agents)

    def as_dict(self) -> dict[Stage, BaseAgent]:
        """返回内部字典副本。"""
        return dict(self._agents)
