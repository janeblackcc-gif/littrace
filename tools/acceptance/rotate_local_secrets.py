"""轮换 JWT_SECRET_KEY 与 POSTGRES_PASSWORD（本机开发环境）。

为什么不直接手改 .env：
1. 新口令必须由**密码学安全**的随机源生成，不能手打；
2. 旧 PG 口令要**先备份**再做「旧口令已被拒绝」的验证，否则验完就再也证明不了；
3. 脚本只打印新值的前后各 4 位与长度，不把明文写进任何日志或终端。
   新值仅落在两处：`.env`（gitignored）与容器内的 PG 角色口令。

用法：
    python tools/acceptance/rotate_local_secrets.py            # DRY RUN，只看将改什么
    python tools/acceptance/rotate_local_secrets.py --apply    # 真正生成并写入 .env
"""
from __future__ import annotations

import os
import secrets
import shutil
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENV = os.path.join(BASE, ".env")
OLD_BACKUP = os.path.join(BASE, ".rotate-old-secrets.json")  # gitignored，仅本机


def mask(v: str) -> str:
    return f"{v[:4]}…{v[-4:]} (len={len(v)})" if v else "(空)"


def read_env(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


def main() -> int:
    apply = "--apply" in sys.argv
    lines = read_env(ENV)
    current = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            current[k.strip()] = v

    print(f"模式: {'APPLY' if apply else 'DRY RUN'}\n")
    print("将轮换:")
    print(f"  JWT_SECRET_KEY     旧: {mask(current.get('JWT_SECRET_KEY',''))}  -> 新: 64 位 hex（32 字节熵）")
    print(f"  POSTGRES_PASSWORD  旧: {mask(current.get('POSTGRES_PASSWORD',''))}  -> 新: token_urlsafe(24)")

    if not apply:
        print("\nDRY RUN 结束。加 --apply 执行。")
        return 0

    new_jwt = secrets.token_hex(32)          # 64 hex chars，满足 >=32 字符的 fail-closed 校验
    new_pg = secrets.token_urlsafe(24)       # 无引号/空格，可直接进 .env 与 SQL

    # 1) 备份旧值（供「旧口令/旧密钥已失效」验证；gitignored）
    #    两项都备份：JWT 是对称密钥，不留下旧值就无法严格验证「旧 token 被拒」。
    #    若调用方用 --old-jwt=<值> 显式提供（用于补做已轮换过的场景），以它为准。
    import json
    old_jwt = None
    for item in sys.argv:
        if item.startswith("--old-jwt="):
            old_jwt = item.split("=", 1)[1]
    backup = {"POSTGRES_PASSWORD": current.get("POSTGRES_PASSWORD", ""),
              "JWT_SECRET_KEY": old_jwt or current.get("JWT_SECRET_KEY", "")}
    existing = {}
    if os.path.exists(OLD_BACKUP):
        try:
            with open(OLD_BACKUP, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    # 不要用空值覆盖已有备份
    for k, v in backup.items():
        if not v and existing.get(k):
            backup[k] = existing[k]
    with open(OLD_BACKUP, "w", encoding="utf-8") as f:
        json.dump(backup, f)
    os.chmod(OLD_BACKUP, 0o600)
    print(f"\n旧值已备份到 {os.path.basename(OLD_BACKUP)}（gitignored，验证完请删除）")
    print(f"  POSTGRES_PASSWORD 旧: {mask(backup['POSTGRES_PASSWORD'])}")
    print(f"  JWT_SECRET_KEY    旧: {mask(backup['JWT_SECRET_KEY'])}")

    # 2) 先备份 .env，再就地替换（只替换值，保留注释与顺序）
    shutil.copy2(ENV, ENV + ".bak")
    out = []
    for line in lines:
        if line.startswith("JWT_SECRET_KEY="):
            out.append("JWT_SECRET_KEY=" + new_jwt)
        elif line.startswith("POSTGRES_PASSWORD="):
            out.append("POSTGRES_PASSWORD=" + new_pg)
        else:
            out.append(line)
    with open(ENV, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")

    print("已写入 .env（备份 .env.bak）")
    print(f"  JWT_SECRET_KEY     新: {mask(new_jwt)}")
    print(f"  POSTGRES_PASSWORD  新: {mask(new_pg)}")
    print("\n下一步（必须做，否则容器内口令与 .env 不一致）：")
    print("  1) 容器内改角色口令，见 rotate_local_secrets 说明或执行：")
    print("     docker exec littrace-pg psql -U littrace -d littrace_dev -c \"ALTER USER littrace WITH PASSWORD '<新值>';\"")
    print("     （新值可用 --print-pg 取出，或从 .env 读，勿贴到聊天里）")
    print("  2) 重建容器让 compose 环境变量同步：")
    print("     docker compose -f docker-compose.littrace.yml up -d --force-recreate littrace-pg")
    print("  3) 验证 idle_session_timeout 仍为 600000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
