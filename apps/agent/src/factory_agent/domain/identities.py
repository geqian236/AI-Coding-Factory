"""factory_agent.domain.identities — 关联 ID 类型与验证。

所有 Factory Agent 运行时 ID 均使用前缀_hex 格式：
  run_<16 hex>  task_<16 hex>  step_<16 hex>
  att_<16 hex>  trc_<16 hex>  req_<16 hex>

ID 由调用方生成（通常通过 generate_*），不在此层分配业务语义。
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from re import Pattern
from typing import ClassVar

from factory_agent.errors import IdentityFormatError

# 每种 ID 的正则：前缀 + 下划线 + 16 位十六进制
_HEX16 = r"[0-9a-f]{16}"
_PATTERNS: dict[str, Pattern[str]] = {
    "run":  re.compile(rf"^run_{_HEX16}$"),
    "task": re.compile(rf"^task_{_HEX16}$"),
    "step": re.compile(rf"^step_{_HEX16}$"),
    "att":  re.compile(rf"^att_{_HEX16}$"),
    "trc":  re.compile(rf"^trc_{_HEX16}$"),
    "req":  re.compile(rf"^req_{_HEX16}$"),
}


def _validate(prefix: str, value: str) -> str:
    """验证 ID 格式，不合规则抛出 IdentityFormatError。

    Args:
        prefix: ID 前缀（如 "run"、"task"）。
        value:  待验证的 ID 字符串。

    Returns:
        原始 value（已通过验证）。

    Raises:
        IdentityFormatError: 格式不符合规范。
    """
    pat = _PATTERNS.get(prefix)
    if pat is None or not pat.match(value):
        raise IdentityFormatError(
            f"Invalid {prefix} ID format: expected '{prefix}_<16hex>', got '{value[:40]}'"
        )
    return value


def _generate(prefix: str) -> str:
    """生成新的随机 ID。

    Args:
        prefix: ID 前缀。

    Returns:
        格式为 "{prefix}_{16 hex chars}" 的随机 ID。
    """
    return f"{prefix}_{secrets.token_hex(8)}"


@dataclass(frozen=True)
class RunId:
    """运行级别 ID，格式 run_<16hex>。"""

    PREFIX: ClassVar[str] = "run"
    value: str

    def __post_init__(self) -> None:
        """初始化后校验格式。"""
        _validate(self.PREFIX, self.value)

    @classmethod
    def generate(cls) -> RunId:
        """生成新的随机 RunId。"""
        return cls(_generate(cls.PREFIX))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class TaskId:
    """任务级别 ID，格式 task_<16hex>。"""

    PREFIX: ClassVar[str] = "task"
    value: str

    def __post_init__(self) -> None:
        """初始化后校验格式。"""
        _validate(self.PREFIX, self.value)

    @classmethod
    def generate(cls) -> TaskId:
        """生成新的随机 TaskId。"""
        return cls(_generate(cls.PREFIX))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class StepId:
    """步骤级别 ID，格式 step_<16hex>。"""

    PREFIX: ClassVar[str] = "step"
    value: str

    def __post_init__(self) -> None:
        """初始化后校验格式。"""
        _validate(self.PREFIX, self.value)

    @classmethod
    def generate(cls) -> StepId:
        """生成新的随机 StepId。"""
        return cls(_generate(cls.PREFIX))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class AttemptId:
    """重试尝试 ID，格式 att_<16hex>。"""

    PREFIX: ClassVar[str] = "att"
    value: str

    def __post_init__(self) -> None:
        """初始化后校验格式。"""
        _validate(self.PREFIX, self.value)

    @classmethod
    def generate(cls) -> AttemptId:
        """生成新的随机 AttemptId。"""
        return cls(_generate(cls.PREFIX))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class TraceId:
    """分布式追踪 ID，格式 trc_<16hex>。"""

    PREFIX: ClassVar[str] = "trc"
    value: str

    def __post_init__(self) -> None:
        """初始化后校验格式。"""
        _validate(self.PREFIX, self.value)

    @classmethod
    def generate(cls) -> TraceId:
        """生成新的随机 TraceId。"""
        return cls(_generate(cls.PREFIX))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class RequestId:
    """入站请求 ID，格式 req_<16hex>。"""

    PREFIX: ClassVar[str] = "req"
    value: str

    def __post_init__(self) -> None:
        """初始化后校验格式。"""
        _validate(self.PREFIX, self.value)

    @classmethod
    def generate(cls) -> RequestId:
        """生成新的随机 RequestId。"""
        return cls(_generate(cls.PREFIX))

    def __str__(self) -> str:
        return self.value
