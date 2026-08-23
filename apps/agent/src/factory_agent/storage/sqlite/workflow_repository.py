"""Task 2 工作流复合 CAS，复用 Task 1 UoW 的状态事件配对登记。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from factory_agent.domain import authorization as authorization_domain
from factory_agent.domain.artifacts import Artifact
from factory_agent.domain.control import ControlCommandType
from factory_agent.domain.plans import PlanRevisionBundle
from factory_agent.domain.workflow import (
    AchievedStage,
    Attempt,
    AttemptOutcome,
    AttemptPhase,
    DrainState,
    PhaseBarrier,
    Run,
    RunDesiredState,
    RunObservedState,
    RunPhase,
    Step,
    StepOutcome,
    StepPhase,
    Task,
    TaskLifecycle,
)
from factory_agent.errors import FactoryError
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.policy.plan_hash import barrier_id as derive_barrier_id
from factory_agent.policy.plan_hash import is_factory_rfc3339_date_time
from factory_agent.state_machine.barriers import BarrierStepFacts
from factory_agent.state_machine.predicates import KNOWN_TERMINAL_OUTCOMES
from factory_agent.state_machine.transitions import (
    require_achieved_stage_transition,
    require_attempt_phase_outcome,
    require_attempt_phase_transition,
    require_attempt_termination_transition,
    require_observed_transition,
    require_phase_transition,
    require_step_phase_outcome,
    require_step_phase_transition,
    require_task_lifecycle_transition,
)
from factory_agent.state_machine.write_guards import (
    WriteGuardContext,
    WriteMode,
    WriteOperation,
    require_new_attempt_dispatch,
    require_write,
)
from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork


class WorkflowRepositoryError(FactoryError):
    """工作流 lineage 不完整或出现多个活动 Attempt 时抛出。"""

    error_code = "WORKFLOW_REPOSITORY_INVARIANT"


def _rfc3339_utc(value: object, *, field: str) -> datetime:
    """在仓储边界先验证 Factory RFC3339，再统一成 UTC 瞬时用于安全比较。"""
    if type(value) is not str or not is_factory_rfc3339_date_time(value):
        raise WorkflowRepositoryError(f"{field} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value[-1:].casefold() == "z" else value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp is naive")
        return parsed.astimezone(UTC)
    except (OverflowError, TypeError, ValueError):
        raise WorkflowRepositoryError(f"{field} timestamp is invalid") from None


def _attempt_dispatch_utc(value: object) -> datetime:
    """校验 scheduler 显式注入的权威时刻；仓储禁止读取墙钟或接受 naive datetime。"""
    if not isinstance(value, datetime):
        raise WorkflowRepositoryError("attempt dispatch clock is invalid")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("attempt dispatch clock is naive")
        return value.astimezone(UTC)
    except (OverflowError, TypeError, ValueError):
        raise WorkflowRepositoryError("attempt dispatch clock is invalid") from None


def _authorization_time_active(record: Mapping[str, object], *, now: datetime) -> bool:
    """按 issued <= now < expires 计算授权活性；损坏或倒置时间一律 fail closed。"""
    issued_at = _rfc3339_utc(record.get("issued_at"), field="authorization issued_at")
    expires_at = _rfc3339_utc(record.get("expires_at"), field="authorization expires_at")
    if issued_at >= expires_at:
        raise WorkflowRepositoryError("authorization time interval is invalid")
    revoked_at = record.get("revoked_at")
    if revoked_at is not None:
        revoked_instant = _rfc3339_utc(revoked_at, field="authorization revoked_at")
        if revoked_instant < issued_at or revoked_instant > now or revoked_instant > expires_at:
            # 撤销必须已经发生且落在授权闭区间内；未来或过期后撤销属于损坏事实。
            raise WorkflowRepositoryError("authorization revocation time is invalid")
        return False
    return issued_at <= now < expires_at


def _lease_rows_active(rows: Sequence[Sequence[object]], *, now: datetime) -> bool:
    """验证全部匹配 lease 的 UTC 时间后再 OR，禁止活动行短路掩盖另一条损坏事实。"""
    active = False
    for acquired_wire, heartbeat_wire, expires_wire in rows:
        acquired_at = _rfc3339_utc(acquired_wire, field="lease acquired_at")
        heartbeat_at = _rfc3339_utc(heartbeat_wire, field="lease heartbeat_at")
        expires_at = _rfc3339_utc(expires_wire, field="lease expires_at")
        if acquired_at > heartbeat_at or heartbeat_at > now or heartbeat_at >= expires_at:
            raise WorkflowRepositoryError("lease time ordering is invalid")
        active = active or now < expires_at
    return active


def _validate_step_projection(step: Step) -> None:
    """把共享 Step phase/outcome 状态机错误稳定翻译为仓储不变量错误。"""
    try:
        require_step_phase_outcome(step.phase, step.outcome)
    except FactoryError as exc:
        raise WorkflowRepositoryError("Step phase/outcome is invalid") from exc


def _validate_attempt_projection(attempt: Attempt, *, now: datetime) -> None:
    """共享校验 Attempt phase/outcome 与 UTC 时间线，供读取权威和 termination 写端复用。"""
    try:
        require_attempt_phase_outcome(attempt.phase, attempt.outcome)
    except FactoryError as exc:
        raise WorkflowRepositoryError("Attempt phase/outcome is invalid") from exc

    started_at = None if attempt.started_at is None else _rfc3339_utc(attempt.started_at, field="attempt started_at")
    if attempt.phase is AttemptPhase.TERMINATED:
        if attempt.ended_at is None:
            raise WorkflowRepositoryError("attempt chronology is invalid")
        ended_at = _rfc3339_utc(attempt.ended_at, field="attempt ended_at")
        if ended_at > now or (started_at is not None and (started_at > ended_at or started_at > now)):
            # authoritative now 是严格上界；不引入时钟容差，ended_at==now 才是合法边界。
            raise WorkflowRepositoryError("attempt chronology is invalid")
    elif attempt.ended_at is not None or (started_at is not None and started_at > now):
        raise WorkflowRepositoryError("attempt chronology is invalid")


def _snapshot_digest(selector: object, *, field: str) -> str:
    """从已通过 canonical getter 的快照引用读取 digest，不重做 Task 4 artifact 解析。"""
    if not isinstance(selector, bytes):
        raise WorkflowRepositoryError(f"{field} snapshot selector is invalid")
    try:
        value = json.loads(selector.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WorkflowRepositoryError(f"{field} snapshot selector is invalid") from None
    if not isinstance(value, dict):
        raise WorkflowRepositoryError(f"{field} snapshot selector is invalid")
    digest = value.get("digest")
    if type(digest) is not str:
        raise WorkflowRepositoryError(f"{field} snapshot selector is invalid")
    return digest


@dataclass(frozen=True, slots=True)
class ObservedStateAuthorityFacts:
    """同一 owner transaction 从 Attempt/Lease/Auth/receipt 表投影的不可伪造事实。"""

    active_attempt_count: int
    lease_active: bool
    authorization_active: bool
    control_sequence_current: bool
    heartbeat_valid: bool
    unknown_remote_state: bool
    unsettled_started_receipt: bool
    dispatch_state_valid: bool = False
    blocking_fact: bool = False
    block_reason_code: str | None = None
    pausing_fact: bool = False
    stopping_fact: bool = False
    interrupted_fact: bool = False
    reconciling_fact: bool = False
    terminated_fact: bool = False


@dataclass(frozen=True, slots=True)
class ExecutorWriteAuthority:
    """同一 UoW 内构造的 Run/Step/Attempt 与写保护权威快照。"""

    run: Run
    step: Step
    attempt: Attempt
    guard: WriteGuardContext


@dataclass(frozen=True, slots=True)
class _AuthorizationAuthority:
    """一条已闭合 canonical/lineage/time 的授权事实及其稳定 action key。"""

    active: bool
    stable_action_key: tuple[str, str, bytes]


@dataclass(frozen=True, slots=True)
class BarrierAuthorityFacts:
    """同一事务由 steps/attempts/receipts/artifacts/findings 重算的 gate 事实。"""

    steps: tuple[BarrierStepFacts, ...]
    active_attempt_count: int
    unknown_remote_state: bool
    required_artifacts_committed: bool
    blocking_finding_count: int
    required_node_ids: tuple[str, ...] = ()
    required_node_set_digest: str = ""


@dataclass(frozen=True, slots=True)
class _PlanBarrierAuthority:
    """已通过领域 hydrator 校验的当前 barrier 合同投影。"""

    plan_revision_id: str
    plan_revision_digest: str
    business_phase: str
    barrier_ordinal: int
    pass_predicate_id: str
    required_node_ids: tuple[str, ...]
    required_node_set_digest: str
    settle_timeout_ms: int
    nodes: Mapping[str, Mapping[str, object]]
    barriers: tuple[Mapping[str, object], ...]
    barrier_wire_index: Mapping[tuple[str, int], int]
    stage_maps: Mapping[str, tuple[str, ...]]


class SqliteWorkflowRepository:
    """在现有 UoW 内扩展多列 CAS，不创建第二连接或隐藏事务。"""

    def __init__(self, unit_of_work: SqliteUnitOfWork) -> None:
        self._unit_of_work = unit_of_work
        self._connection = unit_of_work._connection
        self._task1 = unit_of_work.workflow

    def get_run(self, run_id: str) -> Run | None:
        """读取并按 Task 1 精确 selector hydrate Run。"""
        row = self._task1.get_run(run_id)
        if row is None:
            return None
        return Run.from_record({key: row[key] for key in Run.__dataclass_fields__})

    def get_task(self, task_id: str) -> Task | None:
        """读取 Task 的权威 selector，供里程碑事务核对 target 与 outcomeVersion。"""
        row = self._task1.get_task(task_id)
        if row is None:
            return None
        return Task.from_record({key: row[key] for key in Task.__dataclass_fields__})

    @staticmethod
    def _hydrate_barrier(row: dict[str, object]) -> PhaseBarrier:
        """把 STRICT SQLite 的 0/1 显式收敛为 bool，避免把整数泄漏进领域层。"""
        record = {
            key: bool(row[key]) if key in {"settled", "passed"} else row[key]
            for key in PhaseBarrier.__dataclass_fields__
            if not key.startswith("_")
        }
        return PhaseBarrier.from_record(record)

    def get_phase_barrier(self, barrier_id: str) -> PhaseBarrier | None:
        """读取运行时 barrier 的权威 selector。"""
        row = self._task1.get_phase_barrier(barrier_id)
        return None if row is None else self._hydrate_barrier(row)

    def get_step(self, step_id: str) -> Step | None:
        """读取并按 Task 1 精确 selector hydrate Step。"""
        row = self._task1.get_step(step_id)
        if row is None:
            return None
        return Step.from_record({key: row[key] for key in Step.__dataclass_fields__})

    def get_attempt(self, attempt_id: str) -> Attempt | None:
        """读取并按 Task 1 精确 selector hydrate Attempt。"""
        row = self._task1.get_attempt(attempt_id)
        if row is None:
            return None
        return Attempt.from_record({key: row[key] for key in Attempt.__dataclass_fields__})

    def _validate_attempt_interrupt_lineage(
        self,
        *,
        run: Run,
        attempt: Attempt,
        historical: bool,
    ) -> None:
        """验证 DRAINING/DRAINED 指针对应的 blocking command 与初始 ACK。"""
        command_id = attempt.interrupt_command_id
        if command_id is None:
            raise WorkflowRepositoryError("draining attempt interrupt command is missing")
        blocking_types = (
            ControlCommandType.SOFT_PAUSE.value,
            ControlCommandType.IMMEDIATE_STOP.value,
            ControlCommandType.CANCEL.value,
        )
        command = self._connection.execute(
            "SELECT command_id,run_id,command_seq,command_type,acknowledged_attempt_id "
            "FROM control_commands WHERE command_id=?",
            (command_id,),
        ).fetchone()
        if (
            command is None
            or command[1] != run.run_id
            or type(command[2]) is not int
            or command[3] not in blocking_types
            or command[4] != attempt.attempt_id
            or not attempt.accepted_control_command_seq < command[2] <= run.control_command_seq
        ):
            raise WorkflowRepositoryError("attempt blocking command lineage is inconsistent")
        initial_ack_rows = self._connection.execute(
            "SELECT phase,attempt_id FROM control_command_receipt_events WHERE command_id=? AND receipt_seq=0",
            (command_id,),
        ).fetchall()
        if len(initial_ack_rows) != 1 or initial_ack_rows[0] != ("ACKNOWLEDGED", attempt.attempt_id):
            raise WorkflowRepositoryError("attempt blocking command acknowledgement is inconsistent")

        if historical:
            latest = self._connection.execute(
                "SELECT command_id FROM control_commands WHERE run_id=? AND acknowledged_attempt_id=? "
                "AND command_type IN (?,?,?) ORDER BY command_seq DESC LIMIT 1",
                (run.run_id, attempt.attempt_id, *blocking_types),
            ).fetchone()
        else:
            latest = self._connection.execute(
                "SELECT command_id FROM control_commands WHERE run_id=? AND command_type IN (?,?,?) "
                "ORDER BY command_seq DESC LIMIT 1",
                (run.run_id, *blocking_types),
            ).fetchone()
        if latest is None or latest[0] != command_id:
            # 当前 DRAINING 绑定 Run-latest blocker；历史 DRAINED 只绑定确认该 Attempt 的 latest blocker。
            raise WorkflowRepositoryError("attempt latest blocking command lineage is inconsistent")

    def _run_attempts_with_valid_drain_authority(self, run: Run) -> tuple[Attempt, ...]:
        """扫描 Run 全部 Attempt；active 只按 phase 推导，drain 状态独立做 fail-closed 校验。"""
        cursor = self._connection.execute(
            "SELECT a.* FROM attempts AS a JOIN steps AS s ON s.step_id=a.step_id "
            "WHERE s.run_id=? ORDER BY a.attempt_id",
            (run.run_id,),
        )
        columns = tuple(item[0] for item in cursor.description)
        attempts: list[Attempt] = []
        for values in cursor.fetchall():
            record = dict(zip(columns, values, strict=True))
            try:
                attempt = Attempt.from_record({key: record[key] for key in Attempt.__dataclass_fields__})
            except (FactoryError, KeyError, TypeError, ValueError) as exc:
                raise WorkflowRepositoryError("run Attempt drain projection is invalid") from exc
            terminal = attempt.phase is AttemptPhase.TERMINATED
            if attempt.drain_state is DrainState.NONE:
                if attempt.interrupt_command_id is not None:
                    raise WorkflowRepositoryError("NONE drain state cannot retain an interrupt command")
            elif attempt.drain_state is DrainState.DRAINING:
                if terminal:
                    raise WorkflowRepositoryError("terminated attempt cannot remain DRAINING")
                self._validate_attempt_interrupt_lineage(run=run, attempt=attempt, historical=False)
            elif attempt.drain_state is DrainState.DRAINED:
                if not terminal:
                    raise WorkflowRepositoryError("nonterminal attempt cannot be DRAINED")
                self._validate_attempt_interrupt_lineage(run=run, attempt=attempt, historical=True)
            attempts.append(attempt)
        return tuple(attempts)

    def get_active_attempt_for_run(self, run_id: str) -> Attempt | None:
        """先验证 Run 全部 drain 权威，再仅以 phase 返回唯一活动 Attempt。"""
        run = self.get_run(run_id)
        if run is None:
            raise WorkflowRepositoryError("run does not exist")
        attempts = self._run_attempts_with_valid_drain_authority(run)
        active_attempts = tuple(attempt for attempt in attempts if attempt.phase is not AttemptPhase.TERMINATED)
        if len(active_attempts) > 1:
            raise WorkflowRepositoryError("multiple active attempts exist for one run")
        if not active_attempts:
            return None
        return active_attempts[0]

    def load_executor_write_authority(
        self,
        *,
        run_id: str,
        attempt_id: str,
        expected_run_state_version: int,
        expected_executor_id: str,
        expected_fencing_token: int,
        expected_control_epoch: int,
        now: datetime,
    ) -> ExecutorWriteAuthority:
        """同一 owner transaction 内闭合 selector、latest blocker 与 matching lease。"""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise WorkflowRepositoryError("executor write authority clock is invalid")
        now_utc = now.astimezone(UTC)
        run = self.get_run(run_id)
        attempt = self.get_attempt(attempt_id)
        if run is None or attempt is None:
            raise WorkflowRepositoryError("executor write authority is missing")
        step = self.get_step(attempt.step_id)
        if step is None or step.run_id != run.run_id:
            raise WorkflowRepositoryError("executor write lineage is inconsistent")
        # guarded wrapper 共享同一组 projection/时间校验，不能只检查本次准备写的那张表。
        _validate_step_projection(step)
        _validate_attempt_projection(attempt, now=now_utc)
        active_attempt = self.get_active_attempt_for_run(run.run_id)
        if active_attempt is None or active_attempt.attempt_id != attempt.attempt_id:
            raise WorkflowRepositoryError("executor attempt is not the active run attempt")

        blocking_command_attempt_id: str | None = None
        if attempt.drain_state is DrainState.DRAINING:
            blocking_types = (
                ControlCommandType.SOFT_PAUSE.value,
                ControlCommandType.IMMEDIATE_STOP.value,
                ControlCommandType.CANCEL.value,
            )
            latest_blocking_command = self._connection.execute(
                "SELECT command_id,run_id,acknowledged_attempt_id FROM control_commands "
                "WHERE run_id=? AND command_type IN (?,?,?) ORDER BY command_seq DESC LIMIT 1",
                (run.run_id, *blocking_types),
            ).fetchone()
            if (
                latest_blocking_command is None
                or attempt.interrupt_command_id != latest_blocking_command[0]
                or latest_blocking_command[1] != run.run_id
                or latest_blocking_command[2] != attempt.attempt_id
            ):
                # 仅比较最高 blocking command_seq；后续 RESUME 不是阻断命令，不会使旧 Attempt 失去收尾权。
                raise WorkflowRepositoryError("attempt latest blocking command lineage is inconsistent")
            blocking_command_attempt_id = str(latest_blocking_command[2])
        elif attempt.interrupt_command_id is not None:
            raise WorkflowRepositoryError("attempt blocking command latch is inconsistent")

        # matching 仅限 owner/token/epoch；resourceFingerprint 绑定和 lease 生命周期仍属于 Task 4/5。
        lease_active = self._matching_lease_active(active_attempt=attempt, now=now_utc)

        guard = WriteGuardContext(
            run_desired_state=run.desired_state,
            run_control_command_seq=run.control_command_seq,
            run_state_version=run.state_version,
            expected_run_state_version=expected_run_state_version,
            attempt_id=attempt.attempt_id,
            attempt_executor_id=attempt.executor_id or "",
            expected_executor_id=expected_executor_id,
            attempt_accepted_control_command_seq=attempt.accepted_control_command_seq,
            attempt_fencing_token=attempt.fencing_token,
            expected_fencing_token=expected_fencing_token,
            attempt_control_epoch=attempt.control_epoch,
            expected_control_epoch=expected_control_epoch,
            attempt_drain_state=attempt.drain_state,
            blocking_command_attempt_id=blocking_command_attempt_id,
            lease_active=lease_active,
        )
        return ExecutorWriteAuthority(run=run, step=step, attempt=attempt, guard=guard)

    def _matching_lease_active(self, *, active_attempt: Attempt, now: datetime) -> bool:
        """仅按 owner/token/epoch 读取匹配 lease；资源映射仍明确属于 Task 4/5。"""
        rows = self._connection.execute(
            "SELECT acquired_at,heartbeat_at,expires_at FROM resource_leases "
            "WHERE owner_executor_id=? AND fencing_token=? AND control_epoch=? ORDER BY resource_key",
            (active_attempt.executor_id, active_attempt.fencing_token, active_attempt.control_epoch),
        ).fetchall()
        return _lease_rows_active(rows, now=now)

    def _active_authorization_valid(
        self,
        *,
        run: Run,
        active_attempt: Attempt,
        now: datetime,
    ) -> bool:
        """完整校验当前 Attempt 的每条授权，再按稳定 action key 汇总活动事实。"""
        task = self.get_task(run.task_id)
        if task is None:
            raise WorkflowRepositoryError("active authorization task is missing")
        authorization_ids = tuple(
            str(row[0])
            for row in self._connection.execute(
                "SELECT execution_authorization_id FROM execution_authorizations "
                "WHERE attempt_id=? ORDER BY execution_authorization_id",
                (active_attempt.attempt_id,),
            ).fetchall()
        )
        authorities: list[_AuthorizationAuthority] = []
        for authorization_id in authorization_ids:
            authorities.append(
                self._validate_authorization_authority(
                    authorization_id=authorization_id,
                    run=run,
                    task=task,
                    now=now,
                    expected_attempt=active_attempt,
                    require_current_plan=True,
                )
            )
        self._reject_duplicate_active_authorization_keys(authorities)
        return any(authority.active for authority in authorities)

    @staticmethod
    def _reject_duplicate_active_authorization_keys(authorities: Sequence[_AuthorizationAuthority]) -> None:
        """同 Attempt/capability/canonical key 的双活动行语义歧义，必须整体拒绝。"""
        stable_keys: set[tuple[str, str, bytes]] = set()
        for authority in authorities:
            if not authority.active:
                continue
            if authority.stable_action_key in stable_keys:
                raise WorkflowRepositoryError("duplicate active authorization stable action key")
            stable_keys.add(authority.stable_action_key)

    def _validate_authorization_authority(
        self,
        *,
        authorization_id: str,
        run: Run,
        task: Task,
        now: datetime,
        expected_attempt: Attempt | None,
        require_current_plan: bool,
    ) -> _AuthorizationAuthority:
        """共享校验活动/历史授权；历史仅豁免 active Plan 指针，不豁免 canonical 与 lineage。"""
        try:
            # UoW getter 会逐字节重验 canonical contract 与全部 SQL projection，损坏不能降级为 inactive。
            authorization = self._unit_of_work.authorization.get_execution_authorization(authorization_id)
            if authorization is None:
                raise WorkflowRepositoryError("execution authorization disappeared")
            intent = self._unit_of_work.authorization.get_intent_authorization(
                str(authorization["intent_authorization_id"])
            )
            plan = self._task1.get_plan_revision(str(authorization["plan_revision_id"]))
            step = self.get_step(str(authorization["step_id"]))
            attempt = self.get_attempt(str(authorization["attempt_id"]))
            if intent is None or plan is None or step is None or attempt is None:
                raise WorkflowRepositoryError("authorization authority is missing")
            # 由领域 hydrator 读取 RunSpec target；catalog/manifest 与资源绑定仍明确留给 Task 4/5。
            plan_bundle = PlanRevisionBundle.from_persistence_record(plan)
        except authorization_domain.AuthorizationContractError:
            # canonical/projection 损坏保留冻结的 INVALID_AUTHORIZATION_CONTRACT 分类，事务仍由 coordinator 回滚。
            raise
        except WorkflowRepositoryError:
            raise
        except FactoryError as exc:
            raise WorkflowRepositoryError("authorization canonical authority is invalid") from exc

        lineage_valid = (
            task.lifecycle is TaskLifecycle.ACTIVE
            and task.active_run_id == run.run_id
            and run.task_id == task.task_id
            and authorization["run_id"] == run.run_id
            and authorization["step_id"] == step.step_id
            and authorization["attempt_id"] == attempt.attempt_id
            and step.run_id == run.run_id
            and attempt.step_id == step.step_id
            and step.plan_revision_id == authorization["plan_revision_id"]
            and plan["task_id"] == task.task_id
            and plan["intent_authorization_id"] == authorization["intent_authorization_id"]
            and intent["task_id"] == task.task_id
            and intent["project_id"] == task.project_id
            and intent["target_stage"] == task.target_stage.value
            and plan_bundle.run_spec["targetStage"] == intent["target_stage"]
            and _snapshot_digest(authorization["semantic_plan_hash"], field="semantic plan")
            == plan["semantic_plan_hash"]
            and _snapshot_digest(authorization["plan_revision_digest"], field="plan revision")
            == plan["plan_revision_digest"]
            and authorization["stage_capability_map_version"] == plan["stage_capability_map_version"]
            and intent["stage_capability_map_version"] == plan["stage_capability_map_version"]
            and authorization["node_capability_map_version"] == plan["node_capability_map_version"]
            and authorization["node_type"] == step.node_type
            and authorization["executor_id"] == attempt.executor_id
            and authorization["fencing_token"] == attempt.fencing_token
            and authorization["control_epoch"] == attempt.control_epoch
            and authorization["accepted_control_command_seq"] == attempt.accepted_control_command_seq
            and (not require_current_plan or task.active_plan_revision_id == authorization["plan_revision_id"])
            and (expected_attempt is None or expected_attempt.attempt_id == attempt.attempt_id)
        )
        if not lineage_valid:
            label = "active" if require_current_plan else "historical"
            raise WorkflowRepositoryError(f"{label} authorization lineage is invalid")

        # 时间即使对 CONSUMED/已撤销行也必须完整解析，防止损坏历史事实被状态短路掩盖。
        authorization_time_active = _authorization_time_active(authorization, now=now)
        intent_time_active = _authorization_time_active(intent, now=now)
        idempotency_key = authorization["idempotency_key"]
        if not isinstance(idempotency_key, bytes):
            raise WorkflowRepositoryError("authorization idempotency key is invalid")
        return _AuthorizationAuthority(
            active=(
                authorization["consumption_state"] == "AVAILABLE"
                and type(authorization["max_uses"]) is int
                and authorization["max_uses"] > 0
                and authorization_time_active
                and intent_time_active
            ),
            stable_action_key=(
                attempt.attempt_id,
                str(authorization["action_capability"]),
                idempotency_key,
            ),
        )

    def load_observed_state_authority(
        self,
        *,
        run_id: str,
        now: datetime,
    ) -> ObservedStateAuthorityFacts:
        """在状态 CAS 前从同一 SQLite 连接重算执行事实，调用方布尔摘要完全不参与授权。"""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise WorkflowRepositoryError("authority fact clock is invalid")
        now_utc = now.astimezone(UTC)
        run = self.get_run(run_id)
        if run is None:
            raise WorkflowRepositoryError("run does not exist")
        active_attempt = self.get_active_attempt_for_run(run_id)
        # observed 判定前扫描当前 Run 的全部执行投影；不能让一条合法 active 行掩盖历史损坏事实。
        for (step_id,) in self._connection.execute(
            "SELECT step_id FROM steps WHERE run_id=? ORDER BY step_id",
            (run_id,),
        ).fetchall():
            step = self.get_step(str(step_id))
            if step is None:
                raise WorkflowRepositoryError("run Step authority is missing")
            _validate_step_projection(step)
        for (attempt_id,) in self._connection.execute(
            "SELECT a.attempt_id FROM attempts AS a JOIN steps AS s ON s.step_id=a.step_id "
            "WHERE s.run_id=? ORDER BY a.attempt_id",
            (run_id,),
        ).fetchall():
            attempt = self.get_attempt(str(attempt_id))
            if attempt is None:
                raise WorkflowRepositoryError("run Attempt authority is missing")
            _validate_attempt_projection(attempt, now=now_utc)
        lease_active = False
        heartbeat_valid = False
        authorization_active = False
        control_sequence_current = False
        dispatch_state_valid = False
        if active_attempt is not None:
            step_phase_row = self._connection.execute(
                "SELECT phase FROM steps WHERE step_id=? AND run_id=?",
                (active_attempt.step_id, run_id),
            ).fetchone()
            # 只有 Step 与 Attempt 都进入 RUNNING，观察态才可越过 QUEUED。
            dispatch_state_valid = (
                active_attempt.phase is AttemptPhase.RUNNING
                and active_attempt.drain_state is DrainState.NONE
                and step_phase_row is not None
                and step_phase_row[0] == StepPhase.RUNNING.value
            )
            lease_active = self._matching_lease_active(active_attempt=active_attempt, now=now_utc)
            heartbeat_valid = lease_active
            control_sequence_current = active_attempt.accepted_control_command_seq == run.control_command_seq
            authorization_active = self._active_authorization_valid(
                run=run,
                active_attempt=active_attempt,
                now=now_utc,
            )
        else:
            # Attempt 已收口时仍复用 canonical 校验器；仅允许合法失效授权按历史 Plan 收口。
            task = self.get_task(run.task_id)
            if task is None:
                raise WorkflowRepositoryError("terminal authorization task is missing")
            authorization_rows = self._connection.execute(
                "SELECT ea.execution_authorization_id FROM execution_authorizations AS ea WHERE ea.run_id=? "
                "UNION SELECT ea.execution_authorization_id FROM execution_authorizations AS ea "
                "JOIN steps AS s ON s.step_id=ea.step_id WHERE s.run_id=? "
                "UNION SELECT ea.execution_authorization_id FROM execution_authorizations AS ea "
                "JOIN attempts AS a ON a.attempt_id=ea.attempt_id "
                "JOIN steps AS s ON s.step_id=a.step_id WHERE s.run_id=?",
                (run_id, run_id, run_id),
            ).fetchall()
            historical_authorities = tuple(
                self._validate_authorization_authority(
                    authorization_id=str(row[0]),
                    run=run,
                    task=task,
                    now=now_utc,
                    expected_attempt=None,
                    require_current_plan=False,
                )
                for row in authorization_rows
            )
            self._reject_duplicate_active_authorization_keys(historical_authorities)
            authorization_active = any(authority.active for authority in historical_authorities)
            lease_rows = self._connection.execute(
                "SELECT l.acquired_at,l.heartbeat_at,l.expires_at FROM resource_leases AS l "
                "JOIN attempts AS a "
                "ON a.executor_id=l.owner_executor_id "
                "AND a.fencing_token=l.fencing_token "
                "AND a.control_epoch=l.control_epoch "
                "JOIN steps AS s ON s.step_id=a.step_id WHERE s.run_id=? ORDER BY l.resource_key",
                (run_id,),
            ).fetchall()
            lease_active = _lease_rows_active(lease_rows, now=now_utc)
            heartbeat_valid = lease_active
        unknown_remote_state = (
            self._connection.execute(
                "SELECT 1 FROM steps AS s WHERE s.run_id=? AND s.outcome='UNKNOWN_REMOTE_STATE' "
                "UNION ALL SELECT 1 FROM attempts AS a JOIN steps AS s ON s.step_id=a.step_id "
                "WHERE s.run_id=? AND a.outcome='UNKNOWN_REMOTE_STATE' LIMIT 1",
                (run_id, run_id),
            ).fetchone()
            is not None
        )
        attempt_fact_rows = self._connection.execute(
            "SELECT a.phase,a.outcome,a.drain_state FROM attempts AS a "
            "JOIN steps AS s ON s.step_id=a.step_id WHERE s.run_id=?",
            (run_id,),
        ).fetchall()
        step_fact_rows = self._connection.execute(
            "SELECT phase,outcome FROM steps WHERE run_id=?",
            (run_id,),
        ).fetchall()
        blocking_finding_row = self._connection.execute(
            "SELECT COUNT(*) FROM review_findings WHERE task_id=? "
            "AND UPPER(severity)='BLOCKING' "
            "AND UPPER(status) NOT IN ('CLOSED','RESOLVED','SUPERSEDED','DISMISSED')",
            (run.task_id,),
        ).fetchone()
        blocking_finding = bool(blocking_finding_row and int(blocking_finding_row[0]) > 0)
        blocking_fact = blocking_finding or unknown_remote_state or bool(run.requires_user_action)
        block_reason_code = run.block_reason_code
        if block_reason_code is None:
            if blocking_finding:
                block_reason_code = "BLOCKING_FINDING_OPEN"
            elif unknown_remote_state:
                block_reason_code = "UNKNOWN_REMOTE_STATE"
        pausing_fact = active_attempt is not None and active_attempt.drain_state is DrainState.DRAINING
        stopping_fact = active_attempt is not None and (
            active_attempt.phase is AttemptPhase.INTERRUPTING or active_attempt.drain_state is DrainState.DRAINING
        )
        interrupted_fact = active_attempt is None and any(
            outcome in {"INTERRUPTED", "KILLED", "LOST"} for _phase, outcome, _drain_state in attempt_fact_rows
        )
        reconciling_fact = (
            unknown_remote_state
            or any(
                phase == StepPhase.RECONCILING.value or phase == AttemptPhase.RECONCILING.value
                for phase, _outcome, _drain_state in attempt_fact_rows
            )
            or any(phase == StepPhase.RECONCILING.value for phase, _outcome in step_fact_rows)
        )
        unsettled_started_receipt = (
            self._connection.execute(
                "SELECT 1 FROM action_receipt_events AS r "
                "JOIN actions AS a ON a.action_id=r.action_id "
                "JOIN attempts AS at ON at.attempt_id=r.attempt_id "
                "JOIN steps AS s ON s.step_id=at.step_id "
                "WHERE s.run_id=? AND r.phase='STARTED' "
                "AND NOT EXISTS ("
                "SELECT 1 FROM action_receipt_events AS later "
                "WHERE later.action_id=r.action_id AND later.receipt_seq>r.receipt_seq "
                "AND later.phase IN ('COMPLETED','ABSENT_CONFIRMED','FAILED_RECONCILED')) LIMIT 1",
                (run_id,),
            ).fetchone()
            is not None
        )
        terminated_fact = (
            active_attempt is None
            and not lease_active
            and not authorization_active
            and not unsettled_started_receipt
            and bool(step_fact_rows)
            and all(
                phase == StepPhase.TERMINAL.value and outcome in {item.value for item in KNOWN_TERMINAL_OUTCOMES}
                for phase, outcome in step_fact_rows
            )
            and all(
                phase == AttemptPhase.TERMINATED.value
                and outcome
                in {
                    AttemptOutcome.SUCCEEDED.value,
                    AttemptOutcome.FAILED.value,
                    AttemptOutcome.INTERRUPTED.value,
                    AttemptOutcome.KILLED.value,
                    AttemptOutcome.LOST.value,
                }
                and drain_state in {DrainState.NONE.value, DrainState.DRAINED.value}
                for phase, outcome, drain_state in attempt_fact_rows
            )
            and not unknown_remote_state
            and not blocking_fact
        )
        return ObservedStateAuthorityFacts(
            active_attempt_count=0 if active_attempt is None else 1,
            lease_active=lease_active,
            authorization_active=authorization_active,
            control_sequence_current=control_sequence_current,
            heartbeat_valid=heartbeat_valid,
            unknown_remote_state=unknown_remote_state,
            unsettled_started_receipt=unsettled_started_receipt,
            dispatch_state_valid=dispatch_state_valid,
            blocking_fact=blocking_fact,
            block_reason_code=block_reason_code,
            pausing_fact=pausing_fact,
            stopping_fact=stopping_fact,
            interrupted_fact=interrupted_fact,
            reconciling_fact=reconciling_fact,
            terminated_fact=terminated_fact,
        )

    def _load_required_node_authority(
        self,
        *,
        barrier_id: str,
        run_id: str,
        task_id: str,
        require_active_barrier: bool = True,
    ) -> _PlanBarrierAuthority:
        """从已验证 PlanRevisionBundle 派生 barrier 合同，并可校验后继投影。"""
        barrier_row = self._connection.execute(
            "SELECT plan_revision_id,business_phase,barrier_ordinal,required_node_set_digest,"
            "pass_predicate_id,settle_timeout_ms "
            "FROM phase_barriers WHERE barrier_id=? AND run_id=?",
            (barrier_id, run_id),
        ).fetchone()
        if barrier_row is None:
            raise WorkflowRepositoryError("barrier declaration is missing")
        if (
            type(barrier_row[0]) is not str
            or type(barrier_row[1]) is not str
            or type(barrier_row[2]) is not int
            or barrier_row[2] < 0
        ):
            raise WorkflowRepositoryError("barrier selector is invalid")
        run_row = self._connection.execute(
            "SELECT task_id,phase,active_barrier_id,dag_version FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if run_row is None or run_row[0] != task_id:
            raise WorkflowRepositoryError("barrier run lineage is inconsistent")
        if require_active_barrier and (run_row[1] != barrier_row[1] or run_row[2] != barrier_id):
            raise WorkflowRepositoryError("barrier run lineage is inconsistent")
        task_row = self._connection.execute(
            "SELECT active_plan_revision_id,target_stage FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if task_row is None or task_row[0] != barrier_row[0]:
            raise WorkflowRepositoryError("barrier is not the current plan revision")
        plan_record = self._task1.get_plan_revision(str(barrier_row[0]))
        if plan_record is None or plan_record.get("task_id") != task_id:
            raise WorkflowRepositoryError("barrier plan lineage is incomplete")
        stored_digest = barrier_row[3]
        stored_pass_predicate_id = barrier_row[4]
        stored_settle_timeout_ms = barrier_row[5]
        if (
            type(stored_digest) is not str
            or type(stored_pass_predicate_id) is not str
            or type(stored_settle_timeout_ms) is not int
            or stored_settle_timeout_ms <= 0
        ):
            raise WorkflowRepositoryError("barrier node set digest is invalid")
        try:
            bundle = PlanRevisionBundle.from_persistence_record(plan_record)
            parsed_revision = bundle.plan_revision
        except (FactoryError, TypeError, ValueError) as exc:
            raise WorkflowRepositoryError("validated plan revision is unavailable") from exc
        plan_revision_digest = parsed_revision.get("planRevisionDigest")
        if type(plan_revision_digest) is not str or plan_revision_digest != plan_record.get("plan_revision_digest"):
            raise WorkflowRepositoryError("plan revision digest is inconsistent")
        try:
            expected_barrier_id = derive_barrier_id(
                run_id,
                plan_revision_digest,
                str(barrier_row[1]),
                int(barrier_row[2]),
            )
        except (FactoryError, TypeError, ValueError) as exc:
            raise WorkflowRepositoryError("barrier identity cannot be derived") from exc
        if expected_barrier_id != barrier_id:
            raise WorkflowRepositoryError("barrier identity is not derived from plan authority")
        declared_dag_version = parsed_revision.get("dagVersion")
        run_spec = bundle.run_spec
        run_spec_work_plan = run_spec.get("workPlan")
        run_spec_dag_version = run_spec_work_plan.get("dagVersion") if isinstance(run_spec_work_plan, Mapping) else None
        if (
            type(run_row[3]) is not int
            or type(declared_dag_version) is not int
            or run_row[3] != declared_dag_version
            or run_row[3] != run_spec_dag_version
            or type(run_spec.get("targetStage")) is not str
            or run_spec.get("targetStage") != task_row[1]
        ):
            raise WorkflowRepositoryError("run plan selector is inconsistent")
        raw_barriers = parsed_revision.get("barriers")
        if not isinstance(raw_barriers, Sequence) or isinstance(raw_barriers, (str, bytes, bytearray)):
            raise WorkflowRepositoryError("plan revision barrier declaration is missing")
        barrier_wire_index: dict[tuple[str, int], int] = {}
        for wire_index, barrier in enumerate(raw_barriers):
            if not isinstance(barrier, Mapping):
                raise WorkflowRepositoryError("plan revision barrier declaration is invalid")
            phase = barrier.get("businessPhase")
            ordinal = barrier.get("barrierOrdinal")
            if type(phase) is not str or not phase or type(ordinal) is not int or ordinal < 0:
                raise WorkflowRepositoryError("plan revision barrier selector is invalid")
            key = (phase, ordinal)
            if key in barrier_wire_index:
                raise WorkflowRepositoryError("plan revision barrier selector is duplicated")
            barrier_wire_index[key] = wire_index
        matching_barriers = [
            barrier
            for barrier in raw_barriers
            if isinstance(barrier, Mapping)
            and barrier.get("businessPhase") == barrier_row[1]
            and barrier.get("barrierOrdinal") == barrier_row[2]
        ]
        if len(matching_barriers) != 1:
            raise WorkflowRepositoryError("plan revision barrier selector is ambiguous")
        declared_barrier = matching_barriers[0]
        declared_timeout_ms = declared_barrier.get("settleTimeoutMs")
        if (
            declared_barrier.get("passPredicateId") != stored_pass_predicate_id
            or type(declared_timeout_ms) is not int
            or declared_timeout_ms <= 0
            or declared_timeout_ms != stored_settle_timeout_ms
        ):
            raise WorkflowRepositoryError("barrier predicate is not the plan predicate")
        raw_node_ids = declared_barrier.get("requiredNodeIds")
        if not isinstance(raw_node_ids, Sequence) or isinstance(raw_node_ids, (str, bytes, bytearray)):
            raise WorkflowRepositoryError("required node declaration is empty")
        required_node_ids: list[str] = []
        for node_id in raw_node_ids:
            if type(node_id) is not str or not node_id:
                raise WorkflowRepositoryError("required node declaration is invalid")
            required_node_ids.append(node_id)
        if len(set(required_node_ids)) != len(required_node_ids):
            raise WorkflowRepositoryError("required node declaration is duplicated")
        expected_digest = "sha256:" + hashlib.sha256(canonicalize(required_node_ids)).hexdigest()
        if expected_digest != stored_digest:
            raise WorkflowRepositoryError("required node set digest is inconsistent")
        raw_nodes = parsed_revision.get("nodes")
        if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes, bytearray)):
            raise WorkflowRepositoryError("plan revision node registry is missing")
        nodes: dict[str, Mapping[str, object]] = {}
        for node in raw_nodes:
            if not isinstance(node, Mapping):
                raise WorkflowRepositoryError("plan revision node registry is invalid")
            node_id = node.get("logicalNodeId")
            if type(node_id) is not str or not node_id or node_id in nodes:
                raise WorkflowRepositoryError("plan revision node identity is invalid")
            required_artifacts = node.get("requiredArtifacts")
            if (
                not isinstance(required_artifacts, Sequence)
                or isinstance(required_artifacts, (str, bytes, bytearray))
                or any(type(artifact) is not str or not artifact for artifact in required_artifacts)
            ):
                raise WorkflowRepositoryError("plan revision artifact registry is invalid")
            if len(set(required_artifacts)) != len(required_artifacts):
                raise WorkflowRepositoryError("plan revision artifact registry is duplicated")
            depends_on = node.get("dependsOn")
            if (
                not isinstance(depends_on, Sequence)
                or isinstance(depends_on, (str, bytes, bytearray))
                or any(type(dependency) is not str or not dependency for dependency in depends_on)
                or len(set(depends_on)) != len(depends_on)
            ):
                raise WorkflowRepositoryError("plan revision dependency registry is invalid")
            if (
                any(
                    type(node.get(field)) is not str or not node.get(field)
                    for field in (
                        "businessPhase",
                        "nodeType",
                        "sideEffectClass",
                        "successPredicateId",
                        "retryPolicyId",
                    )
                )
                or type(node.get("barrierOrdinal")) is not int
                or type(node.get("required")) is not bool
            ):
                raise WorkflowRepositoryError("plan revision node role is invalid")
            node_key = (str(node["businessPhase"]), int(node["barrierOrdinal"]))
            if node_key not in barrier_wire_index:
                raise WorkflowRepositoryError("plan revision node barrier is not declared")
            nodes[node_id] = node
        for node in nodes.values():
            dependencies = node.get("dependsOn")
            if not isinstance(dependencies, Sequence) or isinstance(dependencies, (str, bytes, bytearray)):
                raise WorkflowRepositoryError("plan revision dependency selector is invalid")
            if any(dependency_id == node.get("logicalNodeId") for dependency_id in dependencies):
                # 自依赖会让当前节点自证成功，冻结 DAG 必须在 authority 边界拒绝。
                raise WorkflowRepositoryError("plan revision dependency cycle is invalid")
            if any(dependency_id not in nodes for dependency_id in dependencies):
                raise WorkflowRepositoryError("plan revision dependency selector is invalid")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            """深度优先确认冻结 DAG 无环，避免依赖节点互相自证成功。"""
            if node_id in visiting:
                raise WorkflowRepositoryError("plan revision dependency cycle is invalid")
            if node_id in visited:
                return
            node = nodes[node_id]
            dependencies = node["dependsOn"]
            if not isinstance(dependencies, Sequence) or isinstance(dependencies, (str, bytes, bytearray)):
                raise WorkflowRepositoryError("plan revision dependency selector is invalid")
            visiting.add(node_id)
            for dependency_id in dependencies:
                if dependency_id not in nodes:
                    raise WorkflowRepositoryError("plan revision dependency selector is invalid")
                visit(dependency_id)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in nodes:
            visit(node_id)
        for node_id in required_node_ids:
            node = nodes.get(node_id)
            if (
                node is None
                or node.get("businessPhase") != barrier_row[1]
                or node.get("barrierOrdinal") != barrier_row[2]
            ):
                raise WorkflowRepositoryError("required node is outside the current barrier")
            if node.get("required") is not True:
                raise WorkflowRepositoryError("required node role is inconsistent")
        plan_required_node_ids = {
            node_id
            for node_id, node in nodes.items()
            if node.get("businessPhase") == barrier_row[1]
            and node.get("barrierOrdinal") == barrier_row[2]
            and node.get("required") is True
        }
        if plan_required_node_ids != set(required_node_ids):
            raise WorkflowRepositoryError("plan required node selector is incomplete")
        raw_stage_maps = parsed_revision.get("stageMaps")
        if not isinstance(raw_stage_maps, Mapping):
            raise WorkflowRepositoryError("plan stage map registry is missing")
        stage_maps: dict[str, tuple[str, ...]] = {}
        for stage, stage_node_ids in raw_stage_maps.items():
            if (
                type(stage) is not str
                or not stage
                or not isinstance(stage_node_ids, Sequence)
                or isinstance(stage_node_ids, (str, bytes, bytearray))
            ):
                raise WorkflowRepositoryError("plan stage map registry is invalid")
            normalized_stage_nodes = tuple(stage_node_ids)
            if any(
                type(node_id) is not str or not node_id or node_id not in nodes for node_id in normalized_stage_nodes
            ):
                raise WorkflowRepositoryError("plan stage map node selector is invalid")
            if len(set(normalized_stage_nodes)) != len(normalized_stage_nodes):
                raise WorkflowRepositoryError("plan stage map node selector is duplicated")
            stage_maps[stage] = normalized_stage_nodes
        return _PlanBarrierAuthority(
            plan_revision_id=str(barrier_row[0]),
            plan_revision_digest=plan_revision_digest,
            business_phase=str(barrier_row[1]),
            barrier_ordinal=int(barrier_row[2]),
            pass_predicate_id=stored_pass_predicate_id,
            required_node_ids=tuple(required_node_ids),
            required_node_set_digest=stored_digest,
            settle_timeout_ms=stored_settle_timeout_ms,
            nodes=nodes,
            barriers=tuple(item for item in raw_barriers if isinstance(item, Mapping)),
            barrier_wire_index=barrier_wire_index,
            stage_maps=stage_maps,
        )

    def load_barrier_plan_authority(
        self,
        *,
        barrier_id: str,
        run_id: str,
        task_id: str,
        require_active_barrier: bool = True,
    ) -> _PlanBarrierAuthority:
        """暴露已验证 PlanRevision 投影；后继校验可跳过 active selector 但不跳过谱系。"""
        return self._load_required_node_authority(
            barrier_id=barrier_id,
            run_id=run_id,
            task_id=task_id,
            require_active_barrier=require_active_barrier,
        )

    def _require_successful_dependency_step(
        self,
        *,
        run_id: str,
        plan_revision_id: str,
        plan_revision_digest: str,
        dependency_id: str,
        plan_node: Mapping[str, object],
    ) -> None:
        """核对 dependsOn 的真实 Step、终态谓词和 Artifact，不允许摘要或 FAILED 依赖放行。"""
        dependency_cursor = self._connection.execute(
            "SELECT * FROM steps WHERE run_id=? AND plan_revision_id=? AND logical_node_id=? ORDER BY step_id",
            (run_id, plan_revision_id, dependency_id),
        )
        dependency_rows = dependency_cursor.fetchall()
        if len(dependency_rows) != 1:
            raise WorkflowRepositoryError("barrier dependency step is missing or ambiguous")
        columns = tuple(item[0] for item in dependency_cursor.description)
        record = dict(zip(columns, dependency_rows[0], strict=True))
        expected_phase = plan_node.get("businessPhase")
        expected_ordinal = plan_node.get("barrierOrdinal")
        if (
            record["logical_node_id"] != dependency_id
            or record["business_phase"] != expected_phase
            or record["plan_revision_id"] != plan_revision_id
            or type(expected_phase) is not str
            or type(expected_ordinal) is not int
        ):
            raise WorkflowRepositoryError("barrier dependency step lineage is inconsistent")
        try:
            expected_dependency_barrier_id = derive_barrier_id(
                run_id,
                plan_revision_digest,
                expected_phase,
                expected_ordinal,
            )
        except (FactoryError, TypeError, ValueError) as exc:
            raise WorkflowRepositoryError("barrier dependency identity cannot be derived") from exc
        if record["barrier_id"] != expected_dependency_barrier_id:
            raise WorkflowRepositoryError("barrier dependency identity is inconsistent")
        if (
            record["node_type"] != plan_node.get("nodeType")
            or record["side_effect_class"] != plan_node.get("sideEffectClass")
            or record["success_predicate_id"] != plan_node.get("successPredicateId")
            or record["timeout_ms"] != plan_node.get("timeoutMs")
            or record["retry_policy_id"] != plan_node.get("retryPolicyId")
            or type(record["required"]) is not int
            or record["required"] not in (0, 1)
            or bool(record["required"]) != (plan_node.get("required") is True)
        ):
            raise WorkflowRepositoryError("barrier dependency frozen role is inconsistent")
        raw_dependencies = plan_node.get("dependsOn")
        raw_required_artifacts = plan_node.get("requiredArtifacts")
        if (
            not isinstance(raw_dependencies, Sequence)
            or isinstance(raw_dependencies, (str, bytes, bytearray))
            or not isinstance(raw_required_artifacts, Sequence)
            or isinstance(raw_required_artifacts, (str, bytes, bytearray))
        ):
            raise WorkflowRepositoryError("barrier dependency selector registry is invalid")
        if record["dependency_hash"] != "sha256:" + hashlib.sha256(canonicalize(list(raw_dependencies))).hexdigest():
            raise WorkflowRepositoryError("barrier dependency digest is inconsistent")
        if record["required_artifacts_digest"] != (
            "sha256:" + hashlib.sha256(canonicalize(list(raw_required_artifacts))).hexdigest()
        ):
            raise WorkflowRepositoryError("barrier dependency artifact digest is inconsistent")
        if record["phase"] != StepPhase.TERMINAL.value or record["outcome"] != StepOutcome.SUCCEEDED.value:
            # Step 的冻结 success predicate 必须由权威终态满足，FAILED/NONE 不能作为依赖通过。
            raise WorkflowRepositoryError("barrier dependency success predicate has not passed")
        attempts_cursor = self._connection.execute(
            "SELECT phase,outcome,drain_state FROM attempts WHERE step_id=? ORDER BY attempt_id",
            (record["step_id"],),
        )
        attempts = attempts_cursor.fetchall()
        allowed_attempt_outcomes = {
            AttemptOutcome.SUCCEEDED.value,
            AttemptOutcome.FAILED.value,
            AttemptOutcome.INTERRUPTED.value,
            AttemptOutcome.KILLED.value,
            AttemptOutcome.LOST.value,
        }
        if any(
            phase != AttemptPhase.TERMINATED.value
            or drain_state not in {DrainState.NONE.value, DrainState.DRAINED.value}
            or outcome not in allowed_attempt_outcomes
            for phase, outcome, drain_state in attempts
        ):
            raise WorkflowRepositoryError("barrier dependency attempt is not terminal")
        if any(outcome == AttemptOutcome.UNKNOWN_REMOTE_STATE.value for _phase, outcome, _drain in attempts):
            raise WorkflowRepositoryError("barrier dependency attempt state is unknown")
        unsettled_receipt = self._connection.execute(
            "SELECT 1 FROM action_receipt_events AS r "
            "JOIN actions AS a ON a.action_id=r.action_id "
            "JOIN attempts AS at ON at.attempt_id=r.attempt_id "
            "WHERE at.step_id=? AND r.phase='STARTED' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM action_receipt_events AS later "
            "WHERE later.action_id=r.action_id AND later.receipt_seq>r.receipt_seq "
            "AND later.phase IN ('COMPLETED','ABSENT_CONFIRMED','FAILED_RECONCILED')) LIMIT 1",
            (record["step_id"],),
        ).fetchone()
        if unsettled_receipt is not None:
            raise WorkflowRepositoryError("barrier dependency receipt is unsettled")
        artifact_cursor = self._connection.execute(
            "SELECT ar.* FROM artifacts AS ar JOIN attempts AS at "
            "ON at.attempt_id=ar.producer_attempt_id WHERE at.step_id=? ORDER BY ar.artifact_id",
            (record["step_id"],),
        )
        artifact_columns = tuple(item[0] for item in artifact_cursor.description)
        committed_artifact_ids: set[str] = set()
        for artifact_values in artifact_cursor.fetchall():
            artifact_record = dict(zip(artifact_columns, artifact_values, strict=True))
            try:
                artifact = Artifact.from_record(artifact_record)
            except FactoryError as exc:
                raise WorkflowRepositoryError("barrier dependency artifact identity is invalid") from exc
            if artifact.is_gate_eligible:
                committed_artifact_ids.add(artifact.artifact_id)
        # requiredArtifacts 是门禁下限；同 Attempt 的额外 COMMITTED 产物不改变冻结需求。
        required_artifact_ids = set(raw_required_artifacts)
        if not required_artifact_ids.issubset(committed_artifact_ids):
            raise WorkflowRepositoryError("barrier dependency artifacts are incomplete")

    def load_barrier_authority(
        self,
        *,
        barrier_id: str,
        run_id: str,
        task_id: str,
    ) -> BarrierAuthorityFacts:
        """从权威表重算 barrier，拒绝空步骤和调用方自报的成功摘要。"""
        plan_authority = self._load_required_node_authority(
            barrier_id=barrier_id,
            run_id=run_id,
            task_id=task_id,
        )
        # barrier/target-close 同样先扫描 Run 全部 Attempt，不能让 terminal 坏 drain 事实绕过 active 查询。
        run_active_attempt = self.get_active_attempt_for_run(run_id)
        required_node_ids = plan_authority.required_node_ids
        barrier_revision_id = plan_authority.plan_revision_id
        barrier_phase = plan_authority.business_phase
        current_wire_index = plan_authority.barrier_wire_index.get((barrier_phase, plan_authority.barrier_ordinal))
        if current_wire_index is None:
            raise WorkflowRepositoryError("current barrier wire order is missing")
        required_node_set_digest = self._connection.execute(
            "SELECT required_node_set_digest FROM phase_barriers WHERE barrier_id=?",
            (barrier_id,),
        ).fetchone()[0]
        rows_cursor = self._connection.execute(
            "SELECT * FROM steps WHERE barrier_id=? ORDER BY step_id",
            (barrier_id,),
        )
        rows = rows_cursor.fetchall()
        if not rows:
            raise WorkflowRepositoryError("barrier has no authoritative steps")
        columns = tuple(item[0] for item in rows_cursor.description)
        facts: list[BarrierStepFacts] = []
        actual_node_ids: list[str] = []
        for values in rows:
            record = dict(zip(columns, values, strict=True))
            if (
                record["run_id"] != run_id
                or record["plan_revision_id"] != barrier_revision_id
                or record["business_phase"] != barrier_phase
            ):
                raise WorkflowRepositoryError("barrier step lineage is inconsistent")
            logical_node_id = record["logical_node_id"]
            if type(logical_node_id) is not str or not logical_node_id:
                raise WorkflowRepositoryError("barrier logical node selector is invalid")
            plan_node = plan_authority.nodes.get(logical_node_id)
            if plan_node is None:
                raise WorkflowRepositoryError("barrier step is not declared by the plan")
            plan_node_phase = plan_node.get("businessPhase")
            plan_node_ordinal = plan_node.get("barrierOrdinal")
            if type(plan_node_phase) is not str or type(plan_node_ordinal) is not int:
                raise WorkflowRepositoryError("barrier step plan selector is invalid")
            try:
                expected_step_barrier_id = derive_barrier_id(
                    run_id,
                    plan_authority.plan_revision_digest,
                    plan_node_phase,
                    plan_node_ordinal,
                )
            except (FactoryError, TypeError, ValueError) as exc:
                raise WorkflowRepositoryError("barrier step identity cannot be derived") from exc
            if expected_step_barrier_id != record["barrier_id"] or record["barrier_id"] != barrier_id:
                # Step 的 barrier 身份必须由 active PlanRevision node phase/ordinal 派生且指向当前 barrier。
                raise WorkflowRepositoryError("barrier step identity is inconsistent")
            actual_node_ids.append(logical_node_id)
            try:
                phase = StepPhase(record["phase"])
                outcome = StepOutcome(record["outcome"])
                required_value = record["required"]
                if type(required_value) is not int or required_value not in (0, 1):
                    raise ValueError("step required flag is invalid")
                persisted_required = required_value == 1
            except (TypeError, ValueError, KeyError) as exc:
                raise WorkflowRepositoryError("barrier step selector is invalid") from exc
            declared_required = logical_node_id in required_node_ids
            expected_role = {
                "business_phase": plan_node.get("businessPhase"),
                "node_type": plan_node.get("nodeType"),
                "required": plan_node.get("required"),
                "side_effect_class": plan_node.get("sideEffectClass"),
                "success_predicate_id": plan_node.get("successPredicateId"),
                "timeout_ms": plan_node.get("timeoutMs"),
                "retry_policy_id": plan_node.get("retryPolicyId"),
            }
            if (
                record["business_phase"] != expected_role["business_phase"]
                or record["node_type"] != expected_role["node_type"]
                or record["side_effect_class"] != expected_role["side_effect_class"]
                or record["success_predicate_id"] != expected_role["success_predicate_id"]
                or record["timeout_ms"] != expected_role["timeout_ms"]
                or record["retry_policy_id"] != expected_role["retry_policy_id"]
            ):
                raise WorkflowRepositoryError("barrier step role or predicate is inconsistent")
            if type(expected_role["required"]) is not bool or expected_role["required"] != declared_required:
                raise WorkflowRepositoryError("plan node required role is inconsistent")
            if persisted_required != declared_required:
                # required 身份由不可变 PlanRevision 派生，数据库漂移不能削弱 Artifact/成功谓词门禁。
                raise WorkflowRepositoryError("barrier step required flag is inconsistent")
            plan_dependencies = plan_node.get("dependsOn")
            plan_required_artifacts = plan_node.get("requiredArtifacts")
            if (
                not isinstance(plan_dependencies, Sequence)
                or isinstance(plan_dependencies, (str, bytes, bytearray))
                or not isinstance(plan_required_artifacts, Sequence)
                or isinstance(plan_required_artifacts, (str, bytes, bytearray))
            ):
                raise WorkflowRepositoryError("barrier step selector registry is invalid")
            expected_dependency_digest = "sha256:" + hashlib.sha256(canonicalize(list(plan_dependencies))).hexdigest()
            expected_required_artifacts_digest = (
                "sha256:" + hashlib.sha256(canonicalize(list(plan_required_artifacts))).hexdigest()
            )
            if (
                record["dependency_hash"] != expected_dependency_digest
                or record["required_artifacts_digest"] != expected_required_artifacts_digest
            ):
                # Step 的 selector 必须由冻结 node role 派生，任意摘要不能伪造 gate 输入。
                raise WorkflowRepositoryError("barrier step selector digest is inconsistent")
            for dependency_id in plan_dependencies:
                dependency_node = plan_authority.nodes.get(dependency_id)
                if dependency_node is None:
                    raise WorkflowRepositoryError("barrier dependency node is not declared")
                dependency_phase = dependency_node.get("businessPhase")
                dependency_ordinal = dependency_node.get("barrierOrdinal")
                if type(dependency_phase) is not str or type(dependency_ordinal) is not int:
                    raise WorkflowRepositoryError("barrier dependency barrier selector is invalid")
                dependency_wire_index = plan_authority.barrier_wire_index.get((dependency_phase, dependency_ordinal))
                if dependency_wire_index is None or dependency_wire_index > current_wire_index:
                    # 依赖只能指向当前或已完成 wire barrier，禁止未来 barrier 反向伪造当前 gate。
                    raise WorkflowRepositoryError("barrier dependency points to a future barrier")
                self._require_successful_dependency_step(
                    run_id=run_id,
                    plan_revision_id=barrier_revision_id,
                    plan_revision_digest=plan_authority.plan_revision_digest,
                    dependency_id=dependency_id,
                    plan_node=dependency_node,
                )
            required = declared_required
            active_attempt = (
                self._connection.execute(
                    "SELECT 1 FROM attempts WHERE step_id=? AND phase<>'TERMINATED' LIMIT 1",
                    (record["step_id"],),
                ).fetchone()
                is not None
            )
            unsettled_started_receipt = (
                self._connection.execute(
                    "SELECT 1 FROM action_receipt_events AS r "
                    "JOIN actions AS a ON a.action_id=r.action_id "
                    "JOIN attempts AS at ON at.attempt_id=r.attempt_id "
                    "WHERE at.step_id=? AND r.phase='STARTED' "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM action_receipt_events AS later "
                    "WHERE later.action_id=r.action_id AND later.receipt_seq>r.receipt_seq "
                    "AND later.phase IN ('COMPLETED','ABSENT_CONFIRMED','FAILED_RECONCILED')) LIMIT 1",
                    (record["step_id"],),
                ).fetchone()
                is not None
            )
            artifact_cursor = self._connection.execute(
                "SELECT ar.* FROM artifacts AS ar JOIN attempts AS at "
                "ON at.attempt_id=ar.producer_attempt_id "
                "WHERE at.step_id=? ORDER BY ar.artifact_id",
                (record["step_id"],),
            )
            artifact_rows = artifact_cursor.fetchall()
            artifact_columns = tuple(item[0] for item in artifact_cursor.description)
            committed_artifact_ids: set[str] = set()
            for artifact_values in artifact_rows:
                artifact_record = dict(zip(artifact_columns, artifact_values, strict=True))
                try:
                    artifact = Artifact.from_record(artifact_record)
                except FactoryError as exc:
                    raise WorkflowRepositoryError("barrier artifact identity is invalid") from exc
                if artifact.is_gate_eligible:
                    committed_artifact_ids.add(artifact.artifact_id)
            raw_required_artifact_ids = plan_node.get("requiredArtifacts", ())
            if not isinstance(raw_required_artifact_ids, Sequence) or isinstance(
                raw_required_artifact_ids, (str, bytes)
            ):
                raise WorkflowRepositoryError("plan artifact requirement is invalid")
            required_artifact_ids: tuple[object, ...] = tuple(raw_required_artifact_ids)
            if any(type(item) is not str or not item for item in required_artifact_ids):
                raise WorkflowRepositoryError("plan artifact requirement is invalid")
            # 仅要求冻结清单全部提交；额外合法产物不能把已满足的 required Step 判成失败。
            required_artifact_id_set = set(required_artifact_ids)
            required_artifacts_committed = required_artifact_id_set.issubset(committed_artifact_ids)
            attempt_unknown = (
                self._connection.execute(
                    "SELECT 1 FROM attempts WHERE step_id=? AND outcome='UNKNOWN_REMOTE_STATE' LIMIT 1",
                    (record["step_id"],),
                ).fetchone()
                is not None
            )
            if attempt_unknown:
                # Attempt-only UNKNOWN 也必须进入 barrier 的未知闭环，不能被终止 Step 的成功投影覆盖。
                outcome = StepOutcome.UNKNOWN_REMOTE_STATE
            facts.append(
                BarrierStepFacts(
                    step_id=str(record["step_id"]),
                    required=required,
                    phase=phase,
                    outcome=outcome,
                    active_attempt=active_attempt,
                    unsettled_started_receipt=unsettled_started_receipt,
                    # 谓词结果只能来自持久化终态 Step outcome，绝不采信调用方布尔值。
                    success_predicate_passed=phase is StepPhase.TERMINAL and outcome is StepOutcome.SUCCEEDED,
                    required_artifacts_committed=required_artifacts_committed,
                    blocking_finding_open=False,
                )
            )
        if len(set(actual_node_ids)) != len(actual_node_ids):
            raise WorkflowRepositoryError("barrier step node set is duplicated")
        if not set(required_node_ids).issubset(actual_node_ids):
            raise WorkflowRepositoryError("barrier required node set is incomplete")
        blocking_cursor = self._connection.execute(
            "SELECT COUNT(*) FROM review_findings "
            "WHERE task_id=? AND plan_revision_id=? AND UPPER(severity)='BLOCKING' "
            "AND UPPER(status) NOT IN ('CLOSED','RESOLVED','SUPERSEDED','DISMISSED')",
            (task_id, barrier_revision_id),
        )
        blocking_finding_count = int(blocking_cursor.fetchone()[0])
        if blocking_finding_count:
            facts = [
                BarrierStepFacts(
                    step_id=step.step_id,
                    required=step.required,
                    phase=step.phase,
                    outcome=step.outcome,
                    active_attempt=step.active_attempt,
                    unsettled_started_receipt=step.unsettled_started_receipt,
                    success_predicate_passed=step.success_predicate_passed,
                    required_artifacts_committed=step.required_artifacts_committed,
                    blocking_finding_open=True,
                )
                for step in facts
            ]
        active_attempt_count = 0 if run_active_attempt is None else 1
        unknown_remote_state = (
            self._connection.execute(
                "SELECT 1 FROM steps AS s WHERE s.run_id=? AND s.outcome='UNKNOWN_REMOTE_STATE' "
                "UNION ALL SELECT 1 FROM attempts AS a JOIN steps AS s ON s.step_id=a.step_id "
                "WHERE s.run_id=? AND a.outcome='UNKNOWN_REMOTE_STATE' LIMIT 1",
                (run_id, run_id),
            ).fetchone()
            is not None
        )
        required_artifacts_committed = all(not step.required or step.required_artifacts_committed for step in facts)
        return BarrierAuthorityFacts(
            steps=tuple(facts),
            active_attempt_count=active_attempt_count,
            unknown_remote_state=unknown_remote_state,
            required_artifacts_committed=required_artifacts_committed,
            blocking_finding_count=blocking_finding_count,
            required_node_ids=required_node_ids,
            required_node_set_digest=required_node_set_digest,
        )

    def validate_barrier_sequence(
        self,
        *,
        run_id: str,
        plan_revision_id: str,
        plan_revision_digest: str,
        barrier_specs: tuple[Mapping[str, object], ...],
        current_business_phase: str,
        current_barrier_ordinal: int,
    ) -> int:
        """在同一事务校验 barrier 的完整冻结序列、首个未通过项和后继未预通过。"""
        persisted_cursor = self._connection.execute(
            "SELECT barrier_id,plan_revision_id,business_phase,barrier_ordinal,settled,passed "
            "FROM phase_barriers WHERE run_id=? AND plan_revision_id=? ORDER BY barrier_ordinal,business_phase",
            (run_id, plan_revision_id),
        )
        persisted_rows = persisted_cursor.fetchall()
        if len(persisted_rows) != len(barrier_specs):
            raise WorkflowRepositoryError("barrier sequence projection is incomplete")
        persisted_by_key: dict[tuple[str, int], tuple[object, ...]] = {}
        for row in persisted_rows:
            phase = row[2]
            ordinal = row[3]
            if (
                type(phase) is not str
                or type(ordinal) is not int
                or ordinal < 0
                or (phase, ordinal) in persisted_by_key
                or type(row[4]) is not int
                or row[4] not in (0, 1)
                or type(row[5]) is not int
                or row[5] not in (0, 1)
            ):
                raise WorkflowRepositoryError("barrier sequence projection is invalid")
            persisted_by_key[(phase, ordinal)] = row
        current_index: int | None = None
        seen_keys: set[tuple[str, int]] = set()
        for index, spec in enumerate(barrier_specs):
            phase = spec.get("businessPhase")
            ordinal = spec.get("barrierOrdinal")
            if type(phase) is not str or type(ordinal) is not int or ordinal < 0:
                raise WorkflowRepositoryError("barrier sequence declaration is invalid")
            key = (phase, ordinal)
            if key in seen_keys:
                raise WorkflowRepositoryError("barrier sequence declaration is duplicated")
            seen_keys.add(key)
            persisted = persisted_by_key.get(key)
            if persisted is None:
                raise WorkflowRepositoryError("barrier sequence member is missing")
            try:
                expected_id = derive_barrier_id(run_id, plan_revision_digest, phase, ordinal)
            except (FactoryError, TypeError, ValueError) as exc:
                raise WorkflowRepositoryError("barrier sequence identity cannot be derived") from exc
            if persisted[0] != expected_id or persisted[1] != plan_revision_id:
                raise WorkflowRepositoryError("barrier sequence identity is inconsistent")
            if phase == current_business_phase and ordinal == current_barrier_ordinal:
                if current_index is not None:
                    raise WorkflowRepositoryError("current barrier is ambiguous")
                current_index = index
            if current_index is None or index < current_index:
                if persisted[4] != 1 or persisted[5] != 1:
                    raise WorkflowRepositoryError("barrier predecessor has not passed")
            elif current_index is not None and index > current_index:
                if persisted[4] != 0 or persisted[5] != 0:
                    raise WorkflowRepositoryError("barrier successor was prepassed")
        if current_index is None:
            raise WorkflowRepositoryError("current barrier is absent from frozen sequence")
        current_persisted = persisted_by_key[(current_business_phase, current_barrier_ordinal)]
        if current_persisted[4] != 0 or current_persisted[5] != 0:
            raise WorkflowRepositoryError("current barrier is already passed")
        future_keys = [
            key
            for index, spec in enumerate(barrier_specs)
            if index > current_index
            for key in [(spec.get("businessPhase"), spec.get("barrierOrdinal"))]
        ]
        for phase, ordinal in future_keys:
            if type(phase) is not str or type(ordinal) is not int:
                raise WorkflowRepositoryError("barrier successor selector is invalid")
            successor_id = persisted_by_key[(phase, ordinal)][0]
            step_cursor = self._connection.execute(
                "SELECT step_id,run_id,plan_revision_id,phase,outcome FROM steps WHERE barrier_id=?",
                (successor_id,),
            )
            successor_steps = step_cursor.fetchall()
            for step_id, step_run_id, step_plan_revision_id, step_phase, step_outcome in successor_steps:
                if step_run_id != run_id or step_plan_revision_id != plan_revision_id:
                    # barrier_id 是后继投影的唯一枚举入口；跨 Run/Revision 挂接必须 fail closed。
                    raise WorkflowRepositoryError("barrier successor step lineage is inconsistent")
                if (
                    step_phase not in {StepPhase.PENDING.value, StepPhase.READY.value}
                    or step_outcome != StepOutcome.NONE.value
                ):
                    # 后继只有未派发的 PENDING/READY 空壳可以预建，任何执行态或终态都意味着跨 barrier 提前执行。
                    raise WorkflowRepositoryError("barrier successor step was preexecuted")
                attempt_row = self._connection.execute(
                    "SELECT 1 FROM attempts WHERE step_id=? LIMIT 1",
                    (step_id,),
                ).fetchone()
                if attempt_row is not None:
                    raise WorkflowRepositoryError("barrier successor attempt was precreated")
                receipt_row = self._connection.execute(
                    "SELECT 1 FROM action_receipt_events AS r "
                    "JOIN attempts AS a ON a.attempt_id=r.attempt_id "
                    "WHERE a.step_id=? LIMIT 1",
                    (step_id,),
                ).fetchone()
                if receipt_row is not None:
                    raise WorkflowRepositoryError("barrier successor receipt was precreated")
        return current_index

    def update_run_control(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        desired_state: RunDesiredState,
        control_command_seq: int,
    ) -> Run:
        """同一次 Run CAS 更新 desired 与严格单调 control sequence。"""
        row = self._task1._cas(
            table="runs",
            identity_column="run_id",
            identity=run_id,
            expected_state_version=expected_state_version,
            changes={
                "desired_state": RunDesiredState(desired_state).value,
                "control_command_seq": control_command_seq,
            },
            aggregate_type="RUN",
        )
        return Run.from_record({key: row[key] for key in Run.__dataclass_fields__})

    def update_attempt_drain(
        self,
        *,
        attempt_id: str,
        expected_state_version: int,
        command_id: str,
    ) -> Attempt:
        """阻断命令原子锁定 Attempt 并置 DRAINING；RESUME 不调用本方法。"""
        row = self._task1._cas(
            table="attempts",
            identity_column="attempt_id",
            identity=attempt_id,
            expected_state_version=expected_state_version,
            changes={"interrupt_command_id": command_id, "drain_state": DrainState.DRAINING.value},
            aggregate_type="ATTEMPT",
        )
        return Attempt.from_record({key: row[key] for key in Attempt.__dataclass_fields__})

    def pass_phase_barrier(
        self,
        *,
        barrier_id: str,
        expected_state_version: int,
        gate_digest: str,
    ) -> PhaseBarrier:
        """用单次 CAS 固化 settled/passed 与其脱敏 gate 摘要。"""
        row = self._task1._cas(
            table="phase_barriers",
            identity_column="barrier_id",
            identity=barrier_id,
            expected_state_version=expected_state_version,
            changes={"settled": 1, "passed": 1, "gate_digest": gate_digest},
            aggregate_type="PHASE_BARRIER",
        )
        return self._hydrate_barrier(row)

    def block_run_for_action_required(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        reason_code: str,
    ) -> Run:
        """UNKNOWN 超时在同一 owner transaction 内落成 BLOCKED/ACTION_REQUIRED。"""
        current = self.get_run(run_id)
        if current is None:
            raise WorkflowRepositoryError("run does not exist")
        # 通过统一转换图校验 BLOCKED 边，CAS 失败时由 coordinator 整体回滚。
        require_observed_transition(current.observed_state, RunObservedState.BLOCKED)
        row = self._task1._cas(
            table="runs",
            identity_column="run_id",
            identity=run_id,
            expected_state_version=expected_state_version,
            changes={
                "observed_state": RunObservedState.BLOCKED.value,
                "requires_user_action": 1,
                "block_reason_code": reason_code,
            },
            aggregate_type="RUN",
        )
        record = {key: row[key] for key in Run.__dataclass_fields__}
        record["requires_user_action"] = bool(record["requires_user_action"])
        return Run.from_record(record)

    def advance_run_phase(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        candidate_phase: RunPhase,
        next_barrier_id: str | None,
    ) -> Run:
        """校验 phase 边后原子推进 Run 当前 barrier 投影。"""
        current = self.get_run(run_id)
        if current is None:
            raise WorkflowRepositoryError("run does not exist")
        if RunPhase(candidate_phase) is current.phase:
            current_barrier = self.get_phase_barrier(current.active_barrier_id) if current.active_barrier_id else None
            if current_barrier is None:
                raise WorkflowRepositoryError("same-phase progress has no current barrier")
            if next_barrier_id is None:
                future_row = self._connection.execute(
                    "SELECT 1 FROM phase_barriers WHERE run_id=? AND plan_revision_id=? AND barrier_ordinal>? LIMIT 1",
                    (run_id, current_barrier.plan_revision_id, current_barrier.barrier_ordinal),
                ).fetchone()
                if future_row is not None:
                    raise WorkflowRepositoryError("same-phase successor is required")
            else:
                next_barrier = self.get_phase_barrier(next_barrier_id)
                if (
                    next_barrier is None
                    or next_barrier.run_id != run_id
                    or next_barrier.plan_revision_id != current_barrier.plan_revision_id
                    or next_barrier.business_phase != current.phase.value
                    or next_barrier.barrier_ordinal <= current_barrier.barrier_ordinal
                ):
                    raise WorkflowRepositoryError("same-phase successor is invalid")
        else:
            require_phase_transition(current.phase, candidate_phase)
        row = self._task1._cas(
            table="runs",
            identity_column="run_id",
            identity=run_id,
            expected_state_version=expected_state_version,
            changes={
                "phase": RunPhase(candidate_phase).value,
                "active_barrier_id": next_barrier_id,
            },
            aggregate_type="RUN",
        )
        return Run.from_record({key: row[key] for key in Run.__dataclass_fields__})

    def complete_run(
        self,
        *,
        run_id: str,
        expected_state_version: int,
    ) -> Run:
        """目标达成时原子收口 Run，清除 active barrier 并写入 TERMINATED observed。"""
        current = self.get_run(run_id)
        if current is None:
            raise WorkflowRepositoryError("run does not exist")
        require_observed_transition(current.observed_state, RunObservedState.TERMINATED)
        row = self._task1._cas(
            table="runs",
            identity_column="run_id",
            identity=run_id,
            expected_state_version=expected_state_version,
            changes={
                "observed_state": RunObservedState.TERMINATED.value,
                "active_barrier_id": None,
            },
            aggregate_type="RUN",
        )
        record = {key: row[key] for key in Run.__dataclass_fields__}
        record["requires_user_action"] = bool(record["requires_user_action"])
        return Run.from_record(record)

    def advance_task_milestone(
        self,
        *,
        task_id: str,
        expected_state_version: int,
        candidate_stage: AchievedStage,
        lifecycle: TaskLifecycle,
        outcome_version: int,
    ) -> Task:
        """校验里程碑与可选终局后，在同一 Task CAS 增加 outcomeVersion。"""
        current = self.get_task(task_id)
        if current is None:
            raise WorkflowRepositoryError("task does not exist")
        require_achieved_stage_transition(current.achieved_stage, candidate_stage)
        target_lifecycle = TaskLifecycle(lifecycle)
        if target_lifecycle is not current.lifecycle:
            require_task_lifecycle_transition(current.lifecycle, target_lifecycle)
        row = self._task1._cas(
            table="tasks",
            identity_column="task_id",
            identity=task_id,
            expected_state_version=expected_state_version,
            changes={
                "achieved_stage": AchievedStage(candidate_stage).value,
                "lifecycle": target_lifecycle.value,
                "outcome_version": outcome_version,
            },
            aggregate_type="TASK",
        )
        return Task.from_record({key: row[key] for key in Task.__dataclass_fields__})

    def _transition_observed_projection(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        candidate: RunObservedState,
    ) -> Run:
        """共享的 Run observed CAS primitive；公开入口负责先完成各自授权。"""
        current = self.get_run(run_id)
        if current is None:
            raise WorkflowRepositoryError("run does not exist")
        require_observed_transition(current.observed_state, candidate)
        row = self._task1._cas(
            table="runs",
            identity_column="run_id",
            identity=run_id,
            expected_state_version=expected_state_version,
            changes={"observed_state": RunObservedState(candidate).value},
            aggregate_type="RUN",
        )
        return Run.from_record({key: row[key] for key in Run.__dataclass_fields__})

    def transition_control_plane_observed(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        candidate: RunObservedState,
    ) -> Run:
        """trusted control-plane 只能写非 RUNNING observed projection。"""
        try:
            target = RunObservedState(candidate)
        except (TypeError, ValueError) as exc:
            raise WorkflowRepositoryError("control-plane observed target is invalid") from exc
        if target is RunObservedState.RUNNING:
            raise WorkflowRepositoryError("RUNNING observed projection is executor-only")
        return self._transition_observed_projection(
            run_id=run_id,
            expected_state_version=expected_state_version,
            candidate=target,
        )

    def transition_executor_observed_running(
        self,
        *,
        run_id: str,
        attempt_id: str,
        expected_state_version: int,
        executor_id: str,
        fencing_token: int,
        control_epoch: int,
        now: datetime,
    ) -> tuple[Run, ExecutorWriteAuthority, WriteMode]:
        """用仓储权威上下文授权唯一 executor QUEUED→RUNNING 写。"""
        authority = self.load_executor_write_authority(
            run_id=run_id,
            attempt_id=attempt_id,
            expected_run_state_version=expected_state_version,
            expected_executor_id=executor_id,
            expected_fencing_token=fencing_token,
            expected_control_epoch=control_epoch,
            now=now,
        )
        if authority.run.observed_state is not RunObservedState.QUEUED:
            raise WorkflowRepositoryError("executor RUNNING projection requires QUEUED run")
        mode = require_write(authority.guard, WriteOperation.OBSERVED_STATE)
        updated = self._transition_observed_projection(
            run_id=run_id,
            expected_state_version=expected_state_version,
            candidate=RunObservedState.RUNNING,
        )
        return updated, authority, mode

    def guarded_executor_step_state(
        self,
        *,
        run_id: str,
        step_id: str,
        attempt_id: str,
        expected_run_state_version: int,
        expected_step_state_version: int,
        executor_id: str,
        fencing_token: int,
        control_epoch: int,
        candidate: StepPhase,
        now: datetime,
    ) -> tuple[Step, ExecutorWriteAuthority, WriteMode]:
        """NORMAL_WRITE guard 与 Step CAS 绑定在同一 UoW。"""
        authority = self.load_executor_write_authority(
            run_id=run_id,
            attempt_id=attempt_id,
            expected_run_state_version=expected_run_state_version,
            expected_executor_id=executor_id,
            expected_fencing_token=fencing_token,
            expected_control_epoch=control_epoch,
            now=now,
        )
        if authority.step.step_id != step_id:
            raise WorkflowRepositoryError("executor Step selector is outside the active Attempt lineage")
        mode = require_write(authority.guard, WriteOperation.STEP_STATE)
        try:
            target = StepPhase(candidate)
        except (TypeError, ValueError) as exc:
            raise WorkflowRepositoryError("executor Step target phase is invalid") from exc
        # phase-only 写入口只开放冻结的 executor 有向边，outcome/终态必须由专用原子操作维护。
        require_step_phase_transition(
            authority.step.phase,
            target,
            current_outcome=authority.step.outcome,
        )
        updated = self._task1.compare_and_set_step(
            step_id=step_id,
            expected_state_version=expected_step_state_version,
            phase=target.value,
        )
        return updated, authority, mode

    def guarded_executor_attempt_state(
        self,
        *,
        run_id: str,
        attempt_id: str,
        expected_run_state_version: int,
        expected_attempt_state_version: int,
        executor_id: str,
        fencing_token: int,
        control_epoch: int,
        candidate: AttemptPhase,
        now: datetime,
    ) -> tuple[Attempt, ExecutorWriteAuthority, WriteMode]:
        """普通 Attempt projection 只允许 NORMAL_WRITE，终止必须走独立入口。"""
        authority = self.load_executor_write_authority(
            run_id=run_id,
            attempt_id=attempt_id,
            expected_run_state_version=expected_run_state_version,
            expected_executor_id=executor_id,
            expected_fencing_token=fencing_token,
            expected_control_epoch=control_epoch,
            now=now,
        )
        mode = require_write(authority.guard, WriteOperation.ATTEMPT_STATE)
        try:
            target = AttemptPhase(candidate)
        except (TypeError, ValueError) as exc:
            raise WorkflowRepositoryError("executor Attempt target phase is invalid") from exc
        require_attempt_phase_transition(
            authority.attempt.phase,
            target,
            current_outcome=authority.attempt.outcome,
        )
        updated = self._task1.compare_and_set_attempt(
            attempt_id=attempt_id,
            expected_state_version=expected_attempt_state_version,
            phase=target.value,
        )
        return updated, authority, mode

    def guarded_executor_attempt_termination(
        self,
        *,
        run_id: str,
        attempt_id: str,
        expected_run_state_version: int,
        expected_attempt_state_version: int,
        executor_id: str,
        fencing_token: int,
        control_epoch: int,
        outcome: AttemptOutcome,
        ended_at: str,
        termination_reason: str | None,
        now: datetime,
    ) -> tuple[Attempt, ExecutorWriteAuthority, WriteMode]:
        """NORMAL/DRAIN_WRITE 均可原子写入 Attempt 终止闭集。"""
        authority = self.load_executor_write_authority(
            run_id=run_id,
            attempt_id=attempt_id,
            expected_run_state_version=expected_run_state_version,
            expected_executor_id=executor_id,
            expected_fencing_token=fencing_token,
            expected_control_epoch=control_epoch,
            now=now,
        )
        mode = require_write(authority.guard, WriteOperation.ATTEMPT_TERMINATION)
        try:
            target_outcome = AttemptOutcome(outcome)
        except (TypeError, ValueError) as exc:
            raise WorkflowRepositoryError("Attempt termination outcome is invalid") from exc
        require_attempt_termination_transition(
            authority.attempt.phase,
            current_outcome=authority.attempt.outcome,
            terminal_outcome=target_outcome,
        )
        if termination_reason is not None and (type(termination_reason) is not str or not termination_reason):
            raise WorkflowRepositoryError("Attempt termination reason is invalid")
        # NORMAL 保持 NONE；只有 DRAIN termination 才把配套 drain 事实推进到 DRAINED。
        terminal_drain_state = DrainState.DRAINED if mode is WriteMode.DRAIN_WRITE else DrainState.NONE
        candidate_attempt = replace(
            authority.attempt,
            phase=AttemptPhase.TERMINATED,
            outcome=target_outcome,
            drain_state=terminal_drain_state,
            ended_at=ended_at,
            termination_reason=termination_reason,
        )
        # 写端与读端复用同一 UTC chronology，确保未来 ended_at 不会先 CAS 再靠外层补救。
        _validate_attempt_projection(candidate_attempt, now=now.astimezone(UTC))
        row = self._task1._cas(
            table="attempts",
            identity_column="attempt_id",
            identity=attempt_id,
            expected_state_version=expected_attempt_state_version,
            changes={
                "phase": AttemptPhase.TERMINATED.value,
                "outcome": target_outcome.value,
                "drain_state": terminal_drain_state.value,
                "ended_at": ended_at,
                "termination_reason": termination_reason,
            },
            aggregate_type="ATTEMPT",
        )
        updated = Attempt.from_record({key: row[key] for key in Attempt.__dataclass_fields__})
        return updated, authority, mode

    def append_superseding_attempt(
        self,
        previous: Attempt,
        candidate: Attempt,
        *,
        now: datetime,
    ) -> None:
        """在 owner write transaction 内幂等追加唯一 QUEUED 后继。"""
        authority_now = _attempt_dispatch_utc(now)
        if not self._connection.in_transaction:
            raise WorkflowRepositoryError("attempt dispatch requires an owner write transaction")
        if not isinstance(previous, Attempt) or not isinstance(candidate, Attempt):
            raise WorkflowRepositoryError("attempt dispatch selector is invalid")
        stored_previous = self.get_attempt(previous.attempt_id)
        if stored_previous is None or stored_previous != previous:
            raise WorkflowRepositoryError("previous attempt is not the authoritative selector")

        successor_ids = tuple(
            str(row[0])
            for row in self._connection.execute(
                "SELECT attempt_id FROM attempts WHERE supersedes_attempt_id=? ORDER BY attempt_id",
                (stored_previous.attempt_id,),
            ).fetchall()
        )
        if len(successor_ids) > 1:
            # 多个直接后继代表持久事实已经歧义，不能任选一行伪装成幂等重放。
            raise WorkflowRepositoryError("predecessor has multiple direct successors")
        exact_replay = False
        if successor_ids:
            successor = self.get_attempt(successor_ids[0])
            if successor == candidate:
                exact_replay = True
            else:
                raise WorkflowRepositoryError("predecessor already has a different successor")

        run_id_row = self._connection.execute(
            "SELECT s.run_id FROM steps AS s WHERE s.step_id=?",
            (stored_previous.step_id,),
        ).fetchone()
        if run_id_row is None:
            raise WorkflowRepositoryError("attempt step lineage is missing")
        run = self.get_run(str(run_id_row[0]))
        if run is None:
            raise WorkflowRepositoryError("attempt run lineage is missing")
        # exact replay 也必须在 return 前验证 predecessor、candidate 及所有 sibling；顺序不能被幂等短路。
        run_attempts = self._run_attempts_with_valid_drain_authority(run)
        for persisted_attempt in run_attempts:
            _validate_attempt_projection(persisted_attempt, now=authority_now)
        if exact_replay:
            allowed_active_ids = {stored_previous.attempt_id, candidate.attempt_id}
        else:
            allowed_active_ids = {stored_previous.attempt_id}
        other_active = next(
            (
                attempt
                for attempt in run_attempts
                if attempt.phase is not AttemptPhase.TERMINATED and attempt.attempt_id not in allowed_active_ids
            ),
            None,
        )
        if other_active is not None:
            raise WorkflowRepositoryError("another active attempt already exists for the run")
        # replay 只豁免重复插入和瞬态 freshness 消费；predecessor/candidate 的不可变 dispatch 形状仍须重验。
        if (
            stored_previous.phase is not AttemptPhase.TERMINATED
            or stored_previous.drain_state is not DrainState.DRAINED
        ):
            raise WorkflowRepositoryError("previous attempt is not fully drained")
        if candidate.supersedes_attempt_id != stored_previous.attempt_id:
            raise WorkflowRepositoryError("candidate must supersede the authoritative previous attempt")
        if candidate.accepted_control_command_seq != run.control_command_seq:
            raise WorkflowRepositoryError("candidate control sequence is not the authoritative Run sequence")
        if candidate.accepted_control_command_seq <= stored_previous.accepted_control_command_seq:
            raise WorkflowRepositoryError("candidate control sequence is not fresh")
        if candidate.control_epoch <= stored_previous.control_epoch:
            raise WorkflowRepositoryError("candidate control epoch is not fresh")
        try:
            expected = stored_previous.supersede(
                new_attempt_id=candidate.attempt_id,
                fencing_token=candidate.fencing_token,
                control_epoch=candidate.control_epoch,
                accepted_control_command_seq=candidate.accepted_control_command_seq,
            )
        except FactoryError as exc:
            raise WorkflowRepositoryError("candidate dispatch facts are invalid") from exc
        if (
            candidate != expected
            or candidate.phase is not AttemptPhase.CREATED
            or candidate.outcome is not AttemptOutcome.NONE
        ):
            raise WorkflowRepositoryError("candidate must be a fresh CREATED Attempt")
        if exact_replay:
            # 同一 candidate 经全 Run 与共同 dispatch 校验后幂等返回，不重复消费瞬态 freshness。
            return
        try:
            require_new_attempt_dispatch(
                observed_state=run.observed_state,
                new_attempt_id=candidate.attempt_id,
                previous_attempt_id=stored_previous.attempt_id,
            )
        except FactoryError as exc:
            raise WorkflowRepositoryError("new attempt dispatch state is invalid") from exc
        if run.desired_state is not RunDesiredState.RUNNING:
            raise WorkflowRepositoryError("new attempt dispatch requires desired RUNNING")
        prior_authorization = self._connection.execute(
            "SELECT MAX(fencing_token), MAX(control_epoch), MAX(accepted_control_command_seq) "
            "FROM execution_authorizations WHERE run_id=? AND step_id=?",
            (run.run_id, candidate.step_id),
        ).fetchone()
        if prior_authorization is not None:
            max_fencing, max_control_epoch, max_control_seq = prior_authorization
            if max_fencing is not None and candidate.fencing_token <= int(max_fencing):
                raise WorkflowRepositoryError("candidate fencing token is not fresh relative to authorization")
            if max_control_epoch is not None and candidate.control_epoch <= int(max_control_epoch):
                raise WorkflowRepositoryError("candidate control epoch is not fresh relative to authorization")
            if max_control_seq is not None and candidate.accepted_control_command_seq <= int(max_control_seq):
                raise WorkflowRepositoryError("candidate control sequence is not fresh relative to authorization")
        try:
            self._task1.insert_attempt(
                {
                    field: getattr(candidate, field).value
                    if hasattr(getattr(candidate, field), "value")
                    else getattr(candidate, field)
                    for field in Attempt.__dataclass_fields__
                }
            )
        except sqlite3.IntegrityError as exc:
            # BEGIN IMMEDIATE 已串行化正常竞争；剩余约束冲突统一暴露稳定仓储错误。
            raise WorkflowRepositoryError("candidate attempt insert violated persistence constraints") from exc


__all__ = [
    "BarrierAuthorityFacts",
    "ExecutorWriteAuthority",
    "ObservedStateAuthorityFacts",
    "SqliteWorkflowRepository",
    "WorkflowRepositoryError",
]
