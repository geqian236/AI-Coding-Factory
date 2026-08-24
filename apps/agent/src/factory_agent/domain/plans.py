"""PlanRevision 双合同与双 canonical blob 的纯领域边界。

本模块不访问数据库。它只验证两份冻结 wire 合同、共同 selector 与无损 DAG 映射，
并把普通 ``dict``/``list`` 深复制成可被 JCS 编码但不可原位修改的只读 facade。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, NoReturn

from factory_agent.errors import FactoryError
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.policy.plan_hash import plan_revision_digest, semantic_plan_hash

_RUN_SPEC_SCHEMA_ID = "run-spec.v1"
_PLAN_REVISION_SCHEMA_ID = "plan-revision.v1"
_SCHEMA_VERSION = 1
_PERSISTED_RUN_SPEC_FIELDS = frozenset({"specRevision", "parentRevisionId", "planRevisionDigest", "createdAt"})
_PERSISTENCE_FIELDS = frozenset(
    {
        "plan_revision_id",
        "task_id",
        "spec_revision",
        "parent_revision_id",
        "intent_authorization_id",
        "semantic_plan_hash",
        "plan_revision_digest",
        "dag_version",
        "node_capability_map_version",
        "stage_capability_map_version",
        "created_at",
        "run_spec_schema_id",
        "run_spec_schema_version",
        "canonical_run_spec",
        "plan_revision_schema_id",
        "plan_revision_schema_version",
        "canonical_plan_revision",
    }
)


class PlanContractError(FactoryError):
    """两份独立合同自身非法或共同语义漂移时抛出。"""

    error_code = "INVALID_PLAN_REVISION_BUNDLE"


class PlanPersistenceError(FactoryError):
    """持久 selector、schema 身份或 canonical bytes 不可信时抛出。"""

    def __init__(self, error_code: str) -> None:
        """只暴露稳定错误分类，避免将合同正文或底层解析异常写入消息。"""
        self.error_code = error_code
        super().__init__(error_code)


class FrozenDict(tuple[tuple[str, Any], ...], Mapping[str, Any]):
    """由 tuple 自身承载键值对的只读 JSON 映射，实例没有可替换 slot。

    tuple 基类只承担无属性不可变存储；公开迭代/索引严格采用 Mapping 语义。
    canonicalizer 因而必须先判断 Mapping。调用 tuple 基类只读方法可看到 pair，
    但没有任何写入口，且不改变公开 JSON object 语义。
    """

    __slots__ = ()
    __hash__ = None  # type: ignore[assignment]  # JSON object 按结构相等，必须不可哈希。

    def __new__(cls, values: Mapping[str, Any]) -> FrozenDict:
        """递归取得全部值的所有权，再交给私有已冻结工厂承载为 tuple payload。"""
        return cls._from_frozen_pairs(_freeze_mapping_pairs(values))

    @classmethod
    def _from_frozen_pairs(cls, pairs: Iterable[tuple[str, Any]]) -> FrozenDict:
        """仅供本模块传入已深冻结 pair，避免 public constructor 递归调用自身。"""
        return tuple.__new__(cls, tuple(pairs))

    def __getitem__(self, key: str) -> Any:  # type: ignore[override]  # noqa: ANN401
        """按键读取已深冻结的 JSON 值。"""
        for item_key, value in tuple.__iter__(self):
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:  # type: ignore[override]
        """按冻结时的稳定插入顺序遍历键，保持 Mapping 语义。"""
        return (item[0] for item in tuple.__iter__(self))

    def __len__(self) -> int:
        """返回只读映射键数。"""
        return tuple.__len__(self)

    def __contains__(self, key: object) -> bool:
        """仅按字符串 key 判断 membership，绝不暴露 tuple 内部 pair 语义。"""
        if not isinstance(key, str):
            return False
        return any(item_key == key for item_key, _value in tuple.__iter__(self))

    def __eq__(self, other: object) -> bool:
        """与任意普通 Mapping 按键值结构比较，不暴露 tuple 内部 pair 表示。"""
        if not isinstance(other, Mapping):
            return False
        return dict(self.items()) == dict(other.items())

    def __ne__(self, other: object) -> bool:
        """保持与结构相等语义一致的显式不等比较。"""
        return not self == other

    def __repr__(self) -> str:
        """仅展示普通映射视图，不暴露 tuple pair 的内部承载方式。"""
        return f"FrozenDict({dict(self.items())!r})"

    def _immutable(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise TypeError("frozen JSON object cannot be mutated")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class FrozenList(tuple[Any, ...], Sequence[Any]):
    """由 tuple 自身承载元素的只读 JSON 序列，实例没有可替换 slot。

    tuple 基类只承担无属性不可变存储；公开比较语义覆盖为 JSON Sequence，
    因此 list/tuple/其他只读 Sequence 均按元素相等，且对象保持不可哈希。
    """

    __slots__ = ()
    __hash__ = None  # type: ignore[assignment]  # JSON array 按结构相等，必须不可哈希。

    def __new__(cls, values: Iterable[Any]) -> FrozenList:
        """消费 iterable 一次并递归取得元素所有权，不保留 generator 或嵌套可变别名。"""
        return cls._from_frozen_items(_freeze_json_value(item) for item in values)

    @classmethod
    def _from_frozen_items(cls, items: Iterable[Any]) -> FrozenList:
        """仅供本模块传入已深冻结元素，避免 public constructor 递归调用自身。"""
        return tuple.__new__(cls, tuple(items))

    def __eq__(self, other: object) -> bool:
        """与 list/tuple/只读 Sequence 按 JSON 数组元素比较。"""
        if isinstance(other, Sequence) and not isinstance(other, (str, bytes, bytearray, Mapping)):
            return tuple.__eq__(self, tuple(other))
        return False

    def __ne__(self, other: object) -> bool:
        """保持与结构相等语义一致的显式不等比较。"""
        return not self == other

    def __repr__(self) -> str:
        """仅展示普通序列视图，不暴露 tuple payload 的内部承载方式。"""
        return f"FrozenList({list(tuple.__iter__(self))!r})"

    def _immutable(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise TypeError("frozen JSON array cannot be mutated")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _freeze_mapping_pairs(values: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    """验证外来 Mapping 的字符串 key 闭集与唯一性，并深冻结每个 value。"""
    pairs: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for key, value in values.items():
        if not isinstance(key, str):
            raise TypeError("frozen JSON object key must be a string")
        if key in seen:
            raise ValueError("duplicate frozen JSON object key")
        seen.add(key)
        pairs.append((key, _freeze_json_value(value)))
    return tuple(pairs)


def _freeze_json_value(value: Any) -> Any:  # noqa: ANN401
    """递归复制并冻结 JSON-like 值；已冻结 facade 可安全复用且无可变别名。"""
    if isinstance(value, FrozenDict | FrozenList):
        return value
    if isinstance(value, Mapping):
        return FrozenDict._from_frozen_pairs(_freeze_mapping_pairs(value))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return FrozenList._from_frozen_items(_freeze_json_value(item) for item in value)
    return deepcopy(value)


def _freeze_owned_json(value: Any) -> Any:  # noqa: ANN401
    """兼容内部调用名；所有权和深冻结均由唯一 helper 完成。"""
    return _freeze_json_value(value)


def _deep_freeze_json(value: Any) -> Any:  # noqa: ANN401
    """从任意公共边界深复制并冻结，防止调用方后续修改原 fixture 穿透。"""
    return _freeze_json_value(value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """解析持久 JSON 时拒绝重复键，避免后写值被 ``json.loads`` 静默覆盖。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parse_canonical_blob(blob: object) -> dict[str, Any]:
    """严格解码并逐字节重验一个 canonical blob，错误按持久合同分类。"""
    if not isinstance(blob, bytes):
        raise PlanPersistenceError("NONCANONICAL_PLAN_BLOB")
    try:
        text = blob.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PlanPersistenceError("INVALID_PLAN_BLOB_ENCODING") from exc
    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(parsed, dict) or canonicalize(parsed) != blob:
            raise ValueError("blob is not an exact canonical object")
    except PlanPersistenceError:
        raise
    except Exception as exc:  # noqa: BLE001 - 只暴露稳定持久错误码。
        raise PlanPersistenceError("NONCANONICAL_PLAN_BLOB") from exc
    return parsed


def _raise_invalid_bundle() -> NoReturn:
    """统一收敛合同错误，避免 schema/hash 库的细节或正文越过领域边界。"""
    raise PlanContractError("plan revision bundle validation failed")


def _validate_contract_pair(run_spec: dict[str, Any], revision: dict[str, Any]) -> None:
    """验证双合同摘要、全部共同 selector、无损 nodes/barriers 与 stage 引用。"""
    try:
        if run_spec.get("semanticPlanHash") != semantic_plan_hash(run_spec):
            _raise_invalid_bundle()
        if revision.get("planRevisionDigest") != plan_revision_digest(revision):
            _raise_invalid_bundle()

        work_plan = run_spec["workPlan"]
        parent_revision_id = run_spec["parentRevisionId"]
        revision_parent_present = "parentRevisionId" in revision
        revision_parent = revision.get("parentRevisionId")
        parent_matches = (parent_revision_id is None and not revision_parent_present) or (
            parent_revision_id is not None and revision_parent_present and revision_parent == parent_revision_id
        )

        shared_pairs = (
            (run_spec["taskId"], revision["taskId"]),
            (run_spec["specRevision"], revision["specRevision"]),
            (run_spec["intentAuthorizationId"], revision["intentAuthorizationId"]),
            (run_spec["semanticPlanHash"], revision["semanticPlanHash"]),
            (run_spec["planRevisionDigest"], revision["planRevisionDigest"]),
            (work_plan["dagVersion"], revision["dagVersion"]),
            (run_spec["nodeCapabilityMapVersion"], revision["nodeCapabilityMapVersion"]),
            (run_spec["stageCapabilityMapVersion"], revision["stageCapabilityMapVersion"]),
            (run_spec["createdAt"], revision["createdAt"]),
            (work_plan["nodes"], revision["nodes"]),
            (work_plan["barriers"], revision["barriers"]),
        )
        if not parent_matches or any(left != right for left, right in shared_pairs):
            _raise_invalid_bundle()

        node_ids = {node["logicalNodeId"] for node in revision["nodes"]}
        stage_maps = revision["stageMaps"]
        if any(node_id not in node_ids for stage_nodes in stage_maps.values() for node_id in stage_nodes):
            _raise_invalid_bundle()
    except PlanContractError:
        raise
    except Exception as exc:  # noqa: BLE001 - 对外只暴露稳定 bundle 错误。
        raise PlanContractError("plan revision bundle validation failed") from exc


@dataclass(frozen=True, slots=True)
class PlanRevisionBundle:
    """保留 RunSpec 与 PlanRevision 两份完整合同、独立 JCS bytes 及修订 selector。"""

    run_spec: FrozenDict
    plan_revision: FrozenDict
    canonical_run_spec: bytes
    canonical_plan_revision: bytes
    spec_revision: int

    @classmethod
    def from_contracts(
        cls,
        *,
        run_spec: Mapping[str, Any],
        plan_revision: Mapping[str, Any],
    ) -> PlanRevisionBundle:
        """从两份普通合同构建深不可变 bundle；任何 schema/hash/映射漂移均拒绝。"""
        if not isinstance(run_spec, dict) or not isinstance(plan_revision, dict):
            _raise_invalid_bundle()
        mutable_run_spec = deepcopy(run_spec)
        mutable_revision = deepcopy(plan_revision)
        _validate_contract_pair(mutable_run_spec, mutable_revision)
        try:
            canonical_run_spec = canonicalize(mutable_run_spec)
            canonical_plan_revision = canonicalize(mutable_revision)
            frozen_run_spec = _freeze_owned_json(mutable_run_spec)
            frozen_revision = _freeze_owned_json(mutable_revision)
        except Exception as exc:  # noqa: BLE001 - canonical/冻结细节收敛到 bundle 错误。
            raise PlanContractError("plan revision bundle validation failed") from exc
        return cls(
            run_spec=frozen_run_spec,
            plan_revision=frozen_revision,
            canonical_run_spec=canonical_run_spec,
            canonical_plan_revision=canonical_plan_revision,
            spec_revision=mutable_revision["specRevision"],
        )

    @classmethod
    def from_persistence_record(cls, record: Mapping[str, object]) -> PlanRevisionBundle:
        """从 SQLite selector 与双 canonical blobs 恢复，并逐项重验而非信任行数据。"""
        if not isinstance(record, Mapping) or frozenset(record) != _PERSISTENCE_FIELDS:
            raise PlanPersistenceError("PLAN_REVISION_PERSISTENCE_MISMATCH")
        if (
            record["run_spec_schema_id"] != _RUN_SPEC_SCHEMA_ID
            or record["run_spec_schema_version"] != _SCHEMA_VERSION
            or record["plan_revision_schema_id"] != _PLAN_REVISION_SCHEMA_ID
            or record["plan_revision_schema_version"] != _SCHEMA_VERSION
        ):
            raise PlanPersistenceError("UNSUPPORTED_PLAN_SCHEMA")

        run_spec = _parse_canonical_blob(record["canonical_run_spec"])
        revision = _parse_canonical_blob(record["canonical_plan_revision"])
        if not _PERSISTED_RUN_SPEC_FIELDS.issubset(run_spec):
            raise PlanPersistenceError("INCOMPLETE_PERSISTED_RUN_SPEC")
        try:
            bundle = cls.from_contracts(run_spec=run_spec, plan_revision=revision)
        except PlanContractError as exc:
            raise PlanPersistenceError("PLAN_REVISION_PERSISTENCE_MISMATCH") from exc

        selectors = {
            "plan_revision_id": revision["planRevisionId"],
            "task_id": revision["taskId"],
            "spec_revision": revision["specRevision"],
            "parent_revision_id": revision.get("parentRevisionId"),
            "intent_authorization_id": revision["intentAuthorizationId"],
            "semantic_plan_hash": revision["semanticPlanHash"],
            "plan_revision_digest": revision["planRevisionDigest"],
            "dag_version": revision["dagVersion"],
            "node_capability_map_version": revision["nodeCapabilityMapVersion"],
            "stage_capability_map_version": revision["stageCapabilityMapVersion"],
            "created_at": revision["createdAt"],
        }
        if any(record[name] != value for name, value in selectors.items()):
            raise PlanPersistenceError("PLAN_REVISION_PERSISTENCE_MISMATCH")
        return bundle


__all__ = [
    "PlanContractError",
    "PlanPersistenceError",
    "PlanRevisionBundle",
]
