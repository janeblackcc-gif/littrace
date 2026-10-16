"""方向功能的「开关开启」状态下验证（未知方向 ID 行为表）。

为什么单列：`ENABLE_DIRECTION_PROFILES` 在 deploy/.env.example 与
backend/.env.example 里都是 **true**，但本机 .env 未设置（代码默认 false），
因此此前所有验收都是在**关闭**状态下跑的——「开启方向功能时未选方向必须 422」
这条从未被验证。本脚本按样板默认（开启）逐行核对 1.6 的行为表。

行为表
    已知 preset_id          -> 加载对应档案
    拼错/未知 preset_id      -> 422，不回退
    mode=custom 且有文本     -> 进入推导，标记 inferred
    没选方向                 -> 开启方向功能时 422
    自由输入无法形成合法规则   -> 明确说明不支持，停止
    旧检查点缺版本信息        -> 明确不兼容，要求新任务

用法：
    python tools/acceptance/verify_direction_enforcement.py
"""
from __future__ import annotations

import os
import sys
import uuid

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(BASE, "backend", "app"))

# 关键：按样板默认打开方向功能，再导入应用侧模块
os.environ["ENABLE_DIRECTION_PROFILES"] = "true"

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(BASE, ".env"))
os.environ["ENABLE_DIRECTION_PROFILES"] = "true"   # load_dotenv 不覆盖已存在的值，此处再确认

from core.runtime_settings import sanitize_no_proxy, env_flag  # noqa: E402
sanitize_no_proxy()

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def main() -> int:
    print(f"ENABLE_DIRECTION_PROFILES = {env_flag('ENABLE_DIRECTION_PROFILES', False)}\n")

    from service.direction_contracts import load_preset, resolved, DirectionRequest, ContractError
    from fastapi import HTTPException

    # ---------- 行为表逐行 ----------
    print("[1] 未知方向 ID 行为表")
    # 已知 preset_id -> 加载对应档案
    for pid in ("agent_memory", "rag_context", "llm_evaluation"):
        p = load_preset(pid)
        check(f"已知 preset '{pid}' 能加载", p.id == pid, f"name={p.name}")

    # 未知 preset_id -> 422，不回退
    for bad in ("not_a_direction", "agent_memroy", ""):
        try:
            load_preset(bad)
            check(f"未知 preset '{bad}' 被拒绝", False, "竟然被接受（存在静默回退）")
        except ContractError as exc:
            check(f"未知 preset '{bad}' 被拒绝（不回退）", True, str(exc)[:60])

    # mode=custom 且有文本 -> 标记 inferred
    # 注意字段名：契约里是 `rule_origin`（值 preset|inferred）与逐字段的 `field_origins`，
    # **没有**叫 `origin` 的字段——首版断言写错字段名，得到 None 而误报缺口。
    p = load_preset("agent_memory")
    req = DirectionRequest(choice={"mode": "custom", "text": "向量检索的重排序策略"},
                           start_year=2022, end_year=2025, focus_angles=[])
    try:
        r = resolved(p, req, origin="inferred", model="m")
        d = r.model_dump() if hasattr(r, "model_dump") else r
        check("custom 模式 rule_origin=inferred", d.get("rule_origin") == "inferred",
              f"rule_origin={d.get('rule_origin')}")
        check("custom 模式带 profile_hash", bool(d.get("profile_hash")))
        fo = d.get("field_origins") or {}
        check("field_origins 逐字段记录来源（profile.* = inferred）",
              fo.get("profile.facets") == "inferred" and fo.get("global_policy") == "global",
              f"field_origins={fo}")
    except Exception as exc:
        check("custom 模式 rule_origin=inferred", False, f"{type(exc).__name__}: {str(exc)[:90]}")

    # 对照：preset 模式应为 preset，且与 inferred 区分得开
    try:
        rp = resolved(p, DirectionRequest(choice={"mode": "preset", "preset_id": "agent_memory"},
                                          start_year=2022, end_year=2025, focus_angles=[]),
                      origin="preset", model="m")
        dp = rp.model_dump() if hasattr(rp, "model_dump") else rp
        check("preset 模式 rule_origin=preset", dp.get("rule_origin") == "preset",
              f"rule_origin={dp.get('rule_origin')}")
        # 正确语义：profile_hash 只取决于**档案内容**，与来源（preset/inferred）正交。
        # 旧断言写成 `... != ... or True` 是恒真式——永远不可能失败，等于没有断言
        # 。这里改为可失败的真实断言：同一档案内容 + 不同来源 → 哈希必须一致，
        # 来源差异记录在 rule_origin / field_origins 里，而不是哈希里。
        same = resolved(p, DirectionRequest(choice={"mode": "preset", "preset_id": "agent_memory"},
                                            start_year=2022, end_year=2025, focus_angles=[]),
                        origin="preset", model="m")
        ds = same.model_dump() if hasattr(same, "model_dump") else same
        check("同一档案内容 profile_hash 一致（哈希与来源正交）",
              bool(dp.get("profile_hash")) and dp.get("profile_hash") == d.get("profile_hash"),
              f"preset={str(dp.get('profile_hash'))[:12]} inferred={str(d.get('profile_hash'))[:12]}")
        check("来源差异记录在 rule_origin，不混进档案哈希",
              dp.get("rule_origin") == "preset" and d.get("rule_origin") == "inferred",
              f"preset={dp.get('rule_origin')} inferred={d.get('rule_origin')}")
        check("对照对象哈希与 preset 一致（排除偶然相等）",
              ds.get("profile_hash") == dp.get("profile_hash"), str(ds.get("profile_hash"))[:12])
    except Exception as exc:
        check("preset 模式 rule_origin=preset", False, f"{type(exc).__name__}: {str(exc)[:90]}")

    # 没选方向 -> 开启方向功能时 422（走真实路由函数判定）
    print("\n[2] 未选方向时路由必须 422（开启方向功能）")
    from router.research_router import stream_research, ResearchRequest

    class _User:
        id = uuid.uuid4()

    class _DB:
        def rollback(self): pass
        # 哨兵：`_DB.query` 只在越过方向校验之后才被调用；它抛 NotImplementedError 即为「已越过方向校验」。
        def query(self, *a, **k): raise NotImplementedError("sentinel: passed direction validation")
    import asyncio
    try:
        payload = ResearchRequest(query="x", search_modes=["web"], version="v2")   # 故意不给 direction
        asyncio.get_event_loop().run_until_complete(stream_research(payload, _User(), _DB()))
        check("未选方向 -> 422", False, "竟然被接受")
    except HTTPException as exc:
        check("未选方向 -> 422", exc.status_code == 422, f"HTTP {exc.status_code}: {exc.detail}")
    except Exception as exc:
        check("未选方向 -> 422", False, f"{type(exc).__name__}: {str(exc)[:110]}")

    # [2b] 开关**未设置**时也必须 fail-closed：文献产品默认要求显式 direction。
    # PR1 前默认是 false，于是「本机没配这个开关」就等于关掉了方向强制，
    # 请求会静默落到行业规划路径。这条断言在修前会失败。
    print("\n[2b] 开关未设置时缺方向仍必须 422（默认 fail-closed）")
    saved_flag = os.environ.pop("ENABLE_DIRECTION_PROFILES", None)
    try:
        payload = ResearchRequest(query="x", search_modes=["web"], version="v2")   # 故意不给 direction
        asyncio.get_event_loop().run_until_complete(stream_research(payload, _User(), _DB()))
        check("开关未设置时缺方向 -> 422", False, "竟然被接受（默认值是 fail-open）")
    except HTTPException as exc:
        check("开关未设置时缺方向 -> 422", exc.status_code == 422, f"HTTP {exc.status_code}: {exc.detail}")
    except Exception as exc:
        check("开关未设置时缺方向 -> 422", False, f"{type(exc).__name__}: {str(exc)[:110]}")
    finally:
        if saved_flag is not None:
            os.environ["ENABLE_DIRECTION_PROFILES"] = saved_flag

    # 对照：给了 preset 必须越过方向校验。只证明「越过方向校验」必须靠
    # 明确哨兵——`_DB.query` 抛 NotImplementedError 即为证据；任何其他异常（含方向校验自身崩溃）一律失败。
    print("\n[3] 对照：给了方向必须到达后续依赖哨兵（仅有该哨兵证明越过方向校验）")
    try:
        payload = ResearchRequest(query="x", search_modes=["web"], version="v2",
                                  direction={"choice": {"mode": "preset", "preset_id": "agent_memory"},
                                             "start_year": 2022, "end_year": 2025, "focus_angles": []})
        asyncio.get_event_loop().run_until_complete(stream_research(payload, _User(), _DB()))
        check("给了 preset 越过方向校验（命中后续依赖哨兵）", False, "函数在到达哨兵前返回，未证明越过方向校验")
    except HTTPException as exc:
        check("给了 preset 越过方向校验（命中后续依赖哨兵）", False, f"被 HTTP {exc.status_code} 拒绝: {exc.detail}")
    except NotImplementedError as exc:
        check("给了 preset 越过方向校验（命中后续依赖哨兵）", True, f"命中哨兵: {str(exc)[:70]}")
    except Exception as exc:
        check("给了 preset 越过方向校验（命中后续依赖哨兵）", False,
              f"方向校验阶段自身抛错 {type(exc).__name__}: {str(exc)[:90]}")

    # 未知 preset 经路由也必须 422
    print("\n[4] 未知 preset 经路由必须 422")
    try:
        payload = ResearchRequest(query="x", search_modes=["web"], version="v2",
                                  direction={"choice": {"mode": "preset", "preset_id": "typo_direction"},
                                             "start_year": 2022, "end_year": 2025, "focus_angles": []})
        asyncio.get_event_loop().run_until_complete(stream_research(payload, _User(), _DB()))
        check("未知 preset 经路由 -> 422", False, "竟然被接受")
    except HTTPException as exc:
        check("未知 preset 经路由 -> 422", exc.status_code == 422, f"HTTP {exc.status_code}: {exc.detail}")

    # ---------- [4b] 后端请求模型必须**独立**拒绝非法时间窗口----------
    # 前端校验不是安全边界：清空 number 输入得到的 0 曾经被当成合法窗口发出去。
    # 这里直接对请求模型做边界验收，不经过浏览器。
    print("\n[4b] 后端独立拒绝非法时间窗口（不依赖前端）")
    from pydantic import ValidationError
    from service.direction_contracts import DirectionRequest

    invalid_windows = [
        ({"start_year": 0, "end_year": 0}, "0~0（清空 number 输入的结果）"),
        ({"start_year": 2025, "end_year": 2022}, "start_year > end_year"),
        ({"start_year": 1989, "end_year": 2025}, "低于 1990"),
        ({"start_year": 2022, "end_year": 2101}, "高于 2100"),
    ]
    for payload_extra, label in invalid_windows:
        try:
            DirectionRequest(choice={"mode": "preset", "preset_id": "rag_context"}, **payload_extra)
            check(f"后端拒绝非法窗口：{label}", False, "竟然被接受")
        except ValidationError as exc:
            check(f"后端拒绝非法窗口：{label}", True, str(exc).splitlines()[0][:60])
    try:
        DirectionRequest(choice={"mode": "preset", "preset_id": "rag_context"},
                         start_year=2022, end_year=2025)
        check("后端接受合法窗口：2022~2025", True, "ok")
    except ValidationError as exc:
        check("后端接受合法窗口：2022~2025", False, str(exc)[:80])

    # 旧检查点缺版本信息 -> 明确不兼容
    print("\n[5] 旧检查点缺版本信息必须拒绝")
    from service.research_integrity import validate_resume, ContractError as CE
    sid, uid = str(uuid.uuid4()), str(uuid.uuid4())
    good = {"schema_version": 2, "pipeline_version": 2, "session_id": sid, "user_id": uid,
            "next_stage": "plan", "search_local": False}
    bad = dict(good); bad.pop("schema_version")
    try:
        validate_resume(bad, sid, uid)
        check("缺 schema_version 被拒绝", False, "竟然通过")
    except CE as exc:
        check("缺 schema_version 被拒绝", True, str(exc)[:60])
    try:
        validate_resume(dict(good, pipeline_version="old"), sid, uid)
        check("旧 pipeline_version 被拒绝", False, "竟然通过")
    except CE as exc:
        check("旧 pipeline_version 被拒绝", True, str(exc)[:60])

    # [6] 方向合同的**单元测试**（PR4 清单：把"来源标注"测试归位为单元测试，
    #     并补上非法档案 / 不支持取值 / 未知 preset 的负例；这些都不需要调用模型）
    print("\n[6] 方向合同单元测试（档案校验与比较合同，不调用模型）")
    from service.direction_contracts import ClaimRule
    import pydantic
    try:
        ClaimRule(id="bad_rule", family="memory_retention",
                  required_conditions=["task"], required_evidence_blocks=["results"])
        check("非法档案：删掉 family 必需条件必须被拒绝", False, "竟然通过")
    except pydantic.ValidationError as exc:
        check("非法档案：删掉 family 必需条件必须被拒绝", True, str(exc).splitlines()[0][:70])
    try:
        ClaimRule(id="bad_missing", family="mechanism", required_conditions=["task", "method_version"],
                  required_evidence_blocks=["results"], missing_fields="not_applicable")
        check("不支持取值：missing_fields 只允许 insufficient_evidence", False, "竟然通过")
    except pydantic.ValidationError as exc:
        check("不支持取值：missing_fields 只允许 insufficient_evidence", True,
              str(exc).splitlines()[0][:70])
    try:
        ClaimRule(id="bad_compare", family="mechanism", required_conditions=["task", "method_version"],
                  required_evidence_blocks=["results"], comparison_conditions=["dataset"])
        check("比较合同：comparison_conditions 必须是 required_conditions 的子集", False, "竟然通过")
    except pydantic.ValidationError as exc:
        check("比较合同：comparison_conditions 必须是 required_conditions 的子集", True,
              str(exc).splitlines()[0][:70])
    rule = ClaimRule(id="ok_rule", family="retrieval_comparison",
                     required_conditions=["task", "dataset", "model", "corpus_version", "retriever",
                                          "chunk_size", "top_k", "context_budget", "metric"],
                     required_evidence_blocks=["method", "results", "experiment_setup"],
                     comparison_mode="cross_dataset")
    fields = rule.comparison_fields()
    check("比较合同：cross_dataset 允许 dataset 不同、其余必须对齐",
          "dataset" not in fields and "task" in fields and "metric" in fields,
          f"mode={rule.comparison_mode} fields={fields}")
    from service.direction_contracts import load_preset
    try:
        load_preset("agent_memroy")
        check("未知 preset_id 必须拒绝且不回退", False, "竟然通过")
    except CE as exc:
        check("未知 preset_id 必须拒绝且不回退", True, str(exc)[:60])
    for preset in ("agent_memory", "rag_context", "llm_evaluation"):
        try:
            profile = load_preset(preset)
            modes = {r.id: r.comparison_mode for r in profile.claim_types}
            check(f"预置档案 {preset} 声明显式比较合同", bool(modes) and all(modes.values()),
                  str(modes))
        except Exception as exc:  # noqa: BLE001
            check(f"预置档案 {preset} 声明显式比较合同", False, f"{type(exc).__name__}: {exc}")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n方向强制（开启状态）验证: {passed}/{len(results)} PASS")
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} — {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
