"""控制命令幂等接受、Run/Attempt CAS 与追加 receipt 应用服务。"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from factory_agent.application.transition_service import (
    TransactionCoordinator,
    append_authoritative_state_event,
)
from factory_agent.domain.control import ControlCommand, ControlCommandReceiptPhase, ControlCommandType
from factory_agent.domain.workflow import Attempt, Run, RunDesiredState, RunObservedState
from factory_agent.errors import FactoryError
from factory_agent.observability.logging import get_logger
from factory_agent.state_machine.transitions import require_desired_transition
from factory_agent.storage.sqlite.control_repository import ControlReceiptEvent, SqliteControlRepository
from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork
from factory_agent.storage.sqlite.workflow_repository import SqliteWorkflowRepository, WorkflowRepositoryError

LOGGER = get_logger(__name__)
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_BLOCKING_COMMANDS = frozenset(
    {ControlCommandType.SOFT_PAUSE, ControlCommandType.IMMEDIATE_STOP, ControlCommandType.CANCEL}
)


class ControlRequestError(FactoryError):
    """命令输入形状、identity 或状态前置条件非法时抛出。"""

    error_code = "INVALID_CONTROL_REQUEST"


class ControlRequestConflictError(FactoryError):
    """同一 requestId 被不同命令语义复用时抛出。"""

    error_code = "CONTROL_REQUEST_CONFLICT"


class ControlCommandNotFoundError(FactoryError):
    """追加 receipt 时找不到已接受命令。"""

    error_code = "CONTROL_COMMAND_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class ControlCommandRequest:
    """控制入口的 exact semantic 输入；reason 只接受摘要，不接收原文。"""

    request_id: str
    run_id: str
    command_type: ControlCommandType
    actor_id: str
    expected_state_version: int
    reason_digest: str

    def __post_init__(self) -> None:
        """在进入日志和数据库前校验闭集、版本与摘要。"""
        object.__setattr__(self, "command_type", ControlCommandType(self.command_type))
        if (
            any(not isinstance(value, str) or not value for value in (self.request_id, self.run_id, self.actor_id))
            or type(self.expected_state_version) is not int
            or self.expected_state_version < 0
            or not isinstance(self.reason_digest, str)
            or _SHA256.fullmatch(self.reason_digest) is None
        ):
            raise ControlRequestError("control request shape is invalid")


@dataclass(frozen=True, slots=True)
class ControlAcceptance:
    """命令接受结果；幂等重放返回原 command/ACK receipt identity。"""

    command: ControlCommand
    receipt: ControlReceiptEvent
    idempotent_replay: bool


def _desired_target(command_type: ControlCommandType) -> RunDesiredState:
    """把控制命令机械映射到 desired_state，绝不映射 observed_state。"""
    if command_type is ControlCommandType.RESUME:
        return RunDesiredState.RUNNING
    if command_type is ControlCommandType.CANCEL:
        return RunDesiredState.CANCELLED
    return RunDesiredState.PAUSED


def _same_request(command: ControlCommand, request: ControlCommandRequest) -> bool:
    """判断 requestId 重放是否逐字段等价。"""
    return (
        command.run_id == request.run_id
        and command.command_type is request.command_type
        and command.actor_id == request.actor_id
        and command.expected_state_version == request.expected_state_version
        and command.reason_digest == request.reason_digest
    )


class ControlService:
    """通过唯一 writer 接受命令；后续完成/失败只追加 receipt。"""

    def __init__(
        self,
        *,
        coordinator: TransactionCoordinator,
        now: Callable[[], datetime],
        command_id_factory: Callable[[], str],
        receipt_id_factory: Callable[[], str],
        state_event_id_factory: Callable[[], str],
    ) -> None:
        self._coordinator = coordinator
        self._now = now
        self._command_id_factory = command_id_factory
        self._receipt_id_factory = receipt_id_factory
        self._state_event_id_factory = state_event_id_factory

    def _append_run_event(
        self,
        unit_of_work: SqliteUnitOfWork,
        *,
        previous: Run,
        updated: Run,
    ) -> None:
        """登记 Run desired/control seq 的权威状态事件。"""
        append_authoritative_state_event(
            unit_of_work,
            state_event_id=self._state_event_id_factory(),
            aggregate_type="RUN",
            aggregate_id=updated.run_id,
            task_id=updated.task_id,
            run_id=updated.run_id,
            step_id=None,
            attempt_id=None,
            previous_state_version=previous.state_version,
            state_version=updated.state_version,
            payload={
                "desiredState": updated.desired_state.value,
                "controlCommandSeq": updated.control_command_seq,
            },
        )

    def _append_attempt_event(
        self,
        unit_of_work: SqliteUnitOfWork,
        *,
        run: Run,
        previous: Attempt,
        updated: Attempt,
    ) -> None:
        """登记阻断命令设置的 DRAINING latch，不包含进程或用户原文。"""
        append_authoritative_state_event(
            unit_of_work,
            state_event_id=self._state_event_id_factory(),
            aggregate_type="ATTEMPT",
            aggregate_id=updated.attempt_id,
            task_id=run.task_id,
            run_id=run.run_id,
            step_id=updated.step_id,
            attempt_id=updated.attempt_id,
            previous_state_version=previous.state_version,
            state_version=updated.state_version,
            payload={"drainState": updated.drain_state.value, "interruptCommandId": updated.interrupt_command_id},
        )

    def _accept(self, unit_of_work: SqliteUnitOfWork, request: ControlCommandRequest) -> ControlAcceptance:
        """在一个同步 UoW callback 内接受或幂等读取命令。"""
        workflow = SqliteWorkflowRepository(unit_of_work)
        controls = SqliteControlRepository(unit_of_work)
        existing = controls.get_by_request_id(request.request_id)
        if existing is not None:
            if not _same_request(existing, request):
                raise ControlRequestConflictError("request id already belongs to another command")
            receipts = controls.list_receipts(existing.command_id)
            if not receipts:
                raise ControlRequestError("accepted command is missing its acknowledgement receipt")
            return ControlAcceptance(command=existing, receipt=receipts[0], idempotent_replay=True)

        run = workflow.get_run(request.run_id)
        if run is None:
            raise WorkflowRepositoryError("run does not exist")
        desired = _desired_target(request.command_type)
        attempt = (
            workflow.get_active_attempt_for_run(run.run_id) if request.command_type in _BLOCKING_COMMANDS else None
        )
        immediate_stop_noop = (
            request.command_type is ControlCommandType.IMMEDIATE_STOP
            and run.desired_state is RunDesiredState.PAUSED
            and run.observed_state is RunObservedState.PAUSED
            and attempt is None
        )
        immediate_stop_escalation = (
            request.command_type is ControlCommandType.IMMEDIATE_STOP
            and run.desired_state is RunDesiredState.PAUSED
            and attempt is not None
        )
        immediate_stop_completed_without_executor = (
            request.command_type is ControlCommandType.IMMEDIATE_STOP
            and attempt is None
            and run.observed_state in {RunObservedState.PAUSED, RunObservedState.QUEUED}
        )
        if not immediate_stop_noop and not immediate_stop_escalation:
            require_desired_transition(run.desired_state, desired)
        issued_at = self._now().isoformat()
        command = ControlCommand(
            command_id=self._command_id_factory(),
            run_id=run.run_id,
            command_seq=run.control_command_seq + 1,
            request_id=request.request_id,
            command_type=request.command_type,
            actor_id=request.actor_id,
            expected_state_version=request.expected_state_version,
            accepted_state_version=request.expected_state_version + 1,
            issued_at=issued_at,
            acknowledged_attempt_id=None if attempt is None else attempt.attempt_id,
            reason_digest=request.reason_digest,
        )
        controls.append_command(command)
        updated_run = workflow.update_run_control(
            run_id=run.run_id,
            expected_state_version=request.expected_state_version,
            desired_state=desired,
            control_command_seq=command.command_seq,
        )
        self._append_run_event(unit_of_work, previous=run, updated=updated_run)
        if attempt is not None:
            updated_attempt = workflow.update_attempt_drain(
                attempt_id=attempt.attempt_id,
                expected_state_version=attempt.state_version,
                command_id=command.command_id,
            )
            self._append_attempt_event(
                unit_of_work,
                run=updated_run,
                previous=attempt,
                updated=updated_attempt,
            )
        receipt = controls.append_receipt(
            receipt_id=self._receipt_id_factory(),
            command_id=command.command_id,
            phase=ControlCommandReceiptPhase.ACKNOWLEDGED,
            attempt_id=command.acknowledged_attempt_id,
            state_event_id=None,
            evidence_digest=None,
            created_at=issued_at,
        )
        if immediate_stop_completed_without_executor:
            # PAUSED/QUEUED 且无 Attempt 时没有 executor 动作，完成事实仍以追加事件表达。
            controls.append_receipt(
                receipt_id=self._receipt_id_factory(),
                command_id=command.command_id,
                phase=ControlCommandReceiptPhase.COMPLETED,
                attempt_id=None,
                state_event_id=None,
                evidence_digest=None,
                created_at=issued_at,
            )
        return ControlAcceptance(command=command, receipt=receipt, idempotent_replay=False)

    async def submit(self, request: ControlCommandRequest) -> ControlAcceptance:
        """通过单写 coordinator 接受控制命令并记录脱敏结构化日志。"""
        started = time.monotonic()
        LOGGER.info(
            "control_command_accept_started",
            request_id=request.request_id,
            run_id=request.run_id,
            operation="control_command_accept",
            status="started",
            duration_ms=0,
        )
        try:
            acceptance = await self._coordinator.execute(
                operation="control_command_accept",
                context={"request_id": request.request_id, "run_id": request.run_id},
                command=lambda unit_of_work: self._accept(unit_of_work, request),
            )
        except Exception as exc:
            LOGGER.warning(
                "control_command_accept_rejected",
                request_id=request.request_id,
                run_id=request.run_id,
                operation="control_command_accept",
                status="rejected",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                error_code=getattr(exc, "error_code", type(exc).__name__),
            )
            raise
        LOGGER.info(
            "control_command_accept_committed",
            request_id=request.request_id,
            run_id=request.run_id,
            operation="control_command_accept",
            status="committed",
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            command_seq=acceptance.command.command_seq,
            idempotent_replay=acceptance.idempotent_replay,
        )
        return acceptance

    async def append_receipt(
        self,
        *,
        command_id: str,
        phase: ControlCommandReceiptPhase,
        attempt_id: str | None,
        evidence_digest: str | None,
    ) -> ControlReceiptEvent:
        """追加 COMPLETED/FAILED receipt；不修改原命令或既有 receipt。"""
        started = time.monotonic()
        receipt_phase = ControlCommandReceiptPhase(phase)
        if receipt_phase is ControlCommandReceiptPhase.ACKNOWLEDGED:
            raise ControlRequestError("acknowledgement is created only by command acceptance")
        if evidence_digest is not None and _SHA256.fullmatch(evidence_digest) is None:
            raise ControlRequestError("receipt evidence digest is invalid")

        def command(unit_of_work: SqliteUnitOfWork) -> ControlReceiptEvent:
            controls = SqliteControlRepository(unit_of_work)
            accepted = controls.get_command(command_id)
            if accepted is None:
                raise ControlCommandNotFoundError("control command does not exist")
            return controls.append_receipt(
                receipt_id=self._receipt_id_factory(),
                command_id=command_id,
                phase=receipt_phase,
                attempt_id=attempt_id,
                state_event_id=None,
                evidence_digest=evidence_digest,
                created_at=self._now().isoformat(),
            )

        try:
            receipt = await self._coordinator.execute(
                operation="control_receipt_append",
                context={"request_id": None, "command_id": command_id},
                command=command,
            )
        except Exception as exc:
            LOGGER.warning(
                "control_receipt_append_rejected",
                command_id=command_id,
                attempt_id=attempt_id,
                operation="control_receipt_append",
                status="rejected",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                error_code=getattr(exc, "error_code", type(exc).__name__),
            )
            raise
        LOGGER.info(
            "control_receipt_appended",
            command_id=command_id,
            attempt_id=attempt_id,
            operation="control_receipt_append",
            status="committed",
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            receipt_phase=receipt.phase.value,
            receipt_seq=receipt.receipt_seq,
        )
        return receipt


__all__ = [
    "ControlAcceptance",
    "ControlCommandNotFoundError",
    "ControlCommandRequest",
    "ControlRequestConflictError",
    "ControlRequestError",
    "ControlService",
]
