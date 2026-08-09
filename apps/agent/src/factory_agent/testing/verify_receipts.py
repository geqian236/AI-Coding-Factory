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
  - EMPTY_EXPECTED        : result=PASS 但 expected 为空（P0-6）
  - EMPTY_ACTUAL          : result=PASS 但 actual 为空（P0-6）
  - MISSING_OWNER         : finalPassOwner 缺失（P0-6）
  - MISSING_QUALIFICATION : qualification 缺失（P0-6）
  - MISSING_DIGEST        : scenarioContractDigest 缺失或与 catalog 不一致（P0-6）
  - MISSING_REPLAYS       : requiredReplays 缺失或 <1（P0-6）
  - OWNER_MISMATCH        : receipt.finalPassOwner 与 catalog 不一致（P0-6）
  - UNAUTHORIZED_OWNER    : finalPassOwner 不在授权白名单（P0-6，初版 codex-reviewer）
  - DUPLICATE_RECEIPT_ID  : receiptId 全局重复（P0-6）
  - INSUFFICIENT_REPLAYS  : PASS 回执去重数 < catalog.requiredReplays（P0-6）

用法：
  python -m factory_agent.testing.verify_receipts \\
      --catalog contracts/testing/required-test-catalog.v1.json \\
      --receipts tests/fixtures/receipts/

算法版本: v1（Phase 0）
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any

from factory_agent.testing.receipts import (
    load_receipts_from_dir,
    load_receipts_from_file,
)
from factory_agent.testing.required_test_catalog import (
    digest_by_test_id,
    get_test_ids,
    load_catalog,
    owner_by_test_id,
    replays_by_test_id,
)

# ─── 授权白名单（初版） ──────────────────────────────────────────────────────
# P0-6：只接受已授权的 finalPassOwner；新增角色须同步更新并通过 recon 审核。
# GPT 第二轮审核：catalog 中 finalPassOwner 应被限制为 Phase 0–6 阶段允许的
# owner 角色。当前 catalog 全部为 "codex-reviewer"，但聚合门禁需知道完整白名单。
# 实施者（claude / codex）只产生 PARTIAL，不写入 FINAL；Phase 6 收口必须由
# 独立 reviewer 出具 FINAL。
_AUTHORIZED_OWNERS: frozenset[str] = frozenset({
    "codex-reviewer",
    # 实施者（Phase 0 沙盒未启用）：保留扩展点，便于 Phase 1+ 引入
    "claude-implementer",
    "codex-implementer",
    "release-engineer",
})


def _is_utf8_encoding(encoding: object) -> bool:
    """仅接受无 BOM 的 UTF-8 编码标识，避免 CLI 在 ANSI 代码页输出中文证据。"""
    if not isinstance(encoding, str):
        return False
    return encoding.replace("_", "").replace("-", "").casefold() == "utf8"


def _configure_cli_text_stream_utf8(stream: object) -> None:
    """将一个 CLI 文本流固定为严格 UTF-8；未知流形态一律拒绝而非猜测编码。

    回执报告和参数错误均可能包含中文，必须在任何正文输出前完成此配置。若宿主替换了
    `sys.stdout`/`sys.stderr`，仅接受明确的 Unicode StringIO 或已标记 UTF-8 的流，避免
    cp1252 等代码页写出半份回执后才失败。普通流还必须回读 `errors == "strict"`；所有
    属性读取、重配置和状态回读异常都归一化为稳定分类，不能把宿主异常正文带到 CLI。
    """
    try:
        # StringIO 只保存 Unicode 字符串，不经过外部编码器，是唯一可安全省略状态属性的流。
        if isinstance(stream, io.StringIO):
            return

        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")

        # 即使 reconfigure 未抛错，也必须回读两项状态，拒绝 replace/ignore 等伪成功实现。
        encoding = getattr(stream, "encoding", None)
        errors = getattr(stream, "errors", None)
        if _is_utf8_encoding(encoding) and isinstance(errors, str) and errors == "strict":
            return
    except Exception as exc:  # noqa: BLE001 - 失败分类不能泄露宿主异常正文。
        raise RuntimeError("CLI_TEXT_UTF8_CONFIGURATION_FAILED") from exc

    raise RuntimeError("CLI_TEXT_UTF8_CONFIGURATION_FAILED")


def _configure_cli_text_output_utf8() -> None:
    """在解析参数和输出回执前同时固定 stdout/stderr，保证两条证据面编码一致。"""
    _configure_cli_text_stream_utf8(sys.stdout)
    _configure_cli_text_stream_utf8(sys.stderr)


def _emit_cli_text_encoding_failure() -> bool:
    """尝试输出稳定 ASCII 失败分类；返回值显式记录 stderr 是否仍可写。"""
    try:
        sys.stderr.write("CLI_TEXT_UTF8_CONFIGURATION_FAILED\n")
        sys.stderr.flush()
        return True
    except Exception:  # noqa: BLE001 - 失败路径不允许再以 traceback 泄露详情。
        return False


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
    require_coverage: bool = False,
) -> VerificationResult:
    """
    验证回执目录（或单文件）是否通过所有规则。

    参数：
      catalog_path     : required-test-catalog.v1.json 路径
      receipts_path    : 包含回执 JSON 文件的目录，或单个 JSON 文件
      require_coverage : True 时启用覆盖门禁——required-test-catalog 中每个
                         testId 都必须有一条 result=PASS 的回执，否则报
                         MISSING_COVERAGE。默认 False 保持逐回执校验模式
                         （单元测试与增量 fixture 用），Phase 0 总门禁显式传 True。

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

    # ── 4. 检查 receiptId 全局唯一（P0-6，杜绝同一回执伪装多个 testId 通过）──
    seen_receipt_ids: dict[str, str] = {}  # receiptId -> first testId
    for receipt in receipts:
        rid = receipt.receiptId
        if not rid:
            continue  # schema 层会报 MISSING_FIELD
        if rid in seen_receipt_ids:
            result.add_error(
                f"DUPLICATE_RECEIPT_ID [{rid}]: receiptId 在多个回执中出现"
                f"（testId={seen_receipt_ids[rid]} / {receipt.testId}）"
            )
        else:
            seen_receipt_ids[rid] = receipt.testId

    # ── 5. 检查 testId 映射（必须全部在 catalog 中）──────────────────────────
    for receipt in receipts:
        if receipt.testId not in catalog_ids:
            result.add_error(
                f"UNMAPPED_ID [{receipt.receiptId}]: testId='{receipt.testId}' "
                "不在 required-test-catalog 中"
            )

    # ── 6. 检查同一 testId 结果冲突 ───────────────────────────────────────────
    results_by_id: dict[str, list[str]] = {}
    for receipt in receipts:
        results_by_id.setdefault(receipt.testId, []).append(receipt.result)

    for test_id, result_list in results_by_id.items():
        unique_results = set(result_list)
        if len(unique_results) > 1:
            result.add_error(
                f"CONFLICTING_RESULTS [{test_id}]: 同一 testId 出现冲突结果 {sorted(unique_results)}"
            )

    # ── 7. scenarioContractDigest 绑定校验 ────────────────────────────────────
    # 回执若携带非空 scenarioContractDigest，必须与 catalog 中该 testId 的冻结值一致；
    # 场景合同漂移（catalog 侧改动）或回执引用了旧合同都会失配，fail closed。
    # P0-6：PASS 时 declared None 强制报 MISSING_DIGEST（保留 FAIL/BLOCKED 宽松）。
    catalog_digests = digest_by_test_id(catalog)
    for receipt in receipts:
        declared = receipt.scenarioContractDigest
        if declared is None:
            # P0-6：PASS 时缺 digest 视为伪造 PASS，强制报错。
            # FAIL/BLOCKED 保持宽松——这些状态不会进入覆盖门禁，宽松便于排错。
            if receipt.result == "PASS":
                result.add_error(
                    f"MISSING_DIGEST [{receipt.receiptId}]: result=PASS 但 "
                    "scenarioContractDigest 缺失，无法绑定场景合同"
                )
            continue
        expected = catalog_digests.get(receipt.testId)
        if expected is not None and declared != expected:
            result.add_error(
                f"DIGEST_MISMATCH [{receipt.receiptId}]: testId='{receipt.testId}' "
                f"的 scenarioContractDigest 与 catalog 冻结值不一致"
                f"（回执={declared} / catalog={expected}）"
            )

    # ── 8. finalPassOwner 绑定 + 白名单（P0-6）────────────────────────────────
    catalog_owners = owner_by_test_id(catalog)
    for receipt in receipts:
        owner = receipt.finalPassOwner
        if not owner:
            # receipts.validate() 已报 MISSING_OWNER；这里只补充跨字段校验
            continue
        # 白名单：owner 必须在授权集合内
        if owner not in _AUTHORIZED_OWNERS:
            result.add_error(
                f"UNAUTHORIZED_OWNER [{receipt.receiptId}]: finalPassOwner='{owner}' "
                f"不在授权白名单 {sorted(_AUTHORIZED_OWNERS)}"
            )
            continue
        # GPT 第二轮：qualification 限定为 PARTIAL/FINAL；result=PASS 必须
        # qualification=FINAL（PARTIAL 用于阶段通过但不能冒充最终收口）。
        qualification = getattr(receipt, "qualification", None)
        if receipt.result == "PASS" and qualification != "FINAL":
            result.add_error(
                f"PARTIAL_NOT_FINAL [{receipt.receiptId}]: result=PASS 但 "
                f"qualification='{qualification}'（必须 FINAL 才能用于收口）"
            )
        # 绑定：owner 必须与 catalog 冻结值一致
        expected_owner = catalog_owners.get(receipt.testId)
        if expected_owner is not None and owner != expected_owner:
            result.add_error(
                f"OWNER_MISMATCH [{receipt.receiptId}]: testId='{receipt.testId}' "
                f"的 finalPassOwner 与 catalog 冻结值不一致"
                f"（回执={owner} / catalog={expected_owner}）"
            )

    # ── 9. 覆盖门禁（仅在 require_coverage=True 时）────────────────────────────
    # Phase 0 总门禁要求：47 个 required testId 每个都必须至少有 catalog.requiredReplays
    # 条 result=PASS 且校验通过的去重 receiptId。
    if require_coverage:
        # 收集 PASS 且无任何校验错误的回执（去重 receiptId）
        error_receipt_ids: set[str] = set()
        for err in result.errors:
            # 错误消息形如 "ERROR_CODE [receiptId]: ..."，提取 receiptId
            if "[" in err and "]" in err:
                try:
                    rid = err.split("[", 1)[1].split("]", 1)[0]
                    error_receipt_ids.add(rid)
                except IndexError:
                    pass

        valid_passes_by_id: dict[str, set[str]] = {}
        for receipt in receipts:
            if receipt.result != "PASS":
                continue
            if receipt.receiptId in error_receipt_ids:
                continue
            valid_passes_by_id.setdefault(receipt.testId, set()).add(receipt.receiptId)

        catalog_replays = replays_by_test_id(catalog)

        # 9a. 每个 testId 必须有 result=PASS 的回执
        passing_ids = set(valid_passes_by_id.keys())
        missing = sorted(catalog_ids - passing_ids)
        if missing:
            result.add_error(
                f"MISSING_COVERAGE: {len(missing)}/{len(catalog_ids)} 个必需 testId "
                f"缺少 result=PASS 的回执: {missing}"
            )

        # 9b. 每个 testId 的 PASS 去重 receiptId 数必须 >= catalog.requiredReplays
        for test_id, required in catalog_replays.items():
            actual_count = len(valid_passes_by_id.get(test_id, set()))
            if actual_count < required:
                result.add_error(
                    f"INSUFFICIENT_REPLAYS [{test_id}]: PASS 回执去重 receiptId 数 "
                    f"{actual_count} < catalog.requiredReplays={required}"
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
    parser.add_argument(
        "--require-coverage",
        action="store_true",
        default=False,
        help="要求 catalog 全部 47 个 testId 均有 PASS 回执；缺任一即 fail-closed（Phase 0 总门禁用）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """
    CLI 主入口。

    返回退出码：0=全部通过，1=有错误，2=参数/加载失败。
    """
    try:
        # argparse/help、JSON 和人可读报告均可能写中文；先固定两条文本输出通道。
        _configure_cli_text_output_utf8()
    except RuntimeError:
        _emit_cli_text_encoding_failure()
        return 2

    parser = _build_parser()
    args = parser.parse_args(argv)

    result = verify(
        catalog_path=args.catalog,
        receipts_path=args.receipts,
        require_coverage=args.require_coverage,
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
