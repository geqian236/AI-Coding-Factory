"""快速 PAUSE→RESUME、DRAIN_WRITE 白名单与新 Attempt 边界测试。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from factory_agent.application.control_service import ControlCommandRequest
from factory_agent.application.transition_service import TransitionService
from factory_agent.domain.control import ControlCommandType
from factory_agent.domain.workflow import (
    Attempt,
    AttemptOutcome,
    AttemptPhase,
    DrainState,
    RunDesiredState,
    RunObservedState,
    StepPhase,
)
from factory_agent.state_machine.write_guards import (
    WriteGuardContext,
    WriteGuardError,
    WriteMode,
    WriteOperation,
    require_new_attempt_dispatch,
    require_write,
)
from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork
from factory_agent.storage.sqlite.workflow_repository import SqliteWorkflowRepository, WorkflowRepositoryError

from tests.agent.integration.control.test_control_commands import (
    SHA_A,
    _connection,
    _derived_barrier_id,
    _ids,
    _insert,
    _insert_plan_revision_for_barrier,
    _selector_digest,
    _service,
    _SqliteCoordinator,
)


def _guard_context(**overrides: object) -> WriteGuardContext:
    """构造快速恢复后旧 Attempt 的 fencing/control 事实。"""
    values: dict[str, object] = {
        "run_desired_state": RunDesiredState.RUNNING,
        "run_control_command_seq": 2,
        "run_state_version": 2,
        "expected_run_state_version": 2,
        "attempt_id": "attempt-control-1",
        "attempt_executor_id": "executor-control-1",
        "expected_executor_id": "executor-control-1",
        "attempt_accepted_control_command_seq": 0,
        "attempt_fencing_token": 7,
        "expected_fencing_token": 7,
        "attempt_control_epoch": 9,
        "expected_control_epoch": 9,
        "attempt_drain_state": DrainState.DRAINING,
        "blocking_command_attempt_id": "attempt-control-1",
    }
    values.update(overrides)
    return WriteGuardContext(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["run_desired_state", "attempt_drain_state"])
def test_write_guard_context_invalid_enum_maps_to_factory_error(field: str) -> None:
    """写保护上下文不能把未知 wire 枚举泄漏为原生 ValueError。"""
    with pytest.raises(WriteGuardError) as caught:
        _guard_context(**{field: "NOT_A_FROZEN_ENUM"})

    assert caught.value.error_code == "WRITE_GUARD_REJECTED"


@pytest.mark.parametrize("invalid_operation", ["NOT_A_WRITE_OPERATION", ""])
def test_require_write_invalid_operation_maps_to_factory_error(invalid_operation: str) -> None:
    """写操作入口必须把非法枚举映射为稳定错误码。"""
    with pytest.raises(WriteGuardError) as caught:
        require_write(_guard_context(), invalid_operation)  # type: ignore[arg-type]

    assert caught.value.error_code == "WRITE_GUARD_REJECTED"


@pytest.mark.parametrize("invalid_observed_state", ["NOT_A_RUN_STATE", ""])
def test_require_new_attempt_dispatch_invalid_observed_maps_to_factory_error(
    invalid_observed_state: str,
) -> None:
    """新 Attempt 派发入口必须把非法 observed wire 值映射为稳定错误码。"""
    with pytest.raises(WriteGuardError) as caught:
        require_new_attempt_dispatch(
            observed_state=invalid_observed_state,  # type: ignore[arg-type]
            new_attempt_id="attempt-new",
            previous_attempt_id="attempt-old",
        )

    assert caught.value.error_code == "WRITE_GUARD_REJECTED"


def _insert_attempt_graph(connection: object) -> None:
    """在 FK-on migration schema 中插入完整 Plan/Barrier/Step/Attempt lineage。"""
    required_node_set_digest = _insert_plan_revision_for_barrier(
        connection,  # type: ignore[arg-type]
        required_node_ids=("implement",),
    )
    barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
    _insert(
        connection,  # type: ignore[arg-type]
        "phase_barriers",
        {
            "barrier_id": barrier_id,
            "run_id": "run-control-1",
            "plan_revision_id": "plan-control-1",
            "business_phase": "PLANNING",
            "barrier_ordinal": 0,
            "required_node_set_digest": required_node_set_digest,
            "settle_timeout_ms": 30_000,
            "settle_deadline_at": None,
            "pass_predicate_id": "planning-approved-v1",
            "settled": 0,
            "passed": 0,
            "gate_digest": None,
            "state_version": 0,
        },
    )
    _insert(
        connection,  # type: ignore[arg-type]
        "steps",
        {
            "step_id": "step-control-1",
            "run_id": "run-control-1",
            "plan_revision_id": "plan-control-1",
            "barrier_id": barrier_id,
            "logical_node_id": "implement",
            "business_phase": "IMPLEMENTING",
            "node_type": "IMPLEMENT",
            "required": 1,
            "side_effect_class": "workspace_write",
            "phase": "RUNNING",
            "outcome": "NONE",
            "dependency_hash": _selector_digest([]),
            "required_artifacts_digest": _selector_digest([]),
            "success_predicate_id": "implementation-complete-v1",
            "timeout_ms": 30_000,
            "retry_policy_id": "no-retry-v1",
            "idempotency_key": "step-control-1-v1",
            "state_version": 0,
        },
    )
    _insert(
        connection,  # type: ignore[arg-type]
        "attempts",
        {
            "attempt_id": "attempt-control-1",
            "step_id": "step-control-1",
            "supersedes_attempt_id": None,
            "phase": "RUNNING",
            "outcome": "NONE",
            "executor_id": "executor-control-1",
            "process_session_id": "process-session-1",
            "pid": 1234,
            "process_start_time": "2026-08-14T07:59:00Z",
            "job_object_id": "job-control-1",
            "wsl_distro": None,
            "container_id": None,
            "image_digest": None,
            "exit_code": None,
            "termination_reason": None,
            "fencing_token": 7,
            "control_epoch": 9,
            "accepted_control_command_seq": 0,
            "interrupt_command_id": None,
            "drain_state": "NONE",
            "started_at": "2026-08-14T07:59:00Z",
            "ended_at": None,
            "state_version": 0,
        },
    )


def _attempt_record(attempt: Attempt) -> dict[str, object]:
    """把不可变 Attempt 转成持久投影，供损坏/竞争事实测试精确造数。"""
    return {
        field: getattr(attempt, field).value if hasattr(getattr(attempt, field), "value") else getattr(attempt, field)
        for field in Attempt.__dataclass_fields__
    }


def _prepare_drained_predecessor(connection: object) -> tuple[Attempt, Attempt]:
    """准备已收尾 predecessor 与合法 fresh successor，避免重放用例重复造图。"""
    _insert_attempt_graph(connection)
    connection.execute(  # type: ignore[attr-defined]
        "UPDATE attempts SET phase='TERMINATED', outcome='KILLED', drain_state='DRAINED', "
        "ended_at='2026-08-17T09:00:00Z' WHERE attempt_id='attempt-control-1'"
    )
    connection.execute("UPDATE runs SET control_command_seq=1")  # type: ignore[attr-defined]
    repository = SqliteWorkflowRepository(
        SqliteUnitOfWork(connection, failure_probe=lambda _point: None)  # type: ignore[arg-type]
    )
    previous = repository.get_attempt("attempt-control-1")
    assert previous is not None
    candidate = previous.supersede(
        new_attempt_id="attempt-control-2",
        fencing_token=8,
        control_epoch=10,
        accepted_control_command_seq=1,
    )
    return previous, candidate


def _commit_superseding_attempt(connection: object, previous: Attempt, candidate: Attempt) -> None:
    """在 BEGIN IMMEDIATE owner transaction 内提交 scheduler 后继写。"""
    unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)  # type: ignore[arg-type]
    unit_of_work.begin_immediate()
    try:
        SqliteWorkflowRepository(unit_of_work).append_superseding_attempt(previous, candidate)
        unit_of_work.precommit()
        unit_of_work.commit()
    except BaseException:
        unit_of_work.rollback()
        raise


@pytest.mark.asyncio
async def test_fast_pause_resume_keeps_old_attempt_draining() -> None:
    """RESUME 只推进 desired/seq，旧 Attempt 的 drain latch 与阻断命令保持不变。"""
    connection = _connection(desired_state="RUNNING", observed_state="RUNNING")
    try:
        _insert_attempt_graph(connection)
        service = _service(connection)
        paused = await service.submit(
            ControlCommandRequest(
                request_id="request-fast-pause",
                run_id="run-control-1",
                command_type=ControlCommandType.SOFT_PAUSE,
                actor_id="actor-control-1",
                expected_state_version=0,
                reason_digest=SHA_A,
            )
        )
        resumed = await service.submit(
            ControlCommandRequest(
                request_id="request-fast-resume",
                run_id="run-control-1",
                command_type=ControlCommandType.RESUME,
                actor_id="actor-control-1",
                expected_state_version=1,
                reason_digest=SHA_A,
            )
        )

        assert paused.command.acknowledged_attempt_id == "attempt-control-1"
        assert resumed.command.acknowledged_attempt_id is None
        assert connection.execute(
            "SELECT desired_state,observed_state,control_command_seq,state_version FROM runs"
        ).fetchone() == ("RUNNING", "RUNNING", 2, 2)
        assert connection.execute(
            "SELECT drain_state,interrupt_command_id,accepted_control_command_seq,state_version FROM attempts"
        ).fetchone() == ("DRAINING", paused.command.command_id, 0, 1)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_immediate_stop_escalates_soft_pause_on_same_active_attempt() -> None:
    """SOFT_PAUSE 后的 IMMEDIATE_STOP 必须追加新序号并把同一活动 Attempt 锁给新命令。"""
    connection = _connection(desired_state="RUNNING", observed_state="RUNNING")
    try:
        _insert_attempt_graph(connection)
        service = _service(connection)
        paused = await service.submit(
            ControlCommandRequest(
                request_id="request-pause-before-stop",
                run_id="run-control-1",
                command_type=ControlCommandType.SOFT_PAUSE,
                actor_id="actor-control-1",
                expected_state_version=0,
                reason_digest=SHA_A,
            )
        )
        stopped = await service.submit(
            ControlCommandRequest(
                request_id="request-stop-after-pause",
                run_id="run-control-1",
                command_type=ControlCommandType.IMMEDIATE_STOP,
                actor_id="actor-control-1",
                expected_state_version=1,
                reason_digest=SHA_A,
            )
        )

        assert paused.command.acknowledged_attempt_id == "attempt-control-1"
        assert stopped.command.acknowledged_attempt_id == "attempt-control-1"
        assert connection.execute("SELECT desired_state,control_command_seq,state_version FROM runs").fetchone() == (
            "PAUSED",
            2,
            2,
        )
        assert connection.execute("SELECT drain_state,interrupt_command_id,state_version FROM attempts").fetchone() == (
            "DRAINING",
            stopped.command.command_id,
            2,
        )
    finally:
        connection.close()


def test_superseding_attempt_rejects_running_candidate_before_insert() -> None:
    """Queued 派发只能构造 fresh CREATED Attempt，不能把 RUNNING 假事实直接写入。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_attempt_graph(connection)
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            repository = SqliteWorkflowRepository(unit_of_work)
            previous = repository.get_attempt("attempt-control-1")
            assert previous is not None
            candidate = replace(
                previous,
                attempt_id="attempt-control-2",
                supersedes_attempt_id=previous.attempt_id,
                phase=AttemptPhase.RUNNING,
                state_version=0,
            )
            with pytest.raises(WorkflowRepositoryError):
                repository.append_superseding_attempt(previous, candidate)
            unit_of_work.rollback()
        except BaseException:
            if connection.in_transaction:
                unit_of_work.rollback()
            raise
        assert connection.execute("SELECT count(*) FROM attempts").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.parametrize("malformed_field", ["supersedes_attempt_id", "phase", "fencing_token", "control_seq"])
def test_superseding_attempt_rejects_malformed_authoritative_candidate(malformed_field: str) -> None:
    """后继 Attempt 必须由 previous.supersede 和 Run control seq 共同构造。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_attempt_graph(connection)
        connection.execute(
            "UPDATE attempts SET phase='TERMINATED', outcome='KILLED', drain_state='DRAINED', "
            "ended_at='2026-08-17T09:00:00Z' WHERE attempt_id='attempt-control-1'"
        )
        connection.execute("UPDATE runs SET control_command_seq=1")
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            repository = SqliteWorkflowRepository(unit_of_work)
            previous = repository.get_attempt("attempt-control-1")
            assert previous is not None
            candidate = previous.supersede(
                new_attempt_id="attempt-control-2",
                fencing_token=8,
                control_epoch=10,
                accepted_control_command_seq=1,
            )
            if malformed_field == "supersedes_attempt_id":
                candidate = replace(candidate, supersedes_attempt_id=None)
            elif malformed_field == "phase":
                candidate = replace(candidate, phase=AttemptPhase.RUNNING)
            elif malformed_field == "fencing_token":
                candidate = replace(candidate, fencing_token=previous.fencing_token)
            else:
                candidate = replace(candidate, accepted_control_command_seq=previous.accepted_control_command_seq)
            with pytest.raises(WorkflowRepositoryError):
                repository.append_superseding_attempt(previous, candidate)
            unit_of_work.rollback()
        except BaseException:
            if connection.in_transaction:
                unit_of_work.rollback()
            raise
        assert connection.execute("SELECT count(*) FROM attempts").fetchone() == (1,)
    finally:
        connection.close()


def test_superseding_attempt_exact_candidate_replay_is_idempotent() -> None:
    """相同 predecessor/candidate 重放必须成功且不能重复插入。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        previous, candidate = _prepare_drained_predecessor(connection)

        _commit_superseding_attempt(connection, previous, candidate)
        _commit_superseding_attempt(connection, previous, candidate)

        assert connection.execute(
            "SELECT attempt_id,supersedes_attempt_id,phase,fencing_token,control_epoch,"
            "accepted_control_command_seq FROM attempts ORDER BY attempt_id"
        ).fetchall() == [
            ("attempt-control-1", None, "TERMINATED", 7, 9, 0),
            ("attempt-control-2", "attempt-control-1", "CREATED", 8, 10, 1),
        ]
    finally:
        connection.close()


def test_superseding_attempt_rejects_different_direct_successor() -> None:
    """predecessor 已有直接后继时，任何不同 candidate 都必须歧义拒绝。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        previous, candidate = _prepare_drained_predecessor(connection)
        _commit_superseding_attempt(connection, previous, candidate)
        conflicting = previous.supersede(
            new_attempt_id="attempt-control-3",
            fencing_token=9,
            control_epoch=11,
            accepted_control_command_seq=1,
        )

        with pytest.raises(WorkflowRepositoryError, match="different successor"):
            _commit_superseding_attempt(connection, previous, conflicting)

        assert connection.execute("SELECT count(*) FROM attempts").fetchone() == (2,)
    finally:
        connection.close()


def test_superseding_attempt_rejects_ambiguous_multiple_direct_successors() -> None:
    """历史损坏为多个直接后继时，不能把任一行误判成精确幂等重放。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        previous, candidate = _prepare_drained_predecessor(connection)
        _insert(connection, "attempts", _attempt_record(candidate))  # type: ignore[arg-type]
        _insert(
            connection,  # type: ignore[arg-type]
            "attempts",
            _attempt_record(
                replace(
                    candidate,
                    attempt_id="attempt-control-3",
                    fencing_token=9,
                    control_epoch=11,
                )
            ),
        )

        with pytest.raises(WorkflowRepositoryError, match="multiple direct successors"):
            _commit_superseding_attempt(connection, previous, candidate)

        assert connection.execute("SELECT count(*) FROM attempts").fetchone() == (3,)
    finally:
        connection.close()


def test_superseding_attempt_rejects_other_active_attempt_for_run() -> None:
    """无直接后继时，同一 Run 的任一其他活动 Attempt 都阻断新派发。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        previous, candidate = _prepare_drained_predecessor(connection)
        unrelated = replace(
            candidate,
            attempt_id="attempt-control-unrelated",
            supersedes_attempt_id=None,
        )
        _insert(connection, "attempts", _attempt_record(unrelated))  # type: ignore[arg-type]

        with pytest.raises(WorkflowRepositoryError, match="another active attempt"):
            _commit_superseding_attempt(connection, previous, candidate)

        assert connection.execute("SELECT count(*) FROM attempts").fetchone() == (2,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_guarded_step_state_commits_projection_and_paired_event() -> None:
    """NORMAL_WRITE Step CAS 与 authoritative event 必须在同一 UoW 成对提交。"""
    connection = _connection(desired_state="RUNNING", observed_state="RUNNING")
    try:
        _insert_attempt_graph(connection)
        updated = await TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-step-write"),
        ).transition_executor_step_state(
            request_id="request-step-write",
            run_id="run-control-1",
            step_id="step-control-1",
            attempt_id="attempt-control-1",
            expected_run_state_version=0,
            expected_step_state_version=0,
            executor_id="executor-control-1",
            fencing_token=7,
            control_epoch=9,
            candidate=StepPhase.RECONCILING,
        )

        assert (updated.phase, updated.state_version) == (StepPhase.RECONCILING, 1)
        assert connection.execute("SELECT phase,state_version FROM steps").fetchone() == ("RECONCILING", 1)
        assert connection.execute(
            "SELECT aggregate_type,aggregate_id,run_id,step_id,attempt_id,"
            "previous_state_version,state_version FROM authoritative_state_events"
        ).fetchone() == (
            "STEP",
            "step-control-1",
            "run-control-1",
            "step-control-1",
            None,
            0,
            1,
        )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_guarded_attempt_state_commits_projection_and_paired_event() -> None:
    """NORMAL_WRITE Attempt CAS 与 authoritative event 必须共享事务和精确 lineage。"""
    connection = _connection(desired_state="RUNNING", observed_state="RUNNING")
    try:
        _insert_attempt_graph(connection)
        updated = await TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-attempt-write"),
        ).transition_executor_attempt_state(
            request_id="request-attempt-write",
            run_id="run-control-1",
            attempt_id="attempt-control-1",
            expected_run_state_version=0,
            expected_attempt_state_version=0,
            executor_id="executor-control-1",
            fencing_token=7,
            control_epoch=9,
            candidate=AttemptPhase.RECONCILING,
        )

        assert (updated.phase, updated.state_version) == (AttemptPhase.RECONCILING, 1)
        assert connection.execute("SELECT phase,state_version FROM attempts").fetchone() == ("RECONCILING", 1)
        assert connection.execute(
            "SELECT aggregate_type,aggregate_id,run_id,step_id,attempt_id,"
            "previous_state_version,state_version FROM authoritative_state_events"
        ).fetchone() == (
            "ATTEMPT",
            "attempt-control-1",
            "run-control-1",
            "step-control-1",
            "attempt-control-1",
            0,
            1,
        )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_drain_guarded_writes_only_allow_attempt_termination() -> None:
    """DRAIN_WRITE 拒绝普通 Step/Attempt CAS，只允许 Attempt 原子收尾并配对事件。"""
    connection = _connection(desired_state="RUNNING", observed_state="RUNNING")
    try:
        _insert_attempt_graph(connection)
        paused = await _service(connection).submit(
            ControlCommandRequest(
                request_id="request-drain-guarded-writes",
                run_id="run-control-1",
                command_type=ControlCommandType.SOFT_PAUSE,
                actor_id="actor-control-1",
                expected_state_version=0,
                reason_digest=SHA_A,
            )
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-drain-write"),
        )
        event_count = connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone()[0]

        with pytest.raises(WriteGuardError):
            await service.transition_executor_step_state(
                request_id="request-drain-step-write",
                run_id="run-control-1",
                step_id="step-control-1",
                attempt_id="attempt-control-1",
                expected_run_state_version=1,
                expected_step_state_version=0,
                executor_id="executor-control-1",
                fencing_token=7,
                control_epoch=9,
                candidate=StepPhase.RECONCILING,
            )
        with pytest.raises(WriteGuardError):
            await service.transition_executor_attempt_state(
                request_id="request-drain-attempt-write",
                run_id="run-control-1",
                attempt_id="attempt-control-1",
                expected_run_state_version=1,
                expected_attempt_state_version=1,
                executor_id="executor-control-1",
                fencing_token=7,
                control_epoch=9,
                candidate=AttemptPhase.RECONCILING,
            )
        assert connection.execute("SELECT phase,state_version FROM steps").fetchone() == ("RUNNING", 0)
        assert connection.execute("SELECT phase,drain_state,state_version FROM attempts").fetchone() == (
            "RUNNING",
            "DRAINING",
            1,
        )
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (event_count,)

        terminated = await service.terminate_executor_attempt(
            request_id="request-drain-attempt-termination",
            run_id="run-control-1",
            attempt_id="attempt-control-1",
            expected_run_state_version=1,
            expected_attempt_state_version=1,
            executor_id="executor-control-1",
            fencing_token=7,
            control_epoch=9,
            outcome=AttemptOutcome.INTERRUPTED,
            ended_at="2026-08-17T09:00:00Z",
            termination_reason="soft-pause",
        )

        assert paused.command.acknowledged_attempt_id == "attempt-control-1"
        assert (
            terminated.phase,
            terminated.outcome,
            terminated.drain_state,
            terminated.state_version,
        ) == (AttemptPhase.TERMINATED, AttemptOutcome.INTERRUPTED, DrainState.DRAINED, 2)
        assert connection.execute(
            "SELECT phase,outcome,drain_state,ended_at,state_version FROM attempts"
        ).fetchone() == ("TERMINATED", "INTERRUPTED", "DRAINED", "2026-08-17T09:00:00Z", 2)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (event_count + 1,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "operation",
    [
        WriteOperation.SANITIZED_STREAM_TAIL,
        WriteOperation.SEMANTIC_TAIL,
        WriteOperation.HIDDEN_CHECKPOINT,
        WriteOperation.ATTEMPT_TERMINATION,
        WriteOperation.RECONCILIATION_RECEIPT,
    ],
)
def test_old_attempt_can_only_use_drain_whitelist(operation: WriteOperation) -> None:
    """快速恢复后旧 Attempt 仍可安全收尾，但返回模式必须明确是 DRAIN_WRITE。"""
    assert require_write(_guard_context(), operation) is WriteMode.DRAIN_WRITE


@pytest.mark.parametrize(
    "operation",
    [
        WriteOperation.TOOL_CALL,
        WriteOperation.AUTHORIZATION_CONSUME,
        WriteOperation.SIDE_EFFECT,
        WriteOperation.CANDIDATE_ARTIFACT,
    ],
)
def test_current_attempt_can_start_work_only_in_normal_write(operation: WriteOperation) -> None:
    """最新 control seq 且 token/epoch/version 一致的无 drain Attempt 才进入 NORMAL_WRITE。"""
    context = replace(
        _guard_context(),
        attempt_accepted_control_command_seq=2,
        attempt_drain_state=DrainState.NONE,
        blocking_command_attempt_id=None,
    )
    assert require_write(context, operation) is WriteMode.NORMAL_WRITE


@pytest.mark.parametrize(
    "mutation",
    [
        {"run_desired_state": RunDesiredState.PAUSED},
        {"attempt_accepted_control_command_seq": 1},
        {"attempt_executor_id": "executor-other"},
        {"attempt_fencing_token": 6},
        {"attempt_control_epoch": 8},
        {"run_state_version": 1},
        {"attempt_drain_state": DrainState.DRAINING},
    ],
)
def test_normal_write_rejects_each_stale_or_draining_fact(mutation: dict[str, object]) -> None:
    """desired、seq、token、epoch、version 或 drain 任一事实不匹配都 fail closed。"""
    valid = replace(
        _guard_context(),
        attempt_accepted_control_command_seq=2,
        attempt_drain_state=DrainState.NONE,
        blocking_command_attempt_id=None,
    )
    context = replace(valid, **mutation)
    with pytest.raises(WriteGuardError):
        require_write(context, WriteOperation.TOOL_CALL)


@pytest.mark.parametrize(
    "operation",
    [
        WriteOperation.TOOL_CALL,
        WriteOperation.AUTHORIZATION_CONSUME,
        WriteOperation.SIDE_EFFECT,
        WriteOperation.CANDIDATE_ARTIFACT,
    ],
)
def test_old_attempt_cannot_restart_normal_work_after_resume(operation: WriteOperation) -> None:
    """旧 accepted seq 与新 run seq 不等时，新工具/授权/副作用/候选 Artifact 全部拒绝。"""
    with pytest.raises(WriteGuardError) as caught:
        require_write(_guard_context(), operation)
    assert caught.value.error_code == "WRITE_GUARD_REJECTED"


@pytest.mark.parametrize(
    "mutation",
    [
        {"blocking_command_attempt_id": "attempt-other"},
        {"attempt_executor_id": "executor-other"},
        {"attempt_fencing_token": 6},
        {"attempt_control_epoch": 8},
        {"attempt_drain_state": DrainState.DRAINED},
    ],
)
def test_drain_write_rejects_wrong_latch_or_fencing(mutation: dict[str, object]) -> None:
    """DRAIN_WRITE 仍绑定原阻断命令、token、epoch 和 DRAINING latch。"""
    with pytest.raises(WriteGuardError):
        require_write(replace(_guard_context(), **mutation), WriteOperation.ATTEMPT_TERMINATION)


def test_new_attempt_dispatch_requires_queued_and_fresh_identity() -> None:
    """恢复执行必须先收敛到 QUEUED，并创建不同于旧 Attempt 的新 identity。"""
    require_new_attempt_dispatch(
        observed_state=RunObservedState.QUEUED,
        new_attempt_id="attempt-control-2",
        previous_attempt_id="attempt-control-1",
    )
    for observed in (
        RunObservedState.RUNNING,
        RunObservedState.PAUSED,
        RunObservedState.RECONCILING,
    ):
        with pytest.raises(WriteGuardError):
            require_new_attempt_dispatch(
                observed_state=observed,
                new_attempt_id="attempt-control-2",
                previous_attempt_id="attempt-control-1",
            )
    with pytest.raises(WriteGuardError):
        require_new_attempt_dispatch(
            observed_state=RunObservedState.QUEUED,
            new_attempt_id="attempt-control-1",
            previous_attempt_id="attempt-control-1",
        )
