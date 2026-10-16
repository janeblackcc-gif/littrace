# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
DeepResearch V2.0 - LangGraph 工作流

实现多智能体协作的状态机图：
Plan -> Research -> Analyze -> Write -> Review -> (Revise) -> Complete

使用 LangGraph 实现循环和条件分支。
"""

import logging
import asyncio
from typing import Dict, Any, List, Literal, AsyncGenerator
from datetime import datetime

from service.research_control import is_research_cancelled, clear_cancel_flag, db_call
from service.research_runtime import execute_stages, ui_snapshot
from service.checkpoint_guard import CheckpointGuard
from service.research_integrity import (ContractError, clean_snapshot, validate_resume, validate_kb_binding,
                                        validate_research_config, record_run_config, run_config_digest, run_flags)
from core.database import engine
from core.runtime_settings import env_flag
from contextlib import aclosing
from dataclasses import asdict

# LangGraph 导入 - 如果没有安装则使用简化版本
try:
    from langgraph.graph import StateGraph, END
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    logging.warning("LangGraph not installed. Using simplified workflow.")

from .state import ResearchState, ResearchPhase, create_initial_state
from .agents import ChiefArchitect, DeepScout, CodeWizard, CriticMaster, LeadWriter, DataAnalyst

# 导入检查点服务
try:
    from service.checkpoint_service import get_checkpoint_service
except ImportError:
    try:
        from app.service.checkpoint_service import get_checkpoint_service
    except ImportError:
        # 兼容直接运行脚本的情况
        def get_checkpoint_service():
            return None

# 导入配置
try:
    from config.llm_config import get_config
except ImportError:
    try:
        from app.config.llm_config import get_config
    except ImportError:
        # 兼容直接运行脚本的情况
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        from config.llm_config import get_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("DeepResearchGraph")


class DeepResearchGraph:
    """
    DeepResearch V2.0 工作流图

    实现完整的多智能体协作流程：
    1. Plan (ChiefArchitect) - 分析问题，生成研究大纲
    2. Research (DeepScout) - 并行深度搜索
    3. Analyze (CodeWizard) - 数据分析和可视化
    4. Write (LeadWriter) - 撰写报告
    5. Review (CriticMaster) - 对抗式审核
    6. Revise (LeadWriter) - 修订（如果需要）
    """

    def __init__(
        self,
        llm_api_key: str = None,
        llm_base_url: str = None,
        search_api_key: str = None,
        model: str = None,
        max_iterations: int = None
    ):
        """
        初始化工作流

        所有参数都可从配置文件读取，传入的参数会覆盖配置
        """
        # 获取配置
        config = get_config()

        # 使用传入参数或配置默认值
        self.llm_api_key = llm_api_key or config.api_key
        self.llm_base_url = llm_base_url or config.base_url
        self.search_api_key = search_api_key or config.search_api_key
        self.model = model or config.default_model
        self.max_iterations = config.research.max_iterations if max_iterations is None else max_iterations
        self.research_config = validate_research_config(asdict(config.research))

        # 初始化各个 Agent（使用各自配置的模型）
        self.architect = ChiefArchitect(
            self.llm_api_key, self.llm_base_url,
            config.agents.architect.model
        )
        self.scout = DeepScout(
            self.llm_api_key, self.llm_base_url, self.search_api_key,
            config.agents.scout.model
        )
        self.data_analyst = DataAnalyst(
            self.llm_api_key, self.llm_base_url,
            config.agents.data_analyst.model
        )
        self.wizard = CodeWizard(
            self.llm_api_key, self.llm_base_url,
            config.agents.wizard.model
        )
        self.critic = CriticMaster(
            self.llm_api_key, self.llm_base_url,
            config.agents.critic.model
        )
        self.writer = LeadWriter(
            self.llm_api_key, self.llm_base_url,
            config.agents.writer.model
        )

        logger.info(f"DeepResearchGraph initialized with models:")
        logger.info(f"  - Architect: {config.agents.architect.model}")
        logger.info(f"  - Scout: {config.agents.scout.model}")
        logger.info(f"  - DataAnalyst: {config.agents.data_analyst.model}")
        logger.info(f"  - Wizard: {config.agents.wizard.model}")
        logger.info(f"  - Critic: {config.agents.critic.model}")
        logger.info(f"  - Writer: {config.agents.writer.model}")

        # 检查点服务
        self.checkpoint_service = get_checkpoint_service()

        # 构建图
        if LANGGRAPH_AVAILABLE:
            self.graph = self._build_langgraph()
        else:
            self.graph = None

    def _run_models(self) -> Dict[str, Any]:
        """本次实际执行所用的六 Agent 模型（进配置指纹；恢复时用于比对当前配置）。"""
        return {name: getattr(agent, "model", None) for name, agent in (
            ("architect", self.architect), ("scout", self.scout),
            ("data_analyst", self.data_analyst), ("wizard", self.wizard),
            ("critic", self.critic), ("writer", self.writer))}

    def _save_checkpoint(
        self,
        state: Dict[str, Any],
        user_id: str = None,
        ui_state: Dict[str, Any] = None
    ) -> bool:
        """保存检查点（包含后端状态和 UI 状态）"""
        if not self.checkpoint_service:
            return False

        session_id = state.get("session_id", "")
        if not session_id:
            return False

        try:
            checkpoint_id = self.checkpoint_service.save_checkpoint(
                session_id=session_id,
                state=state,
                user_id=user_id,
                ui_state=ui_state,
                final_report=state.get("final_report")
            )
            if checkpoint_id:
                logger.info(f"Checkpoint saved: {checkpoint_id}")
                return True
        except Exception as e:
            logger.warning(f"Failed to save checkpoint: {e}")

        return False

    def _load_checkpoint(self, session_id: str) -> Dict[str, Any]:
        """加载检查点"""
        if not self.checkpoint_service:
            return None

        try:
            state = self.checkpoint_service.load_checkpoint(session_id)
            if state:
                logger.info(f"Checkpoint loaded for session: {session_id}")
                return state
        except Exception as e:
            logger.warning(f"Failed to load checkpoint: {e}")

        return None

    def get_checkpoint_info(self, session_id: str) -> Dict[str, Any]:
        """获取检查点信息"""
        if not self.checkpoint_service:
            return None
        return self.checkpoint_service.get_checkpoint_info(session_id)

    def _build_langgraph(self):
        """构建 LangGraph 状态图"""
        # 定义图
        workflow = StateGraph(ResearchState)

        # 添加节点
        workflow.add_node("plan", self._plan_node)
        workflow.add_node("research", self._research_node)
        workflow.add_node("analyze", self._analyze_node)
        workflow.add_node("write", self._write_node)
        workflow.add_node("review", self._review_node)
        workflow.add_node("revise", self._revise_node)

        # 设置入口
        workflow.set_entry_point("plan")

        # 添加边
        workflow.add_edge("plan", "research")
        workflow.add_edge("research", "analyze")
        workflow.add_edge("analyze", "write")
        workflow.add_edge("write", "review")

        # 条件边：审核后决定下一步
        workflow.add_conditional_edges(
            "review",
            self._should_revise,
            {
                "revise": "revise",
                "complete": END
            }
        )

        # 修订后回到审核
        workflow.add_edge("revise", "review")

        return workflow.compile()

    async def _plan_node(self, state: ResearchState) -> Dict[str, Any]:
        """规划节点"""
        logger.info("Executing Plan node...")
        # 创建状态副本以避免直接修改
        state = dict(state)
        state["phase"] = ResearchPhase.INIT.value
        result = await self.architect.process(state)
        return dict(result)

    async def _research_node(self, state: ResearchState) -> Dict[str, Any]:
        """研究节点"""
        logger.info("Executing Research node...")
        state = dict(state)
        state["phase"] = ResearchPhase.RESEARCHING.value
        result = await self.scout.process(state)
        return dict(result)

    async def _analyze_node(self, state: ResearchState) -> Dict[str, Any]:
        """分析节点"""
        logger.info("Executing Analyze node...")
        state = dict(state)
        state["phase"] = ResearchPhase.ANALYZING.value
        result = await self.wizard.process(state)
        return dict(result)

    async def _write_node(self, state: ResearchState) -> Dict[str, Any]:
        """写作节点"""
        logger.info("Executing Write node...")
        state = dict(state)
        state["phase"] = ResearchPhase.WRITING.value
        result = await self.writer.process(state)
        return dict(result)

    async def _review_node(self, state: ResearchState) -> Dict[str, Any]:
        """审核节点"""
        logger.info("Executing Review node...")
        state = dict(state)
        state["phase"] = ResearchPhase.REVIEWING.value
        result = await self.critic.process(state)
        return dict(result)

    async def _revise_node(self, state: ResearchState) -> Dict[str, Any]:
        """修订节点"""
        logger.info("Executing Revise node...")
        state = dict(state)
        state["phase"] = ResearchPhase.REVISING.value
        result = await self.writer.process(state)
        return dict(result)

    def _should_revise(self, state: ResearchState) -> Literal["revise", "complete"]:
        """决定是否需要修订"""
        # 检查是否有未解决的严重问题
        if state["unresolved_issues"] > 0 and state["iteration"] < state["max_iterations"]:
            return "revise"
        return "complete"

    async def run(self, query: str, session_id: str, resume: bool = False, user_id: str = None,
                  search_web: bool = True, search_local: bool = False, kb_binding: Dict = None,
                  direction_request: Dict = None) -> AsyncGenerator[Dict[str, Any], None]:
        if not user_id:
            raise ContractError("Authenticated user_id is required")
        guard = CheckpointGuard(engine, session_id, user_id)
        acquired = False
        try:
            await db_call(guard.__enter__)
            acquired = True
            existing = await db_call(guard.load)
            if resume:
                if existing is None:
                    raise ContractError("No checkpoint to resume; start a new session explicitly")
                validate_resume(existing, session_id, user_id)
                if existing.get("run_status") == "completed":
                    raise ContractError("Completed run cannot be resumed")
                state = existing
                validate_research_config(state["research_config"])
                # Persisted mode/binding/config are authoritative. Never overwrite from default arguments.
                # 续跑必须比较**当前有效配置**与检查点记录的配置
                # 检查点里的 run_config_hash 是「保存输入」的哈希，而实际继续执行的是
                # 本次构造的 Agent 实例与当前环境开关。两者不一致时拒绝续跑，
                # 否则同一份结果会横跨两套配置，配置指纹就失去意义。
                stored_hash = state.get("run_config_hash")
                if not stored_hash:
                    raise ContractError("checkpoint has no run configuration fingerprint; start a NEW session")
                current_hash = run_config_digest({**state, "run_models": self._run_models(),
                                                  "run_flags": run_flags()})
                if stored_hash != current_hash:
                    raise ContractError(
                        "effective run configuration changed since this checkpoint was written "
                        "(models or evidence-affecting flags); refusing to resume under a different "
                        "configuration — start a NEW session to record a separate execution segment")
                if state.get("search_local"):
                    from service.kb_binding import live_document_ids
                    await db_call(live_document_ids, state["kb_binding"], user_id)
            else:
                if existing is not None:
                    raise ContractError("Session already has a checkpoint: resume it or choose a new session_id")
                if not search_web and not search_local:
                    raise ContractError("No search mode enabled")
                if search_web and not env_flag("ENABLE_WEB_SEARCH", True):
                    raise ContractError("Web search is disabled")
                if search_local:
                    validate_kb_binding(kb_binding, user_id)
                state = create_initial_state(query, session_id, search_web, search_local)
                state.update(user_id=user_id, kb_binding=kb_binding, max_iterations=self.max_iterations,
                             research_config=dict(self.research_config), direction_request=direction_request,
                             # 配置指纹的输入必须与 state 一起落库：模型与运行开关都不在
                             # research_config 里，只用 config 算不出「这份结果用的是哪套配置」。
                             run_models=self._run_models(),
                             run_flags=run_flags())
                record_run_config(state)
                await db_call(guard.save, clean_snapshot(state), ui_snapshot(state))
            await db_call(clear_cancel_flag, session_id)  # after lock; a competing run cannot clear cancellation
            if resume:
                yield {"type": "checkpoint_loaded", "session_id": session_id, "phase": state["phase"],
                       "next_stage": state["next_stage"], "ui_state": ui_snapshot(state)}
            async def save(snapshot, ui):
                await db_call(guard.save, snapshot, ui)
            async def cancelled():
                return await db_call(is_research_cancelled, session_id)
            async with aclosing(self._run_simplified(state, save, cancelled)) as stream:
                async for event in stream:
                    yield event
        finally:
            # Cancellation may arrive after __enter__ acquired the lock but before
            # the awaiting coroutine assigned acquired=True. Inspect the guard itself.
            if guard.conn is not None:
                await db_call(guard.__exit__, None, None, None)
            # Clients are per-service/per-run; close all six connection pools.
            for agent in (self.architect, self.scout, self.data_analyst, self.wizard, self.critic, self.writer):
                close = getattr(getattr(agent, "client", None), "close", None)
                if close is not None:
                    await close()

    async def _run_with_langgraph(self, state: ResearchState) -> AsyncGenerator[Dict[str, Any], None]:
        """使用 LangGraph 执行"""
        # 追踪已输出的消息数量，避免重复
        yielded_count = 0

        try:
            # LangGraph 的流式执行
            async for output in self.graph.astream(state):
                # 提取消息并输出
                for node_name, node_state in output.items():
                    if isinstance(node_state, dict) and "messages" in node_state:
                        messages = node_state["messages"]
                        # 只输出新消息（跳过已输出的）
                        new_messages = messages[yielded_count:]
                        for message in new_messages:
                            yield message
                        yielded_count = len(messages)

        except Exception as e:
            logger.error(f"LangGraph execution error: {e}")
            yield {"type": "error", "content": str(e)}

    async def _run_simplified(self, state: ResearchState, save, cancelled) -> AsyncGenerator[Dict[str, Any], None]:
        agents = {"architect": self.architect, "scout": self.scout, "data_analyst": self.data_analyst,
                  "wizard": self.wizard, "writer": self.writer, "critic": self.critic}
        async with aclosing(execute_stages(state, agents, save, cancelled)) as stream:
            async for event in stream:
                yield event

    async def run_sync(self, query: str, session_id: str, user_id: str = None, **kwargs) -> Dict[str, Any]:
        # Consume the SAME executor; no separate, unguarded orchestration implementation.
        final = None
        async for event in self.run(query, session_id, user_id=user_id, **kwargs):
            if event.get("type") in {"error", "research_cancelled"}:
                raise ContractError(event.get("content", event["type"]))
            if event.get("type") == "research_complete":
                final = event
        if final is None:
            raise ContractError("No complete result")
        return final


def create_research_graph(
    llm_api_key: str = None,
    llm_base_url: str = None,
    search_api_key: str = None,
    model: str = None
) -> DeepResearchGraph:
    """
    工厂函数：创建 DeepResearch 工作流图

    所有参数都是可选的，会从配置文件读取默认值

    Args:
        llm_api_key: LLM API 密钥（可选，默认从配置读取）
        llm_base_url: LLM API 基础 URL（可选，默认从配置读取）
        search_api_key: 搜索 API 密钥（可选，默认从配置读取）
        model: 默认模型名称（可选，默认从配置读取）

    Returns:
        DeepResearchGraph 实例
    """
    return DeepResearchGraph(
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        search_api_key=search_api_key,
        model=model
    )
