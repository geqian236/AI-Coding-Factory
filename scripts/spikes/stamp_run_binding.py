"""
scripts/spikes/stamp_run_binding.py

GPT 第六轮 item 2(d)：把本轮运行身份（runId / runNonce / candidateSha）绑定进
spike receipt，作为可审计的 provenance。

为什么用 Python 而不是 PowerShell 盖章：
  PowerShell 5.1 的 ConvertFrom-Json | ConvertTo-Json 往返会把「单元素数组」拆成
  单对象（assertions 只有 1 条时 receipt 结构漂移），且默认深度会把嵌套断言字符串
  化。用 Python 的 json 模块 load→改→dump 可保真保结构，只新增一个顶层
  runBinding 键，不触碰 environment / eventBatchParameters 等被下游 emit_manifest
  重算摘要的子对象。

为什么只盖六份非 sqlite receipt：
  sqlite receipt 会喂给 emit_manifest._verify_receipt_digests 重算
  sha256_dict(environment) 与 parameterTupleDigest；任何往返/改写都有破坏 digest
  绑定的风险。故 sqlite 不盖 runBinding，改由 wrapper 复用 runner 通过
  $env:SPIKE_RUN_NONCE 传入的 nonce 写进 receipt.run_nonce 来绑定本轮（校验器
  Test-SpikeReceiptEvidence 对 sqlite 走 run_nonce 分支）。

用法：
  python stamp_run_binding.py <receipt_path> <spike_name> [--probe-digest sha256:<64-hex>]
  从环境变量 SPIKE_RUN_ID / SPIKE_RUN_NONCE / SPIKE_CANDIDATE_SHA 读取运行身份。
  对需要绑定实际二进制的 non-sqlite spike（当前为 self-hosted runner_identity），调用方
  额外传入已执行 probe 的 SHA-256；缺失或格式错误不能被静默写成空摘要。
  receipt 不存在或非法 JSON 时以非零退出（fail-closed），调用方据此判失败。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def main() -> int:
    """给指定 receipt 追加顶层 runBinding；可选绑定真实 probe，异常一律非零。"""
    if len(sys.argv) not in (3, 5):
        print(
            "usage: stamp_run_binding.py <receipt_path> <spike_name> "
            "[--probe-digest sha256:<64-hex>]",
            file=sys.stderr,
        )
        return 2
    receipt_path = Path(sys.argv[1])
    spike_name = sys.argv[2]
    probe_digest = ""
    if len(sys.argv) == 5:
        if sys.argv[3] != "--probe-digest":
            print("STAMP_USAGE: expected --probe-digest", file=sys.stderr)
            return 2
        probe_digest = sys.argv[4]
        # 真实二进制摘要必须使用固定的小写 sha256 格式；接受任意字符串会让全零/伪造
        # 值混入 receipt，后续 validator 无法机械区分有效身份绑定与占位符。
        if re.fullmatch(r"sha256:[0-9a-f]{64}", probe_digest) is None:
            print("STAMP_INVALID_PROBE_DIGEST: expected sha256:<64-lowercase-hex>", file=sys.stderr)
            return 7

    run_id = os.environ.get("SPIKE_RUN_ID", "")
    run_nonce = os.environ.get("SPIKE_RUN_NONCE", "")
    candidate_sha = os.environ.get("SPIKE_CANDIDATE_SHA", "")
    if not run_nonce:
        print("STAMP_NO_NONCE: SPIKE_RUN_NONCE 未设置，拒绝盖章（fail-closed）", file=sys.stderr)
        return 3

    if not receipt_path.exists():
        print(f"STAMP_RECEIPT_MISSING: {receipt_path}", file=sys.stderr)
        return 4
    try:
        # utf-8-sig 读：部分 wrapper（clock_source cargo 路径 / tauri）用 PS 5.1
        # Out-File -Encoding utf8 写盘会带 BOM，普通 utf-8 的 json.load 会报
        # "Unexpected UTF-8 BOM"。utf-8-sig 有 BOM 则剥离、无 BOM 也正常，写回仍无 BOM。
        with receipt_path.open(encoding="utf-8-sig") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"STAMP_UNPARSEABLE: {receipt_path}: {exc}", file=sys.stderr)
        return 5

    if not isinstance(obj, dict):
        print(f"STAMP_NOT_OBJECT: receipt 顶层不是对象: {receipt_path}", file=sys.stderr)
        return 6

    # 仅新增一个顶层键；不触碰任何既有字段（保 digest 绑定不变）。若调用方明确传入
    # probeDigest，则把它与 run/candidate 同层写入，供 validator 按预期真实二进制比对。
    run_binding: dict[str, str] = {
        "runId": run_id,
        "runNonce": run_nonce,
        "candidateSha": candidate_sha,
        "spike": spike_name,
        "stampedBy": "phase0-acceptance-runner",
    }
    if probe_digest:
        run_binding["probeDigest"] = probe_digest
    obj["runBinding"] = run_binding

    # 无 BOM UTF-8 写回：下游 Python json.load / PS -Encoding utf8 均要求无 BOM。
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    receipt_path.write_text(text, encoding="utf-8")
    probe_suffix = f" probeDigest={probe_digest[:15]}..." if probe_digest else ""
    print(
        f"STAMP_OK: {spike_name} <- runNonce={run_nonce[:8]}... "
        f"candidateSha={candidate_sha[:8]}...{probe_suffix}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
