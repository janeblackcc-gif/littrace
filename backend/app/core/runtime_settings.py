"""Fail-closed configuration, separate from application startup side effects."""
from __future__ import annotations
import json
import os
from urllib.parse import urlsplit


def required_secret(name: str, env=None) -> str:
    env = os.environ if env is None else env
    value = env.get(name, "").strip()
    if not value or any(x in value.upper() for x in ("REDACTED", "CHANGEME", "CHANGE_ME", "REPLACE_ME")):
        raise RuntimeError(f"missing or placeholder secret: {name}")
    return value


def env_flag(name: str, default: bool = False, env=None) -> bool:
    env = os.environ if env is None else env
    value = env.get(name, "true" if default else "false").lower().strip()
    if value not in {"true", "false", "1", "0"}:
        raise RuntimeError(f"invalid boolean configuration: {name}")
    return value in {"true", "1"}


def sanitize_no_proxy(env=None) -> list[str]:
    """把 NO_PROXY/no_proxy 里带方括号的 IPv6 条目（如 ``[::1]``）规范化为 ``::1``。

    为什么需要：httpx 在按 no_proxy 构建代理白名单时会把每个条目当 URL 解析，
    ``[::1]`` 会被解析出非法端口 ``':1]'`` 并抛 ``httpx.InvalidURL``。
    表现形式是 /research/stream 在构造 AsyncOpenAI 客户端时 500，而非配置报错。
    本机实测复现与修复见交付说明；这里只做规范化，不改变白名单语义。

    返回被改动的变量名列表（便于日志/验收取证）。
    """
    env = os.environ if env is None else env
    changed = []
    for name in ("NO_PROXY", "no_proxy"):
        raw = env.get(name)
        if not raw:
            continue
        parts = []
        for entry in raw.split(","):
            item = entry.strip()
            if not item:
                continue
            if item.startswith("[") and item.endswith("]"):
                item = item[1:-1]
            parts.append(item)
        fixed = ",".join(parts)
        if fixed != raw:
            env[name] = fixed
            changed.append(name)
    return changed


def cors_origins(raw: str) -> list[str]:
    try:
        origins = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("CORS_ORIGINS must be a JSON array") from exc
    if not isinstance(origins, list) or not origins:
        raise RuntimeError("CORS_ORIGINS must be a nonempty JSON array")
    normalized = []
    for value in origins:
        if not isinstance(value, str) or "*" in value:
            raise RuntimeError("wildcard/invalid CORS origin")
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or not p.netloc or p.username or p.password or p.path not in {"", "/"} or p.query or p.fragment:
            raise RuntimeError("CORS origin must contain only scheme, host and optional port")
        normalized.append(f"{p.scheme}://{p.netloc}")
    return list(dict.fromkeys(normalized))


def validate_startup(env=None) -> dict:
    env = os.environ if env is None else env
    for name in ("POSTGRES_PASSWORD", "JWT_SECRET_KEY", "DASHSCOPE_API_KEY"):
        required_secret(name, env)
    if len(required_secret("JWT_SECRET_KEY", env)) < 32:
        raise RuntimeError("JWT_SECRET_KEY must be at least 32 characters")
    if env_flag("ENABLE_WEB_SEARCH", True, env):
        required_secret("BOCHA_API_KEY", env)
    if env_flag("ENABLE_DOCMIND", False, env):
        required_secret("DOCMIND_ACCESS_KEY_ID", env)
        required_secret("DOCMIND_ACCESS_KEY_SECRET", env)
    if env_flag("ENABLE_LEGACY_FEATURES", False, env):
        required_secret("API_KEY", env)
    return {"cors_origins": cors_origins(env.get("CORS_ORIGINS", '["http://localhost:5173"]')),
            "legacy_enabled": env_flag("ENABLE_LEGACY_FEATURES", False, env),
            "text2sql_enabled": env_flag("ENABLE_TEXT2SQL", False, env)}
