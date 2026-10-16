"""验证 JWT_SECRET_KEY 轮换：旧密钥签发的 token 必须失效，新登录必须正常。

为什么要这样验：JWT 是**对称**密钥，拿它在平台上"调用"是验证不了的；
唯一严谨的验证是——用旧密钥签一个结构完全合法、未过期的 token，
看服务端是否拒绝。拒绝才说明轮换真的生效。

用法（需后端已在 8000 运行）：
    python tools/acceptance/verify_jwt_rotation.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from uuid import uuid4

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(BASE_DIR, "backend", "app"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(BASE_DIR, ".env"))

# 与应用同一套实现：项目用的是 python-jose（core/security.py: from jose import jwt），
# 不是 PyJWT。用同一个库才能保证签出来的 token 结构与服务端期望完全一致。
from jose import jwt  # noqa: E402

API = os.getenv("LITTRACE_BASE", "http://127.0.0.1:8000")
OLD_FILE = os.path.join(BASE_DIR, ".rotate-old-secrets.json")
ALGO = os.getenv("JWT_ALGORITHM", "HS256")


def call(path: str, token: str | None = None, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    results = []

    # 0) 注册一个一次性用户，拿一个**新密钥签发**的合法 token
    tag = uuid4().hex[:8]
    creds = {"username": f"jwt_{tag}", "email": f"jwt_{tag}@example.com", "password": "Jwt-Rotation-Passw0rd!"}
    status, body = call("/auth/register", method="POST", payload=creds)
    ok_register = status in (200, 201)
    results.append(("注册测试用户（用新密钥签发 token）", ok_register, f"HTTP {status}"))
    if not ok_register:
        print(f"注册失败: {body[:300]}")
        return 1
    new_token = json.loads(body).get("access_token") or json.loads(body).get("token")

    # 1) 新 token 必须可用
    status, body = call("/auth/me", token=new_token)
    results.append(("新密钥签发的 token 可用", status == 200, f"GET /auth/me -> HTTP {status}"))

    # 2) 用旧密钥签一个合法 token -> 必须被拒
    #
    # 取证文件在 C1-I 闭环后**已按计划删除**（`.rotate-old-secrets.json` 含旧凭证，
    # 不应长期留存）。因此这里必须区分两种情形，否则每次复跑都会显示一个
    # 误导性的 FAIL：凭证轮换已闭环 ≠ 验证失败。
    #   有旧密钥值      -> 真验证「旧 token 被拒」
    #   无旧密钥值      -> SKIP（并说明如何复现），不计入失败
    old_secret = None
    if os.path.exists(OLD_FILE):
        with open(OLD_FILE, encoding="utf-8") as f:
            old_secret = (json.load(f) or {}).get("JWT_SECRET_KEY")
    old_secret = os.getenv("OLD_JWT_SECRET_KEY") or old_secret

    if not old_secret:
        skipped = ("旧密钥验证", "SKIP：取证文件已删除（C1-I 闭环后按计划清理）。"
                   "如需复现，用 OLD_JWT_SECRET_KEY=<旧值> 运行本脚本。")
        print(f"\n  [SKIP] {skipped[0]} — {skipped[1]}")
    else:
        payload = {"sub": "00000000-0000-0000-0000-000000000000", "username": "old",
                   "exp": datetime.now(timezone.utc) + timedelta(minutes=30)}
        old_token = jwt.encode(payload, old_secret, algorithm=ALGO)
        status, body = call("/auth/me", token=old_token)
        results.append(("旧密钥签发的 token 被拒绝", status == 401, f"GET /auth/me -> HTTP {status} {body[:120]}"))

    # 3) 伪造签名也要被拒（对照组，证明 401 不是别的原因）
    payload = {"sub": "00000000-0000-0000-0000-000000000000", "username": "forged",
               "exp": datetime.now(timezone.utc) + timedelta(minutes=30)}
    forged = jwt.encode(payload, "not-the-real-secret", algorithm=ALGO)
    status, body = call("/auth/me", token=forged)
    results.append(("对照组：错误密钥签发的 token 被拒绝", status == 401, f"GET /auth/me -> HTTP {status}"))

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {detail}")
    print(f"\nJWT 轮换验证: {passed}/{len(results)} PASS")
    print("=" * 72)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
