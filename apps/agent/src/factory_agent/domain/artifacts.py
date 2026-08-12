"""Artifact 的 Task 1 只读 selector 与 gate eligibility 边界。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from factory_agent.errors import FactoryError


class ArtifactCommitState(StrEnum):
    STAGING = "STAGING"
    COMMITTED = "COMMITTED"


class ArtifactContractError(FactoryError):
    """Artifact 字段、枚举或 committed identity 不完整时抛出。"""

    error_code = "INVALID_ARTIFACT_RECORD"


@dataclass(frozen=True, slots=True)
class Artifact:
    """Artifact selector 的不可变值对象，不承担对象存储 I/O。"""

    artifact_id: str
    producer_attempt_id: str
    media_type: str
    confidentiality: str
    commit_state: ArtifactCommitState
    storage_path: str | None
    size_bytes: int | None
    digest: str | None
    created_at: str

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Artifact:
        """按 exact-set 构造；COMMITTED artifact 必须具备完整 path/size/digest identity。"""
        expected = frozenset(cls.__dataclass_fields__)
        if not isinstance(record, Mapping) or frozenset(record) != expected:
            raise ArtifactContractError("artifact record fields do not match the frozen contract")
        values = dict(record)
        try:
            values["commit_state"] = ArtifactCommitState(values["commit_state"])
        except (TypeError, ValueError) as exc:
            raise ArtifactContractError("artifact commit state is invalid") from exc
        if values["commit_state"] is ArtifactCommitState.COMMITTED and any(
            values[name] is None for name in ("storage_path", "size_bytes", "digest")
        ):
            raise ArtifactContractError("committed artifact identity is incomplete")
        return cls(**values)

    @property
    def is_gate_eligible(self) -> bool:
        """仅完整 COMMITTED artifact 可满足 gate；STAGING 永不被误当作 durable。"""
        return self.commit_state is ArtifactCommitState.COMMITTED and all(
            value is not None for value in (self.storage_path, self.size_bytes, self.digest)
        )


__all__ = ["Artifact", "ArtifactCommitState", "ArtifactContractError"]
