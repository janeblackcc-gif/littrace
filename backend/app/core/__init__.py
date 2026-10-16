# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。
"""Compatibility exports without creating database/Redis clients during import."""
from importlib import import_module

_EXPORTS = {
    'get_db': '.database', 'SessionLocal': '.database', 'engine': '.database', 'Base': '.database',
    'verify_password': '.security', 'get_password_hash': '.security',
    'create_access_token': '.security', 'decode_token': '.security',
    'Token': '.security', 'TokenData': '.security',
    'cache': '.redis_client', 'get_redis_client': '.redis_client', 'RedisCache': '.redis_client',
}
__all__ = list(_EXPORTS)

def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
