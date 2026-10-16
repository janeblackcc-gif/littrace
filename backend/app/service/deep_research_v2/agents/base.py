# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
DeepResearch V2.0 - Agent 基类

所有专家Agent的基类，提供通用的LLM调用、日志记录等功能。
"""
import uuid
import os
from service.research_integrity import RUN_STATE, ContractError, read_usage, refresh_usage_totals
from core.runtime_settings import env_flag

import json
import logging
import asyncio
import time
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Union
from datetime import datetime
from openai import AsyncOpenAI

from ..state import ResearchState, AgentLog

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')


class BaseAgent(ABC):
    """
    Agent 基类

    所有专家Agent继承此类，实现特定的 process 方法。
    """

    def __init__(
        self,
        name: str,
        role: str,
        llm_api_key: str,
        llm_base_url: str,
        model: str = "qwen-max"
    ):
        self.name = name
        self.role = role
        self.model = model
        # 超时上限：开启思考模式后单次调用实测可达 80–95s（writer/critic 处理长上下文时更久），
        # 原来的 120s 会在 review 阶段触发 Request timed out。可用 LLM_TIMEOUT 覆盖。
        try:
            _timeout = float(os.getenv("LLM_TIMEOUT", "300"))
        except ValueError:
            _timeout = 300.0
        self.client = AsyncOpenAI(api_key=llm_api_key, base_url=llm_base_url,
                                  max_retries=0, timeout=_timeout)
        self.logger = logging.getLogger(f"Agent.{name}")

    @abstractmethod
    async def process(self, state: ResearchState) -> ResearchState:
        """
        处理状态并返回更新后的状态

        Args:
            state: 当前研究状态

        Returns:
            更新后的状态
        """
        pass

    async def call_llm(self, system_prompt: str, user_prompt: str, json_mode: bool = True,
                       temperature: float = 0.3, max_tokens: int = 16000) -> str:
        state = RUN_STATE.get()
        call = {"call_id": uuid.uuid4().hex, "agent": self.name, "model": self.model,
                "status": "started", "usage": read_usage(None), "request_id": None}
        if state is not None:
            state.setdefault("llm_calls", []).append(call)
        start = time.monotonic()
        try:
            kwargs = {"model": self.model, "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}],
                "temperature": temperature, "max_tokens": max_tokens}
            if json_mode:
                # 百炼对 response_format=json_object 有硬性校验：messages 里必须出现 "json" 字样，
                # 否则直接 400（InvalidParameter: 'messages' must contain the word 'json'...）。
                # 这是实测踩到的真实错误（Scout 的证据抽取系统提示词原本不含该词）。
                # 放在这里统一兜底，避免每个 Agent 各自踩坑；原提示词含 "JSON" 时不做任何改动。
                blob = f"{system_prompt}\n{user_prompt}".lower()
                if not any(k in blob for k in ("json", "javascript object notation")):
                    kwargs["messages"][0]["content"] = f"{system_prompt}\n严格输出 JSON。"
                kwargs["response_format"] = {"type": "json_object"}
            # 思考模式控制（PR1 可信度收口：**默认关闭**）。
            # 旧默认是开启，于是把「报告更长、supported 更多、token 更多」当成了质量改善——
            # 而 supported 判定本身有证据语义缺陷，增 token 不能替代修判定逻辑
            # （先修错误判定，再逐 Agent、固定材料与计划做 thinking 实验）。
            # 因此固定已测能完成的非 thinking 配置作为运行基线；
            # 只有证明错误支持下降、条件识别改善且成本可接受，才把该 Agent 的开关打开。
            # enable_thinking/reasoning_effort 不是 OpenAI 标准参数，按百炼文档通过 extra_body 传。
            if env_flag("LLM_ENABLE_THINKING", False):
                extra: dict = {"enable_thinking": True}
                # reasoning_effort 仅 deepseek-v4.1-flash / v4-flash-0731 / v4-pro-0813 支持；
                # 其他模型带上可能被判为非法参数，故按模型前缀限定。
                if self.model.startswith("deepseek-v4"):
                    extra["reasoning_effort"] = os.getenv("LLM_REASONING_EFFORT", "high")
                kwargs["extra_body"] = extra
            elif self.model.startswith("deepseek-v4"):
                # V4 系列默认开启思考；显式关闭需要传 enable_thinking=False
                kwargs["extra_body"] = {"enable_thinking": False}

            client = self.client
            try:
                response = await client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                # 兜底：本项目的 agent 由 graph.py 以位置参数构造，正常都带有下列方法；
                # 若调用方传入了不完整的替身对象，则不做降级重试，保持可组合性。
                is_rejection = getattr(self, "_is_param_rejection", None)
                if "extra_body" not in kwargs or is_rejection is None or not is_rejection(exc):
                    raise
                retry_kwargs = {k: v for k, v in kwargs.items() if k != "extra_body"}
                getattr(self.logger, "warning", lambda *a, **k: None)(
                    "thinking-mode params rejected by provider; retrying without them (model=%s)", self.model)
                call["thinking_fallback"] = f"{type(exc).__name__}: thinking params rejected, retried without"
                response = await client.chat.completions.create(**retry_kwargs)
            call["usage"] = read_usage(getattr(response, "usage", None))
            call["request_id"] = getattr(response, "_request_id", None)
            choice = response.choices[0]
            if getattr(choice, "finish_reason", None) == "length":
                raise ContractError("LLM output was truncated; do not accept partial JSON")
            content = choice.message.content
            if not isinstance(content, str) or not content.strip():
                raise ContractError("LLM returned empty content")
            call["status"] = "completed"
            return content
        except asyncio.CancelledError:
            call["status"] = "cancelled_usage_unknown"
            raise
        except Exception as exc:
            call["status"], call["error_type"] = "failed", type(exc).__name__
            raise
        finally:
            call["duration_ms"] = round((time.monotonic() - start) * 1000)
            if state is not None:
                refresh_usage_totals(state)
            # Do not log prompts, credentials, or raw provider errors.
            self.logger.info("LLM attempt %s: %s, %sms", call["call_id"], call["status"], call["duration_ms"])

    @staticmethod
    def _is_param_rejection(exc: Exception) -> bool:
        """判断异常是否属于「参数不被接受」，用于决定能否安全重试。

        只认参数/请求层面的错误：显式 400，或错误文本里出现参数相关字样。
        鉴权（401）、限流（429）、网络等一律不重试——重试它们既没用又会掩盖真问题。
        """
        status = getattr(exc, "status_code", None)
        if status is None:
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", None)
        if status == 400:
            return True
        if status is not None:
            return False

    def parse_json_response(self, response: str) -> Dict[str, Any]:
        """安全解析JSON响应，处理markdown代码块和格式问题"""
        import re

        def fix_escaped_newlines(s: str) -> str:
            """修复过度转义的换行符"""
            # 处理多层转义: \\\\n -> \n, \\n -> \n
            s = s.replace('\\\\\\\\n', '\n')
            s = s.replace('\\\\n', '\n')
            # 处理可能的 \\r\\n
            s = s.replace('\\\\\\\\r', '\r')
            s = s.replace('\\\\r', '\r')
            return s

        def try_parse(s: str) -> Optional[Dict]:
            """尝试解析JSON，包含修复逻辑"""
            # 清理常见问题
            s = s.strip()
            # 移除可能的BOM
            if s.startswith('\ufeff'):
                s = s[1:]

            try:
                result = json.loads(s)
                # 成功解析后，修复值中的转义字符
                return self._fix_escaped_values(result)
            except json.JSONDecodeError:
                pass

            # 尝试修复常见JSON问题
            try:
                # 修复无效的JSON转义序列 \[ \] \# 等 (LLM经常产生这种错误)
                # 需要在字符串值中修复，但避免影响已转义的反斜杠
                # \\[ 是有效的(表示 \[ 字面量)，但 \[ 不是有效的JSON转义
                s = re.sub(r'(?<!\\)\\(?!["\\/bfnrtu])', '', s)
                # 移除注释
                s = re.sub(r'//.*?$', '', s, flags=re.MULTILINE)
                s = re.sub(r'/\*.*?\*/', '', s, flags=re.DOTALL)
                # 修复尾随逗号
                s = re.sub(r',(\s*[}\]])', r'\1', s)
                # 修复缺少逗号的情况（在 } 或 ] 后面缺少逗号）
                s = re.sub(r'([}\]])(\s*)([{\[])', r'\1,\2\3', s)
                # 修复没有引号的key
                s = re.sub(r'(\{|\,)\s*(\w+)\s*:', r'\1"\2":', s)
                result = json.loads(s)
                return self._fix_escaped_values(result)
            except json.JSONDecodeError:
                pass

            return None

        # 1. 先尝试直接解析
        result = try_parse(response)
        if result:
            self.logger.debug("Direct JSON parse succeeded")
            return result

        # 2. 尝试提取markdown代码块
        code_block_pattern = r'```(?:json)?\s*([\s\S]*?)\s*```'
        match = re.search(code_block_pattern, response)
        if match:
            result = try_parse(match.group(1))
            if result:
                self.logger.debug("Extracted JSON from code block")
                return result

        # 3. 尝试找到最外层的 {...}
        start = response.find('{')
        end = response.rfind('}')
        if start != -1 and end != -1 and end > start:
            result = try_parse(response[start:end+1])
            if result:
                self.logger.debug("Extracted JSON from braces")
                return result

        # 4. 最后尝试用更宽松的方式解析
        try:
            # 使用 ast.literal_eval 作为备选
            import ast
            # 将 true/false/null 转换为 Python 格式
            s = response
            s = re.sub(r'\btrue\b', 'True', s)
            s = re.sub(r'\bfalse\b', 'False', s)
            s = re.sub(r'\bnull\b', 'None', s)
            start = s.find('{')
            end = s.rfind('}')
            if start != -1 and end != -1:
                result = ast.literal_eval(s[start:end+1])
                if isinstance(result, dict):
                    self.logger.debug("Parsed using ast.literal_eval")
                    return result
        except:
            pass

        self.logger.error(f"JSON parse error, could not extract valid JSON")
        self.logger.warning(f"Raw response (first 800 chars): {response[:800]}")
        return {}

    def _fix_escaped_values(self, obj: Any, key: str = None) -> Any:
        """
        递归修复字典和列表中的转义字符

        注意：对于 'code' 字段，不处理转义，因为代码中的 \n 是有意义的转义序列
        """
        if isinstance(obj, dict):
            return {k: self._fix_escaped_values(v, key=k) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._fix_escaped_values(item, key=key) for item in obj]
        elif isinstance(obj, str):
            # 对于代码字段，不进行转义处理
            # 因为代码中的 \n 应该保持为 \n（两个字符），而不是真正的换行
            if key in ('code', 'fixed_code', 'revised_content'):
                return obj

            # 对于其他字段，修复过度转义的换行符
            result = obj
            result = result.replace('\\\\n', '\n')
            result = result.replace('\\n', '\n')
            result = result.replace('\\\\r', '\r')
            result = result.replace('\\r', '\r')
            result = result.replace('\\\\t', '\t')
            result = result.replace('\\t', '\t')
            return result
        else:
            return obj

    def add_message(self, state: ResearchState, event_type: str, content: Any) -> None:
        """
        添加消息到状态（用于SSE流式输出）

        Args:
            state: 研究状态
            event_type: 事件类型
            content: 消息内容
        """
        message = {
            "type": event_type,
            "agent": self.name,
            "timestamp": datetime.now().isoformat(),
            "content": content
        }
        state["messages"].append(message)

        # 如果有消息队列，立即推送（支持实时流式输出）
        if "_message_queue" in state and state["_message_queue"] is not None:
            try:
                state["_message_queue"].put_nowait(message)
                self.logger.info(f"[SSE] Queued event: {event_type} (queue size: {state['_message_queue'].qsize()})")
            except Exception as e:
                self.logger.warning(f"Failed to push message to queue: {e}")
        else:
            self.logger.warning(f"[SSE] No queue available for event: {event_type}")

    def add_log(
        self,
        state: ResearchState,
        action: str,
        input_summary: str,
        output_summary: str,
        duration_ms: int,
        tokens_used: int = 0
    ) -> None:
        """添加执行日志"""
        log = {
            "timestamp": datetime.now().isoformat(),
            "agent": self.name,
            "action": action,
            "input_summary": input_summary,
            "output_summary": output_summary,
            "duration_ms": duration_ms,
            "tokens_used": tokens_used
        }
        state["logs"].append(log)


class AgentRegistry:
    """Agent 注册表"""

    _agents: Dict[str, BaseAgent] = {}

    @classmethod
    def register(cls, agent: BaseAgent) -> None:
        """注册Agent"""
        cls._agents[agent.name] = agent

    @classmethod
    def get(cls, name: str) -> Optional[BaseAgent]:
        """获取Agent"""
        return cls._agents.get(name)

    @classmethod
    def all(cls) -> Dict[str, BaseAgent]:
        """获取所有Agent"""
        return cls._agents.copy()
