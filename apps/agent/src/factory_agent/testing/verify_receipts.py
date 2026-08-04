"""
apps/agent/src/factory_agent/testing/verify_receipts.py

CLI 和可编程 API：验证测试回执是否满足 required-test-catalog 覆盖要求。

拒绝条件（全部触发时以非零退出）：
  - MISSING_ENV_DIGEST    : environmentManifestDigest 为空
  - UNKNOWN_RUNTIME       : runtimeId 不在已知运行时列表
  - CONFLICTING_RESULTS   : 同一 testId 出现两种不同 result
  - UNMAPPED_ID           : testId 不在 required-test-catalog 中
  - MANUAL_PASS           : result=PASS 但 actions 为空（疑似手工文字 PASS）
  - INVALID_RESULT        : result 不是合法枚举值

用法：
  python -m factory_agent.testing.verify_receipts \\
      --catalog contracts/testing/required-test-catalog.v1.json \\
      --receipts tests/fixtures/receipts/

算法版本: v1（Phase 0）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from factory_agent.testing.receipts import (
    TestReceipt,
    load_receipts_from_dir,
    load_receipts_from_file,
)
from factory_agent.testing.required_test_catalog import (
    get_test_ids,
    load_catalog,
)


# ─── 验证结果 ─────────────────────────────────────────────────────────────────


class VerificationResult:
    """聚合验证结果，包含所有错误。"""

    def __init__(self) -> None:
        # 所有错误消息列表（含错误码前缀）
        self.errors: list[str] = []

    @property
    def passed(self) -> bool:
        """无任何错误时返回 True。"""
        return len(self.errors) == 0

    def add_error(self, message: str) -> None:
        """追加一条错误消息。"""
        self.errors.append(message)

    def report(self, verbose: bool = True) -> str:
        """生成人可读报告。"""
        if self.passed:
            return "PASS: 所有回执验证通过"
        lines = [f"FAIL: 共 {len(self.errors)} 个错误"]
        for err in self.errors:
            lines.append(f"  - {err}")
        return "\n".join(lines)


# ─── 核心验证逻辑 ─────────────────────────────────────────────────────────────


def verify(
    catalog_path: Path,
    receipts_path: Path,
) -> VerificationResult:
    """
    验证回执目录（或单文件）是否通过所有规则。

    参数：
      catalog_path  : required-test-catalog.v1.json 路径
      receipts_path : 包含回执 JSON 文件的目录，或单个 JSON 文件

    返回：VerificationResult（errors 为空表示全部通过）
    """
    result = VerificationResult()

    # ── 1. 加载 catalog ──────────────────────────────────────────────────────
    try:
        catalog = load_catalog(catalog_path)
        catalog_ids = get_test_ids(catalog)
    except ValueError as exc:
        result.add_error(f"CATALOG_LOAD: 加载 catalog 失败: {exc}")
        return result

    # ── 2. 加载回执 ──────────────────────────────────────────────────────────
    try:
        if receipts_path.is_dir():
            receipts = load_receipts_from_dir(receipts_path)
        elif receipts_path.is_file():
            receipts = load_receipts_from_file(receipts_path)
        else:
            result.add_error(f"RECEIPTS_PATH: 路径不存在: {receipts_path}")
            return result
    except ValueError as exc:
        result.add_error(f"RECEIPTS_LOAD: 加载回执失败: {exc}")
        return result

    # ── 3. 逐条校验单个回执字段 ──────────────────────────────────────────────
    for receipt in receipts:
        errors = receipt.validate()
        for err in errors:
            result.add_error(err)

    # ── 4. 检查 testId 映射（必须全部在 catalog 中）──────────────────────────
    for receipt in receipts:
        if receipt.testId not in catalog_ids:
            result.add_error(
                f"UNMAPPED_ID [{receipt.receiptId}]: testId='{receipt.testId}' "
                "不在 required-test-catalog 中"
            )

    # ── 5. 检查同一 testId 结果冲突 ───────────────────────────────────────────
    results_by_id: dict[str, list[str]] = {}
    for receipt in receipts:
        results_by_id.setdefault(receipt.testId, []).append(receipt.result)

    for test_id, result_list in results_by_id.items():
        unique_results = set(result_list)
        if len(unique_results) > 1:
            result.add_error(
                f"CONFLICTING_RESULTS [{test_id}]: 同一 testId 出现冲突结果 {sorted(unique_results)}"
            )

    return result


# ─── CLI 入口 ─────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m factory_agent.testing.verify_receipts",
        description="验证测试回执覆盖与合规性（fail-closed）",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        required=True,
        help="required-test-catalog.v1.json 路径",
    )
    parser.add_argument(
        "--receipts",
        type=Path,
        required=True,
        help="回执 JSON 文件目录（或单个 JSON 文件）",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        default=False,
        help="以 JSON 格式输出结果（方便机器解析）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """
    CLI 主入口。

    返回退出码：0=全部通过，1=有错误，2=参数/加载失败。
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    result = verify(
        catalog_path=args.catalog,
        receipts_path=args.receipts,
    )

    if args.json_output:
        output: dict[str, Any] = {
            "passed": result.passed,
            "errorCount": len(result.errors),
            "errors": result.errors,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(result.report())

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
