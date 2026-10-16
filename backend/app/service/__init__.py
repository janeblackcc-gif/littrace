# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""Lazy compatibility exports: importing pure helpers never starts legacy components."""
from importlib import import_module
_EXPORTS = {'DocumentService': 'document_service', 'ServiceConfig': 'config', 'WebSearchService': 'web_search_service', 'ChatService': 'chat_service', 'SessionService': 'session_service', 'PolicySearchService': 'policy_search_service', 'ResearchService': 'dr_g', 'ReActController': 'react_controller', 'create_default_tools': 'react_controller', 'ToolExecutor': 'tool_executor', 'create_tool_executor': 'tool_executor', 'Text2SQLService': 'text2sql_service', 'create_text2sql_service': 'text2sql_service', 'SmartDataAnalyzer': 'smart_analyzer', 'create_smart_analyzer': 'smart_analyzer', 'ChartGenerator': 'chart_generator', 'create_chart_generator': 'chart_generator'}
__all__ = list(_EXPORTS)

def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = import_module(f"{__name__}.{_EXPORTS[name]}")
    value = getattr(module, name)
    globals()[name] = value
    return value
