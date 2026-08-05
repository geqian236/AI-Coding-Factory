#!/usr/bin/env python3
"""
tools/compat-probes/emit_manifest.py

Compatibility Manifest 发射器（Phase 0 开发用，未签名）。

从已验证的 SQLite spike receipt 读取实际参数，重算 parameterTupleDigest，
写入完整 eventBatchParameters、sqliteSpikeReceiptDigest 和 benchmarkProfileDigest。

任何输入不匹配、字段缺失或组合未认证均 fail closed；正式签名在 Phase 6。

用法：
  python tools/compat-probes/emit_manifest.py \\
      --spike-receipt tools/compat-probes/sqlite_wal_full/receipt.json \\
      --output <output-path>

算法版本: v1（Phase 0）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ─── 路径常量 ─────────────────────────────────────────────────────────────────

# 从本文件向上 2 层到达 repo root（tools/compat-probes/ -> tools/ -> root）
_REPO_ROOT = Path(__file__).resolve().parents[2]

# benchmark profile 默认路径
DEFAULT_BENCHMARK_PROFILE_PATH = (
    _REPO_ROOT / "contracts" / "benchmarks" / "benchmark-profile.v1.json"
)

# compatibility manifest schema 默认路径
DEFAULT_MANIFEST_SCHEMA_PATH = (
    _REPO_ROOT / "contracts" / "schemas" / "compatibility-manifest.v1.schema.json"
)

# stage/node capability map 默认路径
DEFAULT_STAGE_CAP_MAP_PATH = (
    _REPO_ROOT / "contracts" / "policies" / "stage-capability-map.v1.json"
)
DEFAULT_NODE_CAP_MAP_PATH = (
    _REPO_ROOT / "contracts" / "policies" / "node-capability-map.v1.json"
)

# eventBatchParameters 默认值（Master Spec §11 首版参考值：200 events / 256 KiB / 500 ms）。
# 仅在 spike receipt 未携带 eventBatchParameters 时作为回退（合成回执单元测试用）；
# 真实 SQLite spike receipt 会携带实测 tuple，emitter 优先读取并重算 digest 绑定校验。
DEFAULT_MAX_BATCH_EVENTS = 200
DEFAULT_MAX_BATCH_BYTES = 262144      # 256 KiB
DEFAULT_MAX_BATCH_AGE_MS = 500
SYNCHRONOUS_MODE = "FULL"


# ─── 摘要工具 ─────────────────────────────────────────────────────────────────


def _sha256_file(path: Path) -> str:
    """计算文件 SHA-256 摘要（hex 前缀格式 sha256:...）。"""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _sha256_bytes(data: bytes) -> str:
    """计算字节串 SHA-256 摘要。"""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_dict(d: dict[str, Any]) -> str:
    """按 key 排序序列化后计算 SHA-256 摘要。"""
    return _sha256_bytes(json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _verify_receipt_digests(
    spike_receipt: dict[str, Any],
    benchmark_profile_path: Path,
) -> None:
    """强制绑定 spike receipt 的三组 digest（P0-3 修复：消除命名冲突 + fail closed）。

    1. `benchmarkProfileDigest`：receipt 必须携带，且必须等于冻结
       `benchmark_profile_path` 文件的真实 SHA-256。原 emitter 用的是后者，故旧版
       receipt 自报值（如 cc4421bc…，实际是 bench 内部小结字典摘要）与 emitter
       重算值（03df1bc4…）**永不相等**——这是命名冲突而非真不同步。
       现在 bench 已修正，receipt 自报的就是冻结文件摘要，本校验强制 binding。
    2. `env_digest`：必须等于 `sha256_dict(spike_receipt['environment'])`。
    3. `parameterTupleDigest`：必须等于 emitter 自重算值（去掉软守卫）。
       兼容旧 receipt 把 digest 放到顶层 `param_digest`，二者择一存在即可。

    任一缺失或不匹配都 `raise ValueError`，错误码前缀便于测试 `match`：
      BENCHMARK_PROFILE_DIGEST_MISMATCH / ENV_DIGEST_MISMATCH / PARAM_TUPLE_MISMATCH
    """
    # ── (1) benchmarkProfileDigest ────────────────────────────────────────────
    if "benchmarkProfileDigest" not in spike_receipt:
        raise ValueError(
            "BENCHMARK_PROFILE_DIGEST_MISMATCH: spike receipt 缺少 'benchmarkProfileDigest' 字段，"
            "无法证明 receipt 绑定的是真实冻结 benchmark profile 文件摘要（P0-3 fail closed）"
        )
    expected_bp = _sha256_file(benchmark_profile_path)
    actual_bp = spike_receipt["benchmarkProfileDigest"]
    if actual_bp != expected_bp:
        raise ValueError(
            f"BENCHMARK_PROFILE_DIGEST_MISMATCH: spike receipt['benchmarkProfileDigest']={actual_bp} "
            f"与冻结文件 {benchmark_profile_path} 实际摘要 {expected_bp} 不一致 "
            "—— bench 端必须用 _sha256_file 写入真实冻结 profile 摘要，不再用内部小结字典摘要"
        )

    # ── (2) env_digest ────────────────────────────────────────────────────────
    if "env_digest" not in spike_receipt:
        raise ValueError(
            "ENV_DIGEST_MISMATCH: spike receipt 缺少 'env_digest' 字段（P0-3 fail closed）"
        )
    if "environment" not in spike_receipt:
        raise ValueError(
            "ENV_DIGEST_MISMATCH: spike receipt 缺少 'environment' 字段，无法重算 env_digest"
        )
    expected_env = _sha256_dict(spike_receipt["environment"])
    actual_env = spike_receipt["env_digest"]
    if actual_env != expected_env:
        raise ValueError(
            f"ENV_DIGEST_MISMATCH: spike receipt['env_digest']={actual_env} "
            f"与 sha256_dict(receipt['environment']) 重算值 {expected_env} 不一致"
        )

    # ── (3) parameterTupleDigest（兼容旧字段名 param_digest）──────────────────
    declared = spike_receipt.get("parameterTupleDigest") or spike_receipt.get("param_digest")
    if not declared:
        raise ValueError(
            "PARAM_TUPLE_MISMATCH: spike receipt 既无 'parameterTupleDigest' 也无 "
            "'param_digest' 字段（P0-3 fail closed）"
        )
    spike_params = spike_receipt.get("eventBatchParameters")
    if spike_params is None:
        raise ValueError(
            "PARAM_TUPLE_MISMATCH: spike receipt 缺少 'eventBatchParameters'，无法重算 parameterTupleDigest"
        )
    try:
        recomputed = compute_parameter_tuple_digest(
            max_batch_events=spike_params["maxBatchEvents"],
            max_batch_bytes=spike_params["maxBatchBytes"],
            max_batch_age_ms=spike_params["maxBatchAgeMs"],
            synchronous=spike_params["synchronous"],
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(
            f"PARAM_TUPLE_MISMATCH: spike receipt eventBatchParameters 不合法或缺键: {exc}"
        ) from exc
    # P0-3 修复：去掉 `declared is not None` 软守卫，PASS 必须等于。
    if declared != recomputed:
        raise ValueError(
            f"PARAM_TUPLE_MISMATCH: spike receipt 自报 parameterTupleDigest/param_digest={declared} "
            f"与 emitter 重算值 {recomputed} 不一致"
        )


def compute_parameter_tuple_digest(
    max_batch_events: int,
    max_batch_bytes: int,
    max_batch_age_ms: int,
    synchronous: str,
) -> str:
    """
    计算 parameterTupleDigest。

    输入：maxBatchEvents、maxBatchBytes、maxBatchAgeMs、synchronous
    算法：JCS-like 按 key 排序 JSON 序列化后 SHA-256
    """
    if synchronous != "FULL":
        raise ValueError(
            f"SYNCHRONOUS_NOT_FULL: synchronous 必须为 'FULL'，实际为 '{synchronous}'"
        )
    tuple_dict = {
        "maxBatchAgeMs": max_batch_age_ms,
        "maxBatchBytes": max_batch_bytes,
        "maxBatchEvents": max_batch_events,
        "synchronous": synchronous,
    }
    return _sha256_dict(tuple_dict)


# ─── 核心发射函数 ─────────────────────────────────────────────────────────────


def emit_manifest(
    spike_receipt_path: Path,
    output_path: Path | None = None,
    benchmark_profile_path: Path = DEFAULT_BENCHMARK_PROFILE_PATH,
    manifest_schema_path: Path = DEFAULT_MANIFEST_SCHEMA_PATH,
    stage_cap_map_path: Path = DEFAULT_STAGE_CAP_MAP_PATH,
    node_cap_map_path: Path = DEFAULT_NODE_CAP_MAP_PATH,
) -> dict[str, Any]:
    """
    从已验证的 SQLite spike receipt 发射 Compatibility Manifest。

    fail-closed 规则：
    - spike_receipt_path 必须存在
    - spike receipt status 必须为 PASS
    - spike receipt 必须含 env_digest、param_digest
    - benchmark profile 文件必须存在
    - manifest schema 文件必须存在
    - synchronous 必须为 FULL

    返回发射的 manifest 字典。若 output_path 非 None，同时写入文件。
    """
    # ── 1. 读取并验证 SQLite spike receipt ─────────────────────────────────────
    if not spike_receipt_path.exists():
        raise FileNotFoundError(
            f"SPIKE_RECEIPT_MISSING: SQLite spike receipt 不存在: {spike_receipt_path}"
        )

    with spike_receipt_path.open(encoding="utf-8") as f:
        spike_receipt: dict[str, Any] = json.load(f)

    # 检查 spike 状态
    if spike_receipt.get("status") != "PASS":
        raise ValueError(
            f"SPIKE_NOT_PASS: SQLite spike receipt status='{spike_receipt.get('status')}' 不是 PASS"
        )

    # 检查必需字段
    for required_field in ("env_digest", "param_digest"):
        if not spike_receipt.get(required_field):
            raise ValueError(
                f"SPIKE_MISSING_FIELD: spike receipt 缺少必需字段 '{required_field}'"
            )

    # ── 2. 读取 benchmark profile ────────────────────────────────────────────
    if not benchmark_profile_path.exists():
        raise FileNotFoundError(
            f"BENCHMARK_PROFILE_MISSING: benchmark profile 不存在: {benchmark_profile_path}"
        )
    benchmark_profile_digest = _sha256_file(benchmark_profile_path)

    # ── 3. 绑定 eventBatchParameters：优先读 spike 实测 tuple，重算 digest 核对 ──
    # 真实 SQLite spike receipt 携带 eventBatchParameters（实际施加并认证的参数）与
    # parameterTupleDigest；emitter 必须读取实测值、重算 digest 并核对，任一不符即
    # fail closed（PARAM_TUPLE_MISMATCH）——这才是「manifest 绑定 SQLite 实测参数」。
    # 仅当 receipt 未携带该 tuple 时（合成回执）才回退到 §11 默认值。
    spike_params = spike_receipt.get("eventBatchParameters")
    if spike_params is not None:
        for key in ("maxBatchEvents", "maxBatchBytes", "maxBatchAgeMs", "synchronous"):
            if key not in spike_params:
                raise ValueError(
                    f"SPIKE_PARAM_MISSING: spike receipt eventBatchParameters 缺少 '{key}'"
                )
        max_batch_events = spike_params["maxBatchEvents"]
        max_batch_bytes = spike_params["maxBatchBytes"]
        max_batch_age_ms = spike_params["maxBatchAgeMs"]
        synchronous = spike_params["synchronous"]
        parameter_tuple_digest = compute_parameter_tuple_digest(
            max_batch_events=max_batch_events,
            max_batch_bytes=max_batch_bytes,
            max_batch_age_ms=max_batch_age_ms,
            synchronous=synchronous,
        )
        # 注意：详细的参数 digest 绑定校验（含 PASS 必须等于）已下沉到
        # `_verify_receipt_digests`，确保合成回执走的是一致错误码前缀。
    else:
        # 回退：合成回执无实测 tuple，用 §11 默认值（单元测试路径）。
        max_batch_events = DEFAULT_MAX_BATCH_EVENTS
        max_batch_bytes = DEFAULT_MAX_BATCH_BYTES
        max_batch_age_ms = DEFAULT_MAX_BATCH_AGE_MS
        synchronous = SYNCHRONOUS_MODE
        parameter_tuple_digest = compute_parameter_tuple_digest(
            max_batch_events=max_batch_events,
            max_batch_bytes=max_batch_bytes,
            max_batch_age_ms=max_batch_age_ms,
            synchronous=synchronous,
        )

    # ── 3.5 P0-3 强制绑定：在 §11 与 §5 之间收紧 receipt digest 与冻结契约的同步 ──
    # - benchmarkProfileDigest 必须等于冻结 profile 文件 SHA-256
    # - env_digest 必须等于 sha256_dict(receipt.environment)
    # - parameterTupleDigest 必须等于 emitter 自重算值（去掉软守卫）
    # 任一缺失或不匹配均 fail closed，错误码前缀便于测试 `match`。
    _verify_receipt_digests(spike_receipt, benchmark_profile_path)

    # ── 4. 计算 spike receipt 摘要 ────────────────────────────────────────────
    sqlite_spike_receipt_digest = _sha256_dict(spike_receipt)

    # ── 5. 读取 schema/policy 摘要 ────────────────────────────────────────────
    if not manifest_schema_path.exists():
        raise FileNotFoundError(
            f"MANIFEST_SCHEMA_MISSING: manifest schema 不存在: {manifest_schema_path}"
        )
    manifest_schema_digest = _sha256_file(manifest_schema_path)

    stage_cap_digest = (
        _sha256_file(stage_cap_map_path)
        if stage_cap_map_path.exists()
        else "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    )
    node_cap_digest = (
        _sha256_file(node_cap_map_path)
        if node_cap_map_path.exists()
        else "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    )

    # ── 6. 组装 manifest ──────────────────────────────────────────────────────
    # 使用步骤 3 解析出的实测（或回退默认）参数，而非硬编码常量——确保 manifest
    # 绑定的正是 SQLite spike 实际认证的 eventBatchParameters。
    event_batch_parameters: dict[str, Any] = {
        "maxBatchEvents": max_batch_events,
        "maxBatchBytes": max_batch_bytes,
        "maxBatchAgeMs": max_batch_age_ms,
        "synchronous": synchronous,
        "parameterTupleDigest": parameter_tuple_digest,
        "sqliteSpikeReceiptDigest": sqlite_spike_receipt_digest,
    }

    manifest: dict[str, Any] = {
        "manifestId": str(uuid.uuid4()),
        "manifestSchemaDigest": manifest_schema_digest,
        "eventBatchParameters": event_batch_parameters,
        "benchmarkProfileDigest": benchmark_profile_digest,
        "sqliteSpikeReceiptDigest": sqlite_spike_receipt_digest,
        "stageCapeabilityMapDigest": stage_cap_digest,
        "nodeCapabilityMapDigest": node_cap_digest,
        "environmentDigest": spike_receipt["env_digest"],
        "createdAt": datetime.now(tz=timezone.utc).isoformat(),
    }

    # ── 7. 可选写入文件 ────────────────────────────────────────────────────────
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

    return manifest


# ─── CLI 入口 ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口，返回退出码 0=成功，1=失败。"""
    parser = argparse.ArgumentParser(
        prog="python tools/compat-probes/emit_manifest.py",
        description="从已验证 SQLite spike receipt 发射 Compatibility Manifest（Phase 0 开发用）",
    )
    parser.add_argument(
        "--spike-receipt",
        type=Path,
        required=True,
        help="SQLite spike receipt JSON 路径（必须 status=PASS）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出 manifest 文件路径（可选）",
    )
    parser.add_argument(
        "--benchmark-profile",
        type=Path,
        default=DEFAULT_BENCHMARK_PROFILE_PATH,
        help=f"benchmark profile 路径（默认 {DEFAULT_BENCHMARK_PROFILE_PATH}）",
    )

    args = parser.parse_args(argv)

    try:
        manifest = emit_manifest(
            spike_receipt_path=args.spike_receipt,
            output_path=args.output,
            benchmark_profile_path=args.benchmark_profile,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
