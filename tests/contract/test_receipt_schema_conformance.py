"""
tests/contract/test_receipt_schema_conformance.py

GPT 第六轮 REVISE item 3：证明 receipts.py 的运行时校验语义与 JSON Schema
「单一真源」真正一致，而不仅仅比较字段集合。

第六轮指出旧 validate() 只读 schema 的三个数量约束（actions.minItems /
expected.minProperties / actual.minProperties），未执行完整 JSON Schema 校验：
反例（空 receiptId/testId/actionId/description）在旧 validate() 得 0 错误，
但真 jsonschema 得 4 错误。本测试从两个角度锁死修复：

  A. 反例锁定：schema_validation_errors 与 TestReceipt.validate() 对该反例都必须
     返回非空错误（不再是 0）。
  B. 等价证明：以 test-receipt.v1.schema 为真源，系统性变异出一整套违规实例，
     用**真** jsonschema（Draft7Validator，权威参考实现）与 stdlib 回退校验器
     `_stdlib_schema_errors` 逐例比对「是否判违规」，要求二者对每个实例的通过/
     拒绝决定完全一致——这证明缺库环境下的回退器与参考实现语义等价，验收环境
     （锁定 py312 有 jsonschema）走的则是真库本身。

真 jsonschema 不可用时本测试整体 skip（缺库环境不能证明等价）；验收唯一命令在
锁定 py312 下运行，该环境已安装 jsonschema 4.26.0，故 A-1 中本测试真实执行。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

# 真 jsonschema 是本测试的参考实现；缺库则无法证明等价，直接 skip。
jsonschema = pytest.importorskip("jsonschema")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "contracts" / "schemas" / "test-receipt.v1.schema.json"
VALID_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "receipts" / "valid.json"


def _load_schema() -> dict[str, Any]:
    """加载 test-receipt.v1 schema（本测试的真源）。"""
    with SCHEMA_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _valid_base() -> dict[str, Any]:
    """从 valid.json 取一个通过全量 schema 的基准回执，作为变异起点。"""
    with VALID_FIXTURE.open(encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list) and data, "valid.json 应是非空 JSON 数组"
    return data[0]


def _real_jsonschema_has_errors(instance: dict[str, Any], schema: dict[str, Any]) -> bool:
    """权威参考实现（Draft7Validator）是否判该实例违规。"""
    validator = jsonschema.Draft7Validator(schema)
    return bool(list(validator.iter_errors(instance)))


def _stdlib_has_errors(instance: dict[str, Any], schema: dict[str, Any]) -> bool:
    """stdlib 回退校验器是否判该实例违规。"""
    from factory_agent.testing.receipts import _stdlib_schema_errors  # type: ignore[import]

    errors: list[str] = []
    _stdlib_schema_errors(instance, schema, "", errors)
    return bool(errors)


def _build_mutation_battery(base: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """基于 schema 关键字系统性变异出违规/合规实例。返回 (label, instance) 列表。

    覆盖本 schema 用到的每个关键字面：required 缺失、minLength 空串（顶层 +
    嵌套 action 字段）、enum 越界、minItems 空数组、minProperties 空对象、
    minimum 负数、type 错配、additionalProperties 额外键，外加一个合规基准。
    """
    battery: list[tuple[str, dict[str, Any]]] = []

    # 合规基准：两个校验器都必须判「通过」。
    battery.append(("valid_base", copy.deepcopy(base)))

    # required 缺失（逐个必需顶层字段删除）。
    for req in ("receiptId", "testId", "environmentManifestDigest", "actions",
                "expected", "actual", "sideEffectCount", "artifactDigests",
                "result", "createdAt", "finalPassOwner", "requiredReplays",
                "scenarioContractDigest", "qualification"):
        m = copy.deepcopy(base)
        m.pop(req, None)
        battery.append((f"missing_{req}", m))

    # minLength 空串（顶层字符串字段）。
    for f in ("receiptId", "testId", "environmentManifestDigest",
              "scenarioContractDigest", "finalPassOwner"):
        m = copy.deepcopy(base)
        m[f] = ""
        battery.append((f"empty_{f}", m))

    # 嵌套 action 字段空串 / 缺失（reviewer 反例的核心：空 actionId/description）。
    m = copy.deepcopy(base)
    m["actions"] = [{"actionId": "", "description": "", "executedAt": "2026-08-06T00:00:00Z"}]
    battery.append(("empty_action_fields", m))
    m = copy.deepcopy(base)
    m["actions"] = [{"description": "x", "executedAt": "2026-08-06T00:00:00Z"}]  # 缺 actionId
    battery.append(("action_missing_actionId", m))

    # minItems：空 actions 数组。
    m = copy.deepcopy(base)
    m["actions"] = []
    battery.append(("empty_actions_array", m))

    # minProperties：空 expected / actual 对象。
    m = copy.deepcopy(base)
    m["expected"] = {}
    battery.append(("empty_expected", m))
    m = copy.deepcopy(base)
    m["actual"] = {}
    battery.append(("empty_actual", m))

    # enum 越界：result / qualification。
    m = copy.deepcopy(base)
    m["result"] = "MAYBE"
    battery.append(("bad_result_enum", m))
    m = copy.deepcopy(base)
    m["qualification"] = "codex-reviewer"  # 不在 [PARTIAL, FINAL]
    battery.append(("bad_qualification_enum", m))
    m = copy.deepcopy(base)
    m["runtimeId"] = "unknown-runtime-x99"  # 可选字段但存在则必须在 enum 内
    battery.append(("bad_runtimeId_enum", m))

    # minimum：sideEffectCount 负数 / requiredReplays < 1。
    m = copy.deepcopy(base)
    m["sideEffectCount"] = -1
    battery.append(("negative_side_effect", m))
    m = copy.deepcopy(base)
    m["requiredReplays"] = 0
    battery.append(("replays_below_min", m))

    # type 错配：sideEffectCount 应为 integer，给字符串。
    m = copy.deepcopy(base)
    m["sideEffectCount"] = "zero"
    battery.append(("side_effect_wrong_type", m))

    # additionalProperties:false：追加未声明顶层键。
    m = copy.deepcopy(base)
    m["totallyUnknownField"] = "x"
    battery.append(("extra_top_level_prop", m))

    return battery


def test_reviewer_counterexample_now_rejected() -> None:
    """锁定第六轮 item 3 反例：空 receiptId/testId/actionId/description 现在必须被拒。

    旧 validate() 对该对象返回 0 错误；真 jsonschema 返回 4 错误。修复后
    schema_validation_errors 与 TestReceipt.validate() 均须返回非空错误。
    """
    from factory_agent.testing.receipts import TestReceipt, schema_validation_errors  # type: ignore[import]

    schema = _load_schema()
    counterexample = {
        "receiptId": "",
        "testId": "",
        "environmentManifestDigest": "sha256:" + "a" * 64,
        "actions": [{"actionId": "", "description": "", "executedAt": "2026-08-06T00:00:00Z"}],
        "expected": {"ok": True},
        "actual": {"ok": True},
        "sideEffectCount": 0,
        "artifactDigests": [],
        "result": "PASS",
        "createdAt": "2026-08-06T00:00:00Z",
        "finalPassOwner": "codex-reviewer",
        "requiredReplays": 1,
        "scenarioContractDigest": "sha256:" + "0" * 64,
        "qualification": "FINAL",
    }

    # 真 jsonschema 基线：确认该反例确实违规（空 receiptId/testId/actionId/description）。
    real_errors = list(jsonschema.Draft7Validator(schema).iter_errors(counterexample))
    assert real_errors, "参考实现应判该反例违规"

    # 我们的 schema_validation_errors（真库优先）必须一致判违规且非空。
    sve = schema_validation_errors(counterexample)
    assert sve, f"schema_validation_errors 应返回非空错误，实际 {sve}"

    # 端到端：TestReceipt.validate() 追加了全量 schema 校验，也必须返回非空。
    rcpt = TestReceipt.from_dict(counterexample)
    verrs = rcpt.validate()
    assert any(e.startswith("SCHEMA") for e in verrs), (
        f"validate() 应包含 SCHEMA 前缀错误，实际 {verrs}"
    )


def test_stdlib_validator_matches_jsonschema_on_battery() -> None:
    """等价证明：stdlib 回退校验器与真 jsonschema 对整套变异电池的通过/拒绝决定一致。

    这证明「缺 jsonschema 库」的环境下回退器不会放过真库会拒的实例，也不会误拒
    真库放行的实例——运行时语义与 schema 单一真源真正对齐。
    """
    schema = _load_schema()
    base = _valid_base()
    battery = _build_mutation_battery(base)

    disagreements: list[str] = []
    for label, instance in battery:
        real = _real_jsonschema_has_errors(instance, schema)
        stdlib = _stdlib_has_errors(instance, schema)
        if real != stdlib:
            disagreements.append(f"{label}: jsonschema={real} stdlib={stdlib}")

    assert not disagreements, (
        "stdlib 回退校验器与真 jsonschema 判定不一致：\n  " + "\n  ".join(disagreements)
    )


def test_valid_base_passes_both_validators() -> None:
    """基准合规回执在两个校验器下都必须 0 错误（否则等价电池的基准无意义）。"""
    schema = _load_schema()
    base = _valid_base()
    assert not _real_jsonschema_has_errors(base, schema), "valid.json 基准应通过真 jsonschema"
    assert not _stdlib_has_errors(base, schema), "valid.json 基准应通过 stdlib 回退校验器"
