"""factory_agent.policy.plan_hash — 计划语义哈希、修订摘要与 barrier 身份。

三个标识使用同一 canonical_json 底座（NFC + RFC 8785 JCS），
必须与 TypeScript / Rust 实现字节级一致：

  semanticPlanHash
      只标识计划语义，跨修订版本稳定；纳入字段严格来自 Master Spec §6.2：
        schemaVersion, goal, assumptions, scope, constraints,
        acceptanceCriteria, targetStage,
        repository{mode, root, baseBranch, baseCommit},
        workPlan{dagVersion, nodes, barriers},
        riskProfile, nodeCapabilityMapVersion, stageCapabilityMapVersion
      排除 taskId / specRevision / parentRevisionId / intentAuthorizationId /
      生成时间 / 事件·授权·receipt ID / hash·signature 字段。

  planRevisionDigest
      标识包含谱系与授权关联的完整不可变 PlanRevision；对「除自身值与签名外」
      的完整对象做同一 canonicalizer。语义相同可共享 semanticPlanHash，
      但不同谱系必须得到不同 planRevisionDigest。

  barrierId
      稳定 barrier 身份，使用域分离数组，禁止无长度边界的字符串拼接：
        "bar_" + lowercaseHex(SHA-256(JCS(
          ["factory-barrier-v1", runId, planRevisionDigest,
           businessPhase, barrierOrdinal])))

失败错误码：plan-hash-error（不记录完整计划正文）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from functools import lru_cache
from typing import Any

# jsonschema 当前未提供 PEP 561 stubs；只在该第三方 import 处精确抑制，不放宽全局 mypy。
import jsonschema  # type: ignore[import-untyped]

from factory_agent.contracts.generated.models import (
    PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON,
    PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256,
    PLAN_REVISION_SCHEMA_JSON,
    PLAN_REVISION_SCHEMA_JSON_SHA256,
    RUN_SPEC_SCHEMA_JSON,
    RUN_SPEC_SCHEMA_JSON_SHA256,
)
from factory_agent.errors import FactoryError
from factory_agent.policy.canonical_json import canonicalize

# 算法版本：影响输出字节的任何改动都必须同步升级并更新 golden vectors。
PLAN_HASH_VERSION = "plan-hash-v1"

# barrier 身份的域分离标签（数组首元素）。
_BARRIER_DOMAIN = "factory-barrier-v1"

# semanticPlanHash 纳入的顶层字段（严格来自 Master Spec §6.2）。
_SEMANTIC_TOP_FIELDS: tuple[str, ...] = (
    "schemaVersion",
    "goal",
    "assumptions",
    "scope",
    "constraints",
    "acceptanceCriteria",
    "targetStage",
    "riskProfile",
    "nodeCapabilityMapVersion",
    "stageCapabilityMapVersion",
)

# repository 子对象纳入的字段。
_REPO_FIELDS: tuple[str, ...] = ("mode", "root", "baseBranch", "baseCommit")

# workPlan 子对象纳入的字段。
_WORKPLAN_FIELDS: tuple[str, ...] = ("dagVersion", "nodes", "barriers")

# planRevisionDigest 必须排除的字段（自身摘要值与签名）。
_DIGEST_EXCLUDED_FIELDS: frozenset[str] = frozenset({"planRevisionDigest", "signature"})

# 摘要输出前缀。
_SHA256_PREFIX = "sha256:"

# 跨语言只接受可移植 RFC3339 子集；pattern 负责 wire 形态，本函数再核验日历和 UTC offset。
_FACTORY_RFC3339_DATE_TIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9]"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$"
)
_STABLE_VALIDATION_DETAIL = "INVALID_PLAN_HASH_INPUT"
_DRAFT7_SCHEMA_URI = "http://json-schema.org/draft-07/schema#"
_KNOWN_VALIDATOR_FORMATS = frozenset({"date-time"})
_SINGLE_SCHEMA_KEYWORDS = frozenset(
    {
        "additionalItems",
        "additionalProperties",
        "contains",
        "if",
        "then",
        "else",
        "not",
        "propertyNames",
    }
)
_SCHEMA_MAP_KEYWORDS = frozenset({"properties", "patternProperties", "definitions", "$defs"})
_SCHEMA_ARRAY_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf"})


class PlanHashError(FactoryError):
    """计划哈希输入非法（缺字段、类型错误等）时抛出。"""

    error_code = "plan-hash-error"


def is_factory_rfc3339_date_time(value: object) -> bool:
    """校验 Factory 冻结的 RFC3339 子集，拒绝 leap-second、year 0000 与不合法日历/offset。"""
    if not isinstance(value, str):
        return True
    match = _FACTORY_RFC3339_DATE_TIME.fullmatch(value)
    if match is None:
        return False

    # 权威 pattern 故意只定义非捕获分组；按冻结 ASCII wire 位置取值，避免运行时 regex
    # 捕获组形态成为跨语言差异源。
    year, month, day = int(value[0:4]), int(value[5:7]), int(value[8:10])
    hour, minute = int(value[11:13]), int(value[14:16])
    if year == 0 or month < 1 or month > 12 or hour > 23 or minute > 59:
        return False
    days_in_month = 29 if month == 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    if month != 2:
        days_in_month = 30 if month in {4, 6, 9, 11} else 31
    if day < 1 or day > days_in_month:
        return False

    zone_start = 19
    if value[zone_start] == ".":
        zone_start += 1
        while zone_start < len(value) and value[zone_start].isdigit():
            zone_start += 1
    zone = value[zone_start:]
    if zone.casefold() == "z":
        return True
    offset_hour, offset_minute = int(zone[1:3]), int(zone[4:6])
    return offset_hour <= 23 and offset_minute <= 59


def factory_format_checker() -> jsonschema.FormatChecker:
    """创建与 TS/Rust 同名 date-time 校验器；不依赖可选第三方 format extras。"""
    checker = jsonschema.FormatChecker()
    checker.checks("date-time")(is_factory_rfc3339_date_time)
    return checker


def _reject_duplicate_schema_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝生成常量中的重复 JSON key，避免损坏 schema 被 json.loads 静默覆盖。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate generated schema key")
        result[key] = value
    return result


def _walk_validator_subschema(value: object) -> None:
    """仅沿 Draft7 schema 位置遍历，拒绝外部 ref/未知 format 而不误判用户字段名。"""
    if isinstance(value, bool) or not isinstance(value, dict):
        return

    reference = value.get("$ref")
    if reference is not None and (not isinstance(reference, str) or not reference.startswith("#")):
        raise ValueError("external schema reference")
    format_name = value.get("format")
    if format_name is not None and (
        not isinstance(format_name, str) or format_name not in _KNOWN_VALIDATOR_FORMATS
    ):
        raise ValueError("unknown schema format")

    for keyword in _SINGLE_SCHEMA_KEYWORDS:
        if keyword in value:
            _walk_validator_subschema(value[keyword])
    for keyword in _SCHEMA_MAP_KEYWORDS:
        schema_map = value.get(keyword)
        if isinstance(schema_map, dict):
            for child_schema in schema_map.values():
                _walk_validator_subschema(child_schema)
    items = value.get("items")
    if isinstance(items, list):
        for child_schema in items:
            _walk_validator_subschema(child_schema)
    elif items is not None:
        _walk_validator_subschema(items)
    for keyword in _SCHEMA_ARRAY_KEYWORDS:
        schemas = value.get(keyword)
        if isinstance(schemas, list):
            for child_schema in schemas:
                _walk_validator_subschema(child_schema)
    dependencies = value.get("dependencies")
    if isinstance(dependencies, dict):
        for dependency in dependencies.values():
            if isinstance(dependency, (bool, dict)):
                _walk_validator_subschema(dependency)


def _parse_generated_validator_schema(schema_json: str) -> dict[str, Any]:
    """惰性解析 codegen 原始 JSON，并在编译前锁死 Draft7/ref/format 边界。"""
    schema = json.loads(schema_json, object_pairs_hook=_reject_duplicate_schema_keys)
    if not isinstance(schema, dict) or schema.get("$schema") != _DRAFT7_SCHEMA_URI:
        raise ValueError("invalid generated draft7 schema")
    _walk_validator_subschema(schema)
    jsonschema.Draft7Validator.check_schema(schema)
    return schema


def _verify_embedded_schema_integrity(schema_json: str, expected_sha256: str) -> None:
    """在 JSON 解析前验证生成常量的 UTF-8 内容身份，任意字节漂移均 fail closed。"""
    if not isinstance(schema_json, str) or not isinstance(expected_sha256, str):
        raise PlanHashError(_STABLE_VALIDATION_DETAIL)
    actual_sha256 = _SHA256_PREFIX + hashlib.sha256(schema_json.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise PlanHashError(_STABLE_VALIDATION_DETAIL)


def _build_validator(schema_json: str, expected_sha256: str) -> jsonschema.Draft7Validator:
    """惰性解析/编译 codegen schema，初始化异常也必须归一化为稳定哈希错误。"""
    try:
        _verify_embedded_schema_integrity(schema_json, expected_sha256)
        schema = _parse_generated_validator_schema(schema_json)
        return jsonschema.Draft7Validator(schema, format_checker=factory_format_checker())
    except PlanHashError:
        raise
    except Exception as exc:  # noqa: BLE001 - 纯函数边界不泄露 schema 或底层异常正文。
        raise PlanHashError(_STABLE_VALIDATION_DETAIL) from exc


@lru_cache(maxsize=1)
def _run_spec_validator() -> jsonschema.Draft7Validator:
    """按需构建 RunSpec validator，避免生成模块 import-time 解析失败逃逸。"""
    return _build_validator(RUN_SPEC_SCHEMA_JSON, RUN_SPEC_SCHEMA_JSON_SHA256)


@lru_cache(maxsize=1)
def _plan_revision_validator() -> jsonschema.Draft7Validator:
    """按需构建完整 PlanRevision validator，摘要字段剥离前先校验原始 wire。"""
    return _build_validator(PLAN_REVISION_SCHEMA_JSON, PLAN_REVISION_SCHEMA_JSON_SHA256)


@lru_cache(maxsize=1)
def _plan_revision_digest_material_validator() -> jsonschema.Draft7Validator:
    """按需构建 PlanRevision digest material validator，确保仅排除两项字段。"""
    return _build_validator(
        PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON,
        PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256,
    )


def _validate_wire(value: object, validator: jsonschema.Draft7Validator) -> None:
    """执行完整权威 schema；不可信 wire 一律只返回稳定脱敏错误分类。"""
    try:
        if next(validator.iter_errors(value), None) is not None:
            raise PlanHashError(_STABLE_VALIDATION_DETAIL)
    except PlanHashError:
        raise
    except Exception as exc:  # noqa: BLE001 - validator 细节不得随纯函数错误外泄。
        raise PlanHashError(_STABLE_VALIDATION_DETAIL) from exc


def _validate_run_spec(value: object) -> None:
    """RunSpec 必须在任何语义投影前通过完整 schema，禁止 partial projection 绕过。"""
    _validate_wire(value, _run_spec_validator())


def _validate_plan_revision_digest_material(value: object) -> None:
    """摘要 material 只允许由权威 PlanRevision schema 机械派生的字段集合。"""
    _validate_wire(value, _plan_revision_digest_material_validator())


def _validate_plan_revision(value: object) -> None:
    """完整 PlanRevision 必须在摘要字段剥离前通过权威 full schema。"""
    _validate_wire(value, _plan_revision_validator())


def _require_mapping(value: object) -> dict[str, Any]:
    """校验 value 为字典，否则 fail closed。

    Args:
        value: 待校验对象。
    Returns:
        校验通过的字典。

    Raises:
        PlanHashError: value 非字典。
    """
    if not isinstance(value, dict):
        raise PlanHashError(_STABLE_VALIDATION_DETAIL)
    return value


def _project_subset(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """从 source 提取 fields 指定的字段子集，缺字段即 fail closed。

    Args:
        source: 源字典。
        fields: 必须存在的字段名元组。
    Returns:
        仅含指定字段的新字典。

    Raises:
        PlanHashError: 任一字段缺失。
    """
    projected: dict[str, Any] = {}
    for name in fields:
        if name not in source:
            raise PlanHashError(_STABLE_VALIDATION_DETAIL)
        projected[name] = source[name]
    return projected


def build_semantic_projection(plan: object) -> dict[str, Any]:
    """构造 semanticPlanHash 的字段投影（不含任何谱系/时间/ID/hash 字段）。

    Args:
        plan: 计划语义源对象（RunSpec 冻结视图）。

    Returns:
        仅含 §6.2 语义字段的投影字典，供 canonicalize 使用。

    Raises:
        PlanHashError: 缺少必需语义字段或子对象结构非法。
    """
    _validate_run_spec(plan)
    plan_mapping = _require_mapping(plan)
    projection = _project_subset(plan_mapping, _SEMANTIC_TOP_FIELDS)

    # repository：仅纳入 mode/root/baseBranch/baseCommit。
    repository = _require_mapping(plan_mapping.get("repository"))
    projection["repository"] = _project_subset(repository, _REPO_FIELDS)

    # workPlan：仅纳入 dagVersion/nodes/barriers。
    work_plan = _require_mapping(plan_mapping.get("workPlan"))
    projection["workPlan"] = _project_subset(work_plan, _WORKPLAN_FIELDS)

    return projection


def semantic_plan_hash(plan: object) -> str:
    """计算 semanticPlanHash（跨修订版本稳定的计划语义身份）。

    Args:
        plan: 计划语义源对象。

    Returns:
        形如 "sha256:<64 位小写十六进制>" 的摘要。

    Raises:
        PlanHashError: 语义字段缺失、结构非法或字段值无法规范化。
    """
    projection = build_semantic_projection(plan)
    try:
        digest = hashlib.sha256(canonicalize(projection)).hexdigest()
    except Exception as exc:  # noqa: BLE001 - hash API 只能输出稳定脱敏错误。
        raise PlanHashError(_STABLE_VALIDATION_DETAIL) from exc
    return f"{_SHA256_PREFIX}{digest}"


def plan_revision_digest(revision: object) -> str:
    """计算 planRevisionDigest（完整不可变修订记录身份）。

    对「除 planRevisionDigest 与 signature 外」的完整 PlanRevision 做同一
    canonicalizer；因此谱系（parentRevisionId）、授权关联、semanticPlanHash
    和生成时间都参与摘要，使不同谱系得到不同结果。

    Args:
        revision: 完整不可变 PlanRevision 对象。

    Returns:
        形如 "sha256:<64 位小写十六进制>" 的摘要。

    Raises:
        PlanHashError: revision 非对象、结构非法或字段值无法规范化。
    """
    _validate_plan_revision(revision)
    revision_mapping = _require_mapping(revision)
    # full schema 已保证摘要必填、签名可选；此处精确剥离两项，禁止先删后验掩盖非法值。
    material = dict(revision_mapping)
    del material["planRevisionDigest"]
    if "signature" in material:
        del material["signature"]
    _validate_plan_revision_digest_material(material)
    # 纯函数没有日志出口；稳定错误由后续应用入口脱敏记录，本层不声称已实现 runtime 日志。
    try:
        digest = hashlib.sha256(canonicalize(material)).hexdigest()
    except Exception as exc:  # noqa: BLE001 - hash API 只能输出稳定脱敏错误。
        raise PlanHashError(_STABLE_VALIDATION_DETAIL) from exc
    return f"{_SHA256_PREFIX}{digest}"


def barrier_id(
    run_id: str,
    plan_revision_digest_value: str,
    business_phase: str,
    barrier_ordinal: int,
) -> str:
    """计算 barrierId（稳定 barrier 身份，使用域分离数组）。

    Args:
        run_id:                     所属 Run 的 ID。
        plan_revision_digest_value: 对应 PlanRevision 的 planRevisionDigest。
        business_phase:             业务阶段名（如 IMPLEMENTING）。
        barrier_ordinal:            barrier 在该阶段内的序号（整数）。

    Returns:
        形如 "bar_<64 位小写十六进制>" 的身份。

    Raises:
        PlanHashError: 参数类型非法或参数值无法规范化。
    """
    if not isinstance(run_id, str) or not run_id:
        raise PlanHashError("run_id 必须为非空字符串")
    if not isinstance(plan_revision_digest_value, str) or not plan_revision_digest_value:
        raise PlanHashError("planRevisionDigest 必须为非空字符串")
    if not isinstance(business_phase, str) or not business_phase:
        raise PlanHashError("businessPhase 必须为非空字符串")
    # bool 是 int 子类，必须显式排除，避免 True 被当作 1。
    if isinstance(barrier_ordinal, bool) or not isinstance(barrier_ordinal, int):
        raise PlanHashError("barrierOrdinal 必须为整数")

    domain_array = [
        _BARRIER_DOMAIN,
        run_id,
        plan_revision_digest_value,
        business_phase,
        barrier_ordinal,
    ]
    digest = hashlib.sha256(canonicalize(domain_array)).hexdigest()
    return f"bar_{digest}"
