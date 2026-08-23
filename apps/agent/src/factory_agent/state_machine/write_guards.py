"""Executor NORMAL_WRITE/DRAIN_WRITE 互斥谓词与操作白名单。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from factory_agent.domain.workflow import DrainState, RunDesiredState, RunObservedState
from factory_agent.errors import FactoryError


class WriteMode(StrEnum):
    """Executor 可进入的两种互斥写模式。"""

    NORMAL_WRITE = "NORMAL_WRITE"
    DRAIN_WRITE = "DRAIN_WRITE"


class WriteOperation(StrEnum):
    """状态机可机械授权的写操作闭集。"""

    TOOL_CALL = "TOOL_CALL"
    AUTHORIZATION_CONSUME = "AUTHORIZATION_CONSUME"
    SIDE_EFFECT = "SIDE_EFFECT"
    CANDIDATE_ARTIFACT = "CANDIDATE_ARTIFACT"
    OBSERVED_STATE = "OBSERVED_STATE"
    STEP_STATE = "STEP_STATE"
    ATTEMPT_STATE = "ATTEMPT_STATE"
    SANITIZED_STREAM_TAIL = "SANITIZED_STREAM_TAIL"
    SEMANTIC_TAIL = "SEMANTIC_TAIL"
    HIDDEN_CHECKPOINT = "HIDDEN_CHECKPOINT"
    ATTEMPT_TERMINATION = "ATTEMPT_TERMINATION"
    RECONCILIATION_RECEIPT = "RECONCILIATION_RECEIPT"


class WriteGuardError(FactoryError):
    """控制序号、fencing、epoch、版本或白名单不匹配时抛出。"""

    error_code = "WRITE_GUARD_REJECTED"


_DRAIN_OPERATIONS = frozenset(
    {
        WriteOperation.SANITIZED_STREAM_TAIL,
        WriteOperation.SEMANTIC_TAIL,
        WriteOperation.HIDDEN_CHECKPOINT,
        WriteOperation.ATTEMPT_TERMINATION,
        WriteOperation.RECONCILIATION_RECEIPT,
    }
)


@dataclass(frozen=True, slots=True)
class WriteGuardContext:
    """一次写入必须同时核对的 Run/Attempt/executor/fencing 快照。"""

    run_desired_state: RunDesiredState
    run_control_command_seq: int
    run_state_version: int
    expected_run_state_version: int
    attempt_id: str
    attempt_executor_id: str
    expected_executor_id: str
    attempt_accepted_control_command_seq: int
    attempt_fencing_token: int
    expected_fencing_token: int
    attempt_control_epoch: int
    expected_control_epoch: int
    attempt_drain_state: DrainState
    blocking_command_attempt_id: str | None
    lease_active: bool

    def __post_init__(self) -> None:
        """拒绝空 identity、负数与 bool/int 混淆，避免宽松比较绕过 fencing。"""
        try:
            # wire 枚举必须映射为统一写保护异常，避免调用方收到不稳定的原生 ValueError。
            object.__setattr__(self, "run_desired_state", RunDesiredState(self.run_desired_state))
            object.__setattr__(self, "attempt_drain_state", DrainState(self.attempt_drain_state))
        except (TypeError, ValueError) as exc:
            raise WriteGuardError("write guard enum is invalid") from exc
        numeric_fields = (
            self.run_control_command_seq,
            self.run_state_version,
            self.expected_run_state_version,
            self.attempt_accepted_control_command_seq,
            self.attempt_fencing_token,
            self.expected_fencing_token,
            self.attempt_control_epoch,
            self.expected_control_epoch,
        )
        if (
            not isinstance(self.attempt_id, str)
            or not self.attempt_id
            or not isinstance(self.attempt_executor_id, str)
            or not self.attempt_executor_id
            or not isinstance(self.expected_executor_id, str)
            or not self.expected_executor_id
            or type(self.lease_active) is not bool
            or any(type(value) is not int or value < 0 for value in numeric_fields)
        ):
            raise WriteGuardError("write guard context is invalid")


def _normal_write_allowed(context: WriteGuardContext) -> bool:
    """NORMAL_WRITE 绑定 executor、最新 control seq、token/epoch/version 与无 drain latch。"""
    return (
        context.run_desired_state is RunDesiredState.RUNNING
        and context.attempt_executor_id == context.expected_executor_id
        and context.attempt_accepted_control_command_seq == context.run_control_command_seq
        and context.attempt_fencing_token == context.expected_fencing_token
        and context.attempt_control_epoch == context.expected_control_epoch
        and context.run_state_version == context.expected_run_state_version
        and context.attempt_drain_state is DrainState.NONE
        and context.lease_active
    )


def _drain_write_allowed(context: WriteGuardContext) -> bool:
    """DRAIN_WRITE 绑定阻断命令锁定的同一 executor/Attempt，并保留 fencing/version 校验。"""
    return (
        context.blocking_command_attempt_id == context.attempt_id
        and context.attempt_executor_id == context.expected_executor_id
        and context.attempt_fencing_token == context.expected_fencing_token
        and context.attempt_control_epoch == context.expected_control_epoch
        and context.run_state_version == context.expected_run_state_version
        and context.attempt_drain_state is DrainState.DRAINING
        and context.lease_active
    )


def require_write(context: WriteGuardContext, operation: WriteOperation) -> WriteMode:
    """返回唯一允许模式；旧序号只能在 DRAIN 白名单内安全收尾。"""
    try:
        requested_operation = WriteOperation(operation)
    except (TypeError, ValueError) as exc:
        raise WriteGuardError("write operation is invalid") from exc
    if _normal_write_allowed(context):
        return WriteMode.NORMAL_WRITE
    if _drain_write_allowed(context) and requested_operation in _DRAIN_OPERATIONS:
        return WriteMode.DRAIN_WRITE
    raise WriteGuardError("executor write does not satisfy the frozen predicate")


def require_new_attempt_dispatch(
    *,
    observed_state: RunObservedState,
    new_attempt_id: str,
    previous_attempt_id: str | None,
) -> None:
    """新 Attempt 只可从 QUEUED 派发，且必须使用未复用的新 identity。"""
    try:
        requested_observed_state = RunObservedState(observed_state)
    except (TypeError, ValueError) as exc:
        raise WriteGuardError("observed state is invalid") from exc
    if (
        requested_observed_state is not RunObservedState.QUEUED
        or not isinstance(new_attempt_id, str)
        or not new_attempt_id
        or new_attempt_id == previous_attempt_id
    ):
        raise WriteGuardError("new attempt dispatch requires queued state and fresh identity")


__all__ = [
    "WriteGuardContext",
    "WriteGuardError",
    "WriteMode",
    "WriteOperation",
    "require_new_attempt_dispatch",
    "require_write",
]
