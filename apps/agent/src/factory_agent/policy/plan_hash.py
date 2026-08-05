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
from typing import Any

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


class PlanHashError(FactoryError):
    """计划哈希输入非法（缺字段、类型错误等）时抛出。"""

    error_code = "plan-hash-error"


def _require_mapping(value: object, label: str) -> dict[str, object]:  # noqa: ANN401
    """校验 value 为字典，否则 fail closed。

    Args:
        value: 待校验对象。
        label: 出错时的字段标签（不含敏感内容）。

    Returns:
        校验通过的字典。

    Raises:
        PlanHashError: value 非字典。
    """
    if not isinstance(value, dict):
        raise PlanHashError(f"字段 '{label}' 必须为对象")
    return value


def _project_subset(
    source: dict[str, Any], fields: tuple[str, ...], label: str
) -> dict[str, Any]:
    """从 source 提取 fields 指定的字段子集，缺字段即 fail closed。

    Args:
        source: 源字典。
        fields: 必须存在的字段名元组。
        label:  出错时的对象标签。

    Returns:
        仅含指定字段的新字典。

    Raises:
        PlanHashError: 任一字段缺失。
    """
    projected: dict[str, Any] = {}
    for name in fields:
        if name not in source:
            raise PlanHashError(f"{label} 缺少语义字段 '{name}'（{PLAN_HASH_VERSION}）")
        projected[name] = source[name]
    return projected


def build_semantic_projection(plan: dict[str, Any]) -> dict[str, Any]:
    """构造 semanticPlanHash 的字段投影（不含任何谱系/时间/ID/hash 字段）。

    Args:
        plan: 计划语义源对象（RunSpec 冻结视图）。

    Returns:
        仅含 §6.2 语义字段的投影字典，供 canonicalize 使用。

    Raises:
        PlanHashError: 缺少必需语义字段或子对象结构非法。
    """
    plan = _require_mapping(plan, "plan")
    projection = _project_subset(plan, _SEMANTIC_TOP_FIELDS, "plan")

    # repository：仅纳入 mode/root/baseBranch/baseCommit。
    repository = _require_mapping(plan.get("repository"), "repository")
    projection["repository"] = _project_subset(repository, _REPO_FIELDS, "repository")

    # workPlan：仅纳入 dagVersion/nodes/barriers。
    work_plan = _require_mapping(plan.get("workPlan"), "workPlan")
    projection["workPlan"] = _project_subset(work_plan, _WORKPLAN_FIELDS, "workPlan")

    return projection


def semantic_plan_hash(plan: dict[str, Any]) -> str:
    """计算 semanticPlanHash（跨修订版本稳定的计划语义身份）。

    Args:
        plan: 计划语义源对象。

    Returns:
        形如 "sha256:<64 位小写十六进制>" 的摘要。

    Raises:
        PlanHashError:      语义字段缺失或结构非法。
        CanonicalJsonError: 字段值含非法数字/类型/重复键。
    """
    projection = build_semantic_projection(plan)
    digest = hashlib.sha256(canonicalize(projection)).hexdigest()
    return f"{_SHA256_PREFIX}{digest}"


def plan_revision_digest(revision: dict[str, Any]) -> str:
    """计算 planRevisionDigest（完整不可变修订记录身份）。

    对「除 planRevisionDigest 与 signature 外」的完整 PlanRevision 做同一
    canonicalizer；因此谱系（parentRevisionId）、授权关联、semanticPlanHash
    和生成时间都参与摘要，使不同谱系得到不同结果。

    Args:
        revision: 完整不可变 PlanRevision 对象。

    Returns:
        形如 "sha256:<64 位小写十六进制>" 的摘要。

    Raises:
        PlanHashError:      revision 非对象。
        CanonicalJsonError: 字段值含非法数字/类型/重复键。
    """
    revision = _require_mapping(revision, "revision")
    # 排除自身摘要值与签名，其余字段全部纳入。
    material = {k: v for k, v in revision.items() if k not in _DIGEST_EXCLUDED_FIELDS}
    digest = hashlib.sha256(canonicalize(material)).hexdigest()
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
        PlanHashError:      参数类型非法。
        CanonicalJsonError: 参数值无法规范化。
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
