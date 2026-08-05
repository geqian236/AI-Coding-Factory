"""
apps/agent/src/factory_agent/testing/required_test_catalog.py

加载并验证 contracts/testing/required-test-catalog.v1.json。
严格检查：精确 47 个唯一测试 ID，不接受多或少。

算法版本: v1（Phase 0）
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from factory_agent.policy.canonical_json import canonicalize

# ─── 常量 ────────────────────────────────────────────────────────────────────

# scenarioContractDigest 摘要输出前缀。
_SHA256_PREFIX = "sha256:"

# 计算 scenarioContractDigest 时必须排除的字段（自身摘要值，避免自指循环）。
_DIGEST_EXCLUDED_FIELDS = frozenset({"scenarioContractDigest"})

# 相对于本文件向上 5 层到达 repo root
_REPO_ROOT = Path(__file__).resolve().parents[5]

# 默认 catalog 路径
DEFAULT_CATALOG_PATH: Path = (
    _REPO_ROOT / "contracts" / "testing" / "required-test-catalog.v1.json"
)

# Master Spec §20.2 冻结的 47 个测试 ID
REQUIRED_TEST_IDS: frozenset[str] = frozenset({
    "STAGE-001", "STAGE-002", "STAGE-003", "STAGE-004", "STAGE-005", "STAGE-006",
    "AUTH-001", "AUTH-002",
    "PLAN-001", "PLAN-HASH-001",
    "BOOT-001", "MCP-001",
    "STATE-001", "LEASE-001", "PROC-001",
    "CTRL-001", "CTRL-002",
    "EVENT-HASH-001",
    "STREAM-001", "STREAM-002", "STREAM-DUR-001", "STREAM-BP-001",
    "STREAM-PERF-BURST", "STREAM-PERF-SUSTAINED", "STREAM-PERF-SPARSE",
    "REVIEW-001", "GIT-001", "SIDEFX-001",
    "DEPLOY-001", "DEPLOY-002", "DEPLOY-003",
    "GUARD-001", "DEPLOY-FENCE-001",
    "STORE-001", "BUDGET-001", "RETRY-001", "NOTIFY-001",
    "PATH-001", "PATH-002", "CLI-PROFILE-001",
    "RUNTIME-001", "WSL-IO-001", "NGINX-001", "BACKUP-001",
    "DEPLOY-RAM-001", "SCHED-PERF-001", "COMPAT-001",
})

# 精确要求的 ID 数量
REQUIRED_ID_COUNT = 47


# ─── 加载函数 ─────────────────────────────────────────────────────────────────


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    """
    加载并验证 required-test-catalog.v1.json。

    验证规则：
    - 文件必须存在且是合法 JSON
    - tests 数组必须精确包含 47 个条目
    - 所有 testId 必须唯一
    - testId 集合必须与 REQUIRED_TEST_IDS 完全匹配
    - 每条目必须含 implementationContributors/finalPassOwner/requiredReplays/scenarioContractDigest

    返回加载的 catalog 字典；任何违规直接抛出 ValueError。
    """
    catalog_path = path or DEFAULT_CATALOG_PATH

    # 读取文件
    if not catalog_path.exists():
        raise ValueError(f"required-test-catalog 不存在: {catalog_path}")

    with catalog_path.open(encoding="utf-8") as f:
        try:
            catalog = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"required-test-catalog JSON 解析失败: {exc}") from exc

    # 基础结构检查
    if "tests" not in catalog:
        raise ValueError("required-test-catalog 缺少 'tests' 字段")

    tests: list[dict[str, Any]] = catalog["tests"]
    if not isinstance(tests, list):
        raise ValueError("required-test-catalog 'tests' 必须是数组")

    # 精确 47 个条目
    if len(tests) != REQUIRED_ID_COUNT:
        raise ValueError(
            f"required-test-catalog 必须有精确 {REQUIRED_ID_COUNT} 个测试条目，"
            f"实际为 {len(tests)}"
        )

    # 收集 testId
    ids: list[str] = []
    for i, entry in enumerate(tests):
        if not isinstance(entry, dict):
            raise ValueError(f"测试条目 [{i}] 不是对象")
        test_id = entry.get("testId")
        if not test_id or not isinstance(test_id, str):
            raise ValueError(f"测试条目 [{i}] 缺少有效 testId")
        ids.append(test_id)

    # 唯一性检查
    if len(ids) != len(set(ids)):
        duplicates = [tid for tid in ids if ids.count(tid) > 1]
        raise ValueError(f"required-test-catalog 存在重复 testId: {sorted(set(duplicates))}")

    # 集合精确匹配
    actual_set = frozenset(ids)
    extra = actual_set - REQUIRED_TEST_IDS
    missing = REQUIRED_TEST_IDS - actual_set
    if extra or missing:
        parts: list[str] = []
        if extra:
            parts.append(f"多出: {sorted(extra)}")
        if missing:
            parts.append(f"缺少: {sorted(missing)}")
        raise ValueError("required-test-catalog testId 集合不匹配。" + "；".join(parts))

    # 每条目必需字段检查
    required_fields = {"implementationContributors", "finalPassOwner", "requiredReplays", "scenarioContractDigest"}
    for entry in tests:
        test_id = entry["testId"]
        missing_fields = required_fields - set(entry.keys())
        if missing_fields:
            raise ValueError(
                f"测试条目 [{test_id}] 缺少必需字段: {sorted(missing_fields)}"
            )

    return catalog  # type: ignore[no-any-return]


def get_test_ids(catalog: dict[str, Any] | None = None) -> frozenset[str]:
    """
    返回 catalog 中的 testId 集合。

    若未传入已加载的 catalog，则从默认路径加载。
    """
    if catalog is None:
        catalog = load_catalog()
    return frozenset(entry["testId"] for entry in catalog["tests"])


def get_test_entry(test_id: str, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    按 testId 查找 catalog 条目。

    若未找到则抛出 KeyError。
    """
    if catalog is None:
        catalog = load_catalog()
    for entry in catalog["tests"]:
        if entry["testId"] == test_id:
            return entry  # type: ignore[no-any-return]
    raise KeyError(f"testId '{test_id}' 不在 required-test-catalog 中")


def scenario_contract_digest(entry: dict[str, Any]) -> str:
    """计算某测试条目的 scenarioContractDigest（冻结场景合同身份）。

    对「除 scenarioContractDigest 自身外」的完整条目做与 plan/event hash 同一的
    RFC 8785 canonicalizer（NFC + JCS）后取 SHA-256，前缀 "sha256:"。因此 testId、
    scenario 文本、implementationContributors、finalPassOwner、requiredReplays、
    crashPointIds 中任一漂移都会改变摘要，使旧回执的 scenarioContractDigest 失配。

    Args:
        entry: required-test-catalog 中的单个测试条目。

    Returns:
        形如 "sha256:<64 位小写十六进制>" 的场景合同摘要。

    Raises:
        CanonicalJsonError: 条目含非法数字/类型/重复键。
    """
    material = {k: v for k, v in entry.items() if k not in _DIGEST_EXCLUDED_FIELDS}
    digest = hashlib.sha256(canonicalize(material)).hexdigest()
    return f"{_SHA256_PREFIX}{digest}"


def digest_by_test_id(catalog: dict[str, Any] | None = None) -> dict[str, str]:
    """返回 {testId: 冻结的 scenarioContractDigest} 映射（读取 catalog 中的存储值）。

    供聚合器绑定校验使用：回执携带的 scenarioContractDigest 必须与此映射一致。
    """
    if catalog is None:
        catalog = load_catalog()
    return {
        entry["testId"]: entry["scenarioContractDigest"]
        for entry in catalog["tests"]
    }


def owner_by_test_id(catalog: dict[str, Any] | None = None) -> dict[str, str]:
    """返回 {testId: catalog 冻结的 finalPassOwner} 映射。

    P0-6 fail-closed：聚合器检查 receipt.finalPassOwner 必须等于本映射值
    （OWNER_MISMATCH），并且 owner 必须在白名单 UNAUTHORIZED_OWNER。
    不新增 catalog 字段，只读取已存在的 finalPassOwner，避免 digest 漂移。
    """
    if catalog is None:
        catalog = load_catalog()
    return {
        entry["testId"]: entry["finalPassOwner"]
        for entry in catalog["tests"]
    }


def replays_by_test_id(catalog: dict[str, Any] | None = None) -> dict[str, int]:
    """返回 {testId: catalog 冻结的 requiredReplays} 映射。

    P0-6 fail-closed：聚合器对每个 testId 计数 result=PASS 且校验通过的去重
    receiptId 数，< requiredReplays 报 INSUFFICIENT_REPLAYS。
    不新增 catalog 字段，只读取已存在的 requiredReplays，避免 digest 漂移。
    """
    if catalog is None:
        catalog = load_catalog()
    return {
        entry["testId"]: entry["requiredReplays"]
        for entry in catalog["tests"]
    }
