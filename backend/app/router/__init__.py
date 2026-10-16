# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""Lazy compatibility exports: importing pure helpers never starts legacy components."""
from importlib import import_module
_EXPORTS = {'document_router': 'document_router', 'search_router': 'search_router', 'chat_router': 'chat_router', 'research_router': 'research_router'}
__all__ = list(_EXPORTS)

def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = import_module(f"{__name__}.{_EXPORTS[name]}")
    value = getattr(module, "router")
    globals()[name] = value
    return value
