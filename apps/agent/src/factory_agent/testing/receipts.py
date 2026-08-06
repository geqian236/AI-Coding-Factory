"""
apps/agent/src/factory_agent/testing/receipts.py

机器测试回执数据模型。
遵循 contracts/schemas/test-receipt.v1.schema.json；
禁止通过手工文字将 result 改为 PASS。

P0-6 强化（Phase 0）：expected/actual 必须非空、actions 至少 1 条、
finalPassOwner/requiredReplays/scenarioContractDigest/qualification 必填，
杜绝伪造 FINAL PASS。

算法版本: v1（Phase 0）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ─── 常量 ────────────────────────────────────────────────────────────────────

# 合法的 result 枚举值
VALID_RESULTS = frozenset({"PASS", "FAIL", "BLOCKED_UNCERTIFIED"})

# test-receipt schema 是回执结构的单一真源；validate() 运行时读取其
# required / minItems / minProperties 并无条件强制，杜绝手写 validate 与
# schema 漂移（GPT 第五轮 item 6：schema 无条件要求 actions/expected/actual
# 非空，旧 validate 仅在 result==PASS 时检查，可复现漂移）。
# receipts.py 位于 apps/agent/src/factory_agent/testing/，向上 5 层到 REPO_ROOT。
_SCHEMA_PATH = (
    Path(__file__).resolve().parents[5]
    / "contracts" / "schemas" / "test-receipt.v1.schema.json"
)
_schema_cache: dict[str, Any] | None = None


def _load_receipt_schema() -> dict[str, Any]:
    """加载 test-receipt schema（模块级缓存）；schema 是结构约束的单一真源。"""
    global _schema_cache
    if _schema_cache is None:
        with _SCHEMA_PATH.open(encoding="utf-8") as f:
            _schema_cache = json.load(f)
    return _schema_cache


def schema_validation_errors(instance: dict[str, Any]) -> list[str]:
    """对 instance 执行**全量** JSON Schema 校验，返回错误消息列表（GPT 第六轮 item 3）。

    第六轮 REVISE item 3 指出：旧 validate() 只读 schema 的三个数量约束
    （actions.minItems / expected.minProperties / actual.minProperties），
    并未执行完整 JSON Schema 校验——反例（空 receiptId/testId/actionId/description）
    在 validate() 得 0 错误，但真 JSON Schema 得 4 错误。此函数补上完整校验：

    - **优先**用 importlib 动态加载真 `jsonschema` 库（Draft7Validator），这是权威
      参考实现，零语义漂移。锁定 py312 验收环境与 CI 均安装该库，故 A-1 走真库。
    - 动态 import（非顶层 `import jsonschema`）：避免 `uv run mypy` 在未装该库的
      `.venv` 里因缺 stub 报错而打爆 gate-11，也不需要改 pyproject/uv.lock 联网重锁。
    - 库不可用时**回退**到由 schema 驱动的 stdlib 校验器 `_stdlib_schema_errors`，
      其行为由 test_receipt_schema_conformance 用真 jsonschema 对系统性变异电池
      逐例证明等价（回应「运行时语义一致」要求）。

    返回的错误消息带 SCHEMA 前缀 + JSON 路径，便于聚合层定位。
    """
    schema = _load_receipt_schema()
    try:
        # 动态加载：mypy 不静态解析，缺库环境不报错；装了则走权威实现。
        import importlib

        jsonschema = importlib.import_module("jsonschema")
        validator_cls = jsonschema.Draft7Validator  # type: ignore[attr-defined]
        validator = validator_cls(schema)
        errors: list[str] = []
        for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
            loc = "/".join(str(p) for p in err.path) or "<root>"
            errors.append(f"SCHEMA [{loc}]: {err.message}")
        return errors
    except ModuleNotFoundError:
        # 回退：schema 驱动的 stdlib 校验器（与真库逐例等价，见 conformance 测试）。
        errors = []
        _stdlib_schema_errors(instance, schema, "", errors)
        return errors


def _stdlib_schema_errors(
    instance: Any, schema: dict[str, Any], path: str, errors: list[str]
) -> None:
    """schema 驱动的 stdlib JSON Schema 校验器（Draft-07 子集，覆盖本 schema 全部关键字）。

    仅在真 `jsonschema` 库不可用时作为回退。实现的关键字与 test-receipt.v1.schema
    实际使用的一致：type / enum / minLength / minimum / minItems / minProperties /
    required / additionalProperties / properties / items。递归下降，错误带路径前缀。
    保持与 Draft7Validator 相同语义：不校验 `format`（除非显式传 format_checker），
    可选属性仅在存在时校验。
    """
    def _loc(p: str) -> str:
        return p or "<root>"

    stype = schema.get("type")
    # type 校验（JSON 类型 -> Python 类型；bool 不是 int，与 JSON Schema 一致）。
    if stype is not None:
        type_ok = {
            "object": isinstance(instance, dict),
            "array": isinstance(instance, list),
            "string": isinstance(instance, str),
            "integer": isinstance(instance, int) and not isinstance(instance, bool),
            "number": isinstance(instance, (int, float)) and not isinstance(instance, bool),
            "boolean": isinstance(instance, bool),
            "null": instance is None,
        }.get(stype, True)
        if not type_ok:
            errors.append(f"SCHEMA [{_loc(path)}]: 类型应为 {stype}，实际 {type(instance).__name__}")
            return  # 类型不符时后续关键字无意义

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"SCHEMA [{_loc(path)}]: 值 {instance!r} 不在 enum {schema['enum']}")

    if isinstance(instance, str):
        min_len = schema.get("minLength")
        if min_len is not None and len(instance) < min_len:
            errors.append(f"SCHEMA [{_loc(path)}]: 字符串长度 {len(instance)} < minLength={min_len}（不得为空）")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        if minimum is not None and instance < minimum:
            errors.append(f"SCHEMA [{_loc(path)}]: 数值 {instance} < minimum={minimum}")

    if isinstance(instance, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(instance) < min_items:
            errors.append(f"SCHEMA [{_loc(path)}]: 数组长度 {len(instance)} < minItems={min_items}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(instance):
                _stdlib_schema_errors(item, item_schema, f"{path}/{i}" if path else str(i), errors)

    if isinstance(instance, dict):
        min_props = schema.get("minProperties")
        if min_props is not None and len(instance) < min_props:
            errors.append(f"SCHEMA [{_loc(path)}]: 属性数 {len(instance)} < minProperties={min_props}")
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"SCHEMA [{_loc(path)}]: 缺少必需属性 '{req}'")
        props: dict[str, Any] = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    errors.append(f"SCHEMA [{_loc(path)}]: 不允许的额外属性 '{key}'（additionalProperties:false）")
        for key, sub_schema in props.items():
            if key in instance and isinstance(sub_schema, dict):
                _stdlib_schema_errors(instance[key], sub_schema, f"{path}/{key}" if path else key, errors)

# 已知合法的 runtimeId 标识（未来可扩展）
KNOWN_RUNTIME_IDS = frozenset({
    "python-3.12",
    "python-3.11",
    "rust-1.80",
    "rust-stable",
    "typescript-node-20",
    "typescript-node-22",
    "factory-agent-v0",
})


# ─── 子对象模型 ────────────────────────────────────────────────────────────────


@dataclass
class ReceiptAction:
    """回执动作记录。每条动作必须有唯一 actionId、描述和执行时间。"""

    # 动作唯一 ID
    actionId: str
    # 动作描述（机器生成，不可手工填写"PASS"等结论）
    description: str
    # 动作执行时间（ISO 8601）
    executedAt: str
    # 动作输入摘要（可选）
    inputDigest: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReceiptAction:
        """从字典反序列化动作记录。"""
        return cls(
            actionId=d["actionId"],
            description=d["description"],
            executedAt=d["executedAt"],
            inputDigest=d.get("inputDigest"),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（排除 None 值）。"""
        result: dict[str, Any] = {
            "actionId": self.actionId,
            "description": self.description,
            "executedAt": self.executedAt,
        }
        if self.inputDigest is not None:
            result["inputDigest"] = self.inputDigest
        return result


# ─── 主回执模型 ────────────────────────────────────────────────────────────────


@dataclass
class TestReceipt:
    """
    机器可读测试回执。

    字段来自 contracts/schemas/test-receipt.v1.schema.json。
    result 字段只能由机器根据 actions/assertions 计算得出；
    禁止手工将其改为 "PASS"。
    """

    # 回执唯一 ID
    receiptId: str
    # 测试 ID（来自 required-test-catalog.v1.json）
    testId: str
    # 环境 Compatibility Manifest 摘要（不得为空）
    environmentManifestDigest: str
    # 执行动作列表（机器生成；PASS 结论要求至少一个动作）
    actions: list[ReceiptAction]
    # 期望结果（机器可读对象）
    expected: dict[str, Any]
    # 实际观察结果（机器可读对象）
    actual: dict[str, Any]
    # 外部副作用计数（>=0）
    sideEffectCount: int
    # 相关 Artifact 摘要列表
    artifactDigests: list[str]
    # 测试结论：PASS | FAIL | BLOCKED_UNCERTIFIED
    result: str
    # 回执创建时间（ISO 8601）
    createdAt: str

    # 可选字段
    # 失败原因（result=FAIL 时应填写）
    failureReason: str | None = None
    # 场景合同内容摘要（来自 required-test-catalog）
    scenarioContractDigest: str | None = None
    # 实现贡献者列表
    implementationContributors: list[str] = field(default_factory=list)
    # 最终通过责任人（P0-6：必填）
    finalPassOwner: str | None = None
    # 执行资质标签（P0-6：必填，标识评审/实施/审计等角色资格）
    qualification: str | None = None
    # 要求重放次数（P0-6：必填且 >=1）
    requiredReplays: int | None = None
    # 运行时标识（可选；若存在则必须是已知 runtimeId）
    runtimeId: str | None = None

    # ── 校验 ──────────────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """
        校验回执字段合规性。

        返回错误消息列表；空列表表示合规。
        错误码前缀：
          - MISSING_ENV_DIGEST    : environmentManifestDigest 为空
          - UNKNOWN_RUNTIME       : runtimeId 不在已知列表
          - INVALID_RESULT        : result 不是合法枚举值
          - MANUAL_PASS           : result=PASS 但无动作（疑似手工文字 PASS）
          - EMPTY_EXPECTED        : result=PASS 但 expected 为空（P0-6）
          - EMPTY_ACTUAL          : result=PASS 但 actual 为空（P0-6）
          - MISSING_OWNER         : finalPassOwner 缺失（P0-6）
          - MISSING_QUALIFICATION : qualification 缺失（P0-6）
          - MISSING_DIGEST        : scenarioContractDigest 缺失（P0-6）
          - MISSING_REPLAYS       : requiredReplays 缺失或 <1（P0-6）
          - MISSING_FIELD         : 必需字段缺失或类型错误
        """
        errors: list[str] = []

        # 检查 environmentManifestDigest 不为空
        if not self.environmentManifestDigest or not self.environmentManifestDigest.strip():
            errors.append(
                f"MISSING_ENV_DIGEST [{self.receiptId}]: environmentManifestDigest 为空"
            )

        # 检查 result 合法性
        if self.result not in VALID_RESULTS:
            errors.append(
                f"INVALID_RESULT [{self.receiptId}]: result='{self.result}' 不合法；"
                f"必须是 {sorted(VALID_RESULTS)}"
            )

        # GPT 第五轮 item 6：actions/expected/actual 的非空约束由 schema 单一真源
        # 无条件强制（不再仅在 result==PASS 时查）。schema 声明 actions.minItems=1、
        # expected.minProperties=1、actual.minProperties=1；validate() 运行时读取
        # 这些约束并对所有 result 强制，消除手写 validate 与 schema 的可复现漂移
        # （旧代码对 result=FAIL 且三项为空返回 []，而 schema 会拒绝）。
        _schema = _load_receipt_schema()
        _props = _schema.get("properties", {})
        _actions_min = _props.get("actions", {}).get("minItems", 1)
        _expected_min = _props.get("expected", {}).get("minProperties", 1)
        _actual_min = _props.get("actual", {}).get("minProperties", 1)

        # 无条件：actions 至少 minItems 条（保留 MANUAL_PASS 前缀，兼容既有测试）
        if len(self.actions) < _actions_min:
            errors.append(
                f"MANUAL_PASS [{self.receiptId}]: actions 数量 {len(self.actions)} < "
                f"schema minItems={_actions_min}；机器回执必须记录至少一条执行动作"
            )

        # 无条件：expected 至少 minProperties 个属性
        if len(self.expected) < _expected_min:
            errors.append(
                f"EMPTY_EXPECTED [{self.receiptId}]: expected 属性数 {len(self.expected)} < "
                f"schema minProperties={_expected_min}；无法证明期望被验证"
            )

        # 无条件：actual 至少 minProperties 个属性
        if len(self.actual) < _actual_min:
            errors.append(
                f"EMPTY_ACTUAL [{self.receiptId}]: actual 属性数 {len(self.actual)} < "
                f"schema minProperties={_actual_min}；无法证明真实观察被记录"
            )

        # P0-6：finalPassOwner 必填（result=PASS 时强制，FAIL/BLOCKED 时允许 None，
        # 但仍然报错以便聚合时识别伪造 FAIL）。我们保持对全部 result 强制以避免任何字段缺失。
        if not self.finalPassOwner or not self.finalPassOwner.strip():
            errors.append(
                f"MISSING_OWNER [{self.receiptId}]: finalPassOwner 缺失，"
                "无法确认最终通过责任人"
            )

        # P0-6：qualification 必填
        if not self.qualification or not self.qualification.strip():
            errors.append(
                f"MISSING_QUALIFICATION [{self.receiptId}]: qualification 缺失，"
                "无法确认执行资质标签"
            )

        # P0-6：scenarioContractDigest 必填（对所有 result 强制，聚合端 PASS 单独再校验）
        if not self.scenarioContractDigest or not self.scenarioContractDigest.strip():
            errors.append(
                f"MISSING_DIGEST [{self.receiptId}]: scenarioContractDigest 缺失，"
                "无法绑定场景合同身份"
            )

        # P0-6：requiredReplays 必填且 >=1
        if self.requiredReplays is None:
            errors.append(
                f"MISSING_REPLAYS [{self.receiptId}]: requiredReplays 缺失，"
                "无法计算覆盖门禁"
            )
        elif self.requiredReplays < 1:
            errors.append(
                f"MISSING_REPLAYS [{self.receiptId}]: requiredReplays={self.requiredReplays} "
                "必须 >=1"
            )

        # 检查 runtimeId（若存在则必须已知）
        if self.runtimeId is not None and self.runtimeId not in KNOWN_RUNTIME_IDS:
            errors.append(
                f"UNKNOWN_RUNTIME [{self.receiptId}]: runtimeId='{self.runtimeId}' "
                f"不在已知运行时列表 {sorted(KNOWN_RUNTIME_IDS)}"
            )

        # 检查 sideEffectCount 非负
        if self.sideEffectCount < 0:
            errors.append(
                f"MISSING_FIELD [{self.receiptId}]: sideEffectCount={self.sideEffectCount} 不能为负数"
            )

        # GPT 第六轮 item 3：在上述带前缀的语义检查之外，追加**完整** JSON Schema
        # 校验（真 jsonschema 优先，缺库回退 stdlib 校验器）。这补上旧 validate 漏掉的
        # minLength / enum / additionalProperties / 嵌套 items 等全部约束，使运行时
        # 校验与 schema 单一真源真正一致（反例：空 receiptId/testId/actionId/description
        # 现在会返回 SCHEMA 前缀错误，而非旧代码的 0 错误）。校验落盘表示 to_dict()。
        errors.extend(schema_validation_errors(self.to_dict()))

        return errors

    # ── 序列化 ────────────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TestReceipt:
        """从字典反序列化回执（遵循 test-receipt.v1 schema）。"""
        actions = [
            ReceiptAction.from_dict(a) if isinstance(a, dict) else a
            for a in d.get("actions", [])
        ]
        return cls(
            receiptId=d["receiptId"],
            testId=d["testId"],
            environmentManifestDigest=d.get("environmentManifestDigest", ""),
            actions=actions,
            expected=d.get("expected", {}),
            actual=d.get("actual", {}),
            sideEffectCount=d.get("sideEffectCount", 0),
            artifactDigests=d.get("artifactDigests", []),
            result=d.get("result", ""),
            createdAt=d.get("createdAt", ""),
            failureReason=d.get("failureReason"),
            scenarioContractDigest=d.get("scenarioContractDigest"),
            implementationContributors=d.get("implementationContributors", []),
            finalPassOwner=d.get("finalPassOwner"),
            qualification=d.get("qualification"),
            requiredReplays=d.get("requiredReplays"),
            runtimeId=d.get("runtimeId"),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（用于 JSON 输出）。"""
        result: dict[str, Any] = {
            "receiptId": self.receiptId,
            "testId": self.testId,
            "environmentManifestDigest": self.environmentManifestDigest,
            "actions": [a.to_dict() for a in self.actions],
            "expected": self.expected,
            "actual": self.actual,
            "sideEffectCount": self.sideEffectCount,
            "artifactDigests": self.artifactDigests,
            "result": self.result,
            "createdAt": self.createdAt,
        }
        if self.failureReason is not None:
            result["failureReason"] = self.failureReason
        if self.scenarioContractDigest is not None:
            result["scenarioContractDigest"] = self.scenarioContractDigest
        if self.implementationContributors:
            result["implementationContributors"] = self.implementationContributors
        if self.finalPassOwner is not None:
            result["finalPassOwner"] = self.finalPassOwner
        if self.qualification is not None:
            result["qualification"] = self.qualification
        if self.requiredReplays is not None:
            result["requiredReplays"] = self.requiredReplays
        if self.runtimeId is not None:
            result["runtimeId"] = self.runtimeId
        return result


# ─── 批量加载 ─────────────────────────────────────────────────────────────────


def load_receipts_from_file(path: Path) -> list[TestReceipt]:
    """
    从单个 JSON 文件加载回执列表。

    文件格式：JSON 数组，每个元素为一个 TestReceipt 对象。
    加载失败时抛出 ValueError（包含文件路径和原因）。
    """
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败 [{path}]: {exc}") from exc

    if not isinstance(data, list):
        raise ValueError(f"回执文件必须是 JSON 数组 [{path}]")

    receipts: list[TestReceipt] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"回执条目 [{i}] 不是对象 [{path}]")
        try:
            receipts.append(TestReceipt.from_dict(item))
        except KeyError as exc:
            raise ValueError(f"回执条目 [{i}] 缺少必需字段 {exc} [{path}]") from exc

    return receipts


def load_receipts_from_dir(directory: Path) -> list[TestReceipt]:
    """
    从目录中加载所有 .json 文件的回执。

    遍历目录下所有 *.json 文件，合并为一个回执列表。
    """
    all_receipts: list[TestReceipt] = []
    for json_file in sorted(directory.glob("*.json")):
        all_receipts.extend(load_receipts_from_file(json_file))
    return all_receipts
