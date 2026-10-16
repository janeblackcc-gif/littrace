# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
DeepResearch V2.0 - 服务入口

提供与现有路由兼容的接口，支持 SSE 流式输出。
"""
from contextlib import aclosing

import os
import json
import uuid
import logging
from typing import AsyncGenerator, Dict, Any, Optional
from datetime import datetime

from .graph import DeepResearchGraph

# 导入配置
try:
    from config.llm_config import get_config
except ImportError:
    try:
        from app.config.llm_config import get_config
    except ImportError:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        from config.llm_config import get_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("DeepResearchV2Service")


class DeepResearchV2Service:
    """
    DeepResearch V2.0 服务

    特点：
    - 多智能体协作
    - 对抗式质检
    - 代码解释器
    - 流式输出
    """

    def __init__(
        self,
        llm_api_key: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        search_api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_iterations: Optional[int] = None
    ):
        """
        初始化服务

        所有参数都是可选的，会从配置文件读取默认值

        Args:
            llm_api_key: LLM API 密钥（可选，默认从配置读取）
            llm_base_url: LLM API 基础 URL（可选，默认从配置读取）
            search_api_key: 搜索 API 密钥（可选，默认从配置读取）
            model: 默认模型名称（可选，默认从配置读取）
            max_iterations: 最大迭代次数（可选，默认从配置读取）
        """
        # 获取配置
        config = get_config()

        self.llm_api_key = llm_api_key or config.api_key
        self.llm_base_url = llm_base_url or config.base_url
        self.search_api_key = search_api_key or config.search_api_key
        self.model = model or config.default_model
        self.max_iterations = config.research.max_iterations if max_iterations is None else max_iterations

        # 创建工作流图（使用配置）
        self.graph = DeepResearchGraph(
            llm_api_key=self.llm_api_key,
            llm_base_url=self.llm_base_url,
            search_api_key=self.search_api_key,
            model=self.model,
            max_iterations=self.max_iterations
        )

        logger.info(f"DeepResearch V2 Service initialized with default model: {self.model}")

    async def research(self, query: str, session_id: Optional[str] = None, kb_name: Optional[str] = None,
                       resume: bool = False, user_id: Optional[str] = None, search_web: bool = True,
                       search_local: bool = False, kb_binding: Optional[Dict] = None,
                       direction_request: Optional[Dict] = None) -> AsyncGenerator[str, None]:
        session_id = session_id or str(uuid.uuid4())
        try:
            async with aclosing(self.graph.run(query, session_id, resume=resume, user_id=user_id,
                    search_web=search_web, search_local=search_local, kb_binding=kb_binding,
                    direction_request=direction_request)) as stream:
                async for event in stream:
                    yield self._format_sse(event)
        except Exception as exc:
            logger.error("Research failed: %s", type(exc).__name__)
            yield self._format_sse({"type": "error", "content": str(exc), "error_type": type(exc).__name__})
        yield "data: [DONE]\n\n"  # transport ended, NOT a quality/pass signal

    def _format_sse(self, event: Dict[str, Any]) -> str:
        """格式化为 SSE 事件"""
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    async def research_sync(self, query: str, session_id: Optional[str] = None,
                            user_id: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        return await self.graph.run_sync(query, session_id or str(uuid.uuid4()), user_id=user_id, **kwargs)


def create_service(
    llm_api_key: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    search_api_key: Optional[str] = None,
    model: Optional[str] = None
) -> DeepResearchV2Service:
    """
    工厂函数：创建 DeepResearch V2 服务

    所有参数都是可选的，会从配置文件读取默认值

    Args:
        llm_api_key: LLM API 密钥（可选）
        llm_base_url: LLM API 基础 URL（可选）
        search_api_key: 搜索 API 密钥（可选）
        model: 默认模型名称（可选）

    Returns:
        DeepResearchV2Service 实例
    """
    return DeepResearchV2Service(
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        search_api_key=search_api_key,
        model=model
    )
