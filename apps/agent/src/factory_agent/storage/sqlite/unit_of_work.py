"""SQLite 同连接仓储与五路 CAS/authoritative-event 原子配对。"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

import jsonschema  # type: ignore[import-untyped]
from factory_agent.contracts.generated.models import AUTHORITATIVE_STATE_EVENT_V1_SCHEMA_JSON
from factory_agent.domain import authorization as authorization_domain
from factory_agent.domain.events import payload_digest
from factory_agent.domain.plans import PlanRevisionBundle
from factory_agent.domain.resources import ResourceLease, TargetGuard
from factory_agent.domain.workflow import Attempt, PhaseBarrier, Run, Step, Task
from factory_agent.errors import FactoryError
from factory_agent.observability.logging import get_logger
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.policy.plan_hash import factory_format_checker

LOGGER = get_logger(__name__)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """解析持久 JCS 时拒绝重复键，避免后写值覆盖已签发 selector。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate authorization key")
        result[key] = value
    return result


def _decode_canonical_authorization_object(blob: object) -> dict[str, object]:
    """严格解码授权 JCS BLOB；类型、UTF-8、对象形状和逐字节 canonical 均须可信。"""
    if not isinstance(blob, bytes):
        raise authorization_domain.AuthorizationContractError("authorization persistence validation failed")
    try:
        parsed: object = json.loads(
            blob.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        if not isinstance(parsed, dict) or canonicalize(parsed) != blob:
            raise ValueError("authorization blob is not an exact canonical object")
        return parsed
    except authorization_domain.AuthorizationContractError:
        raise
    except Exception:  # noqa: BLE001 - 解析细节与合同正文不得越过仓储边界。
        raise authorization_domain.AuthorizationContractError("authorization persistence validation failed") from None


def _validate_authorization_record(
    record: Mapping[str, object],
    *,
    parser: Callable[[Mapping[str, object]], authorization_domain.ParsedAuthorization],
    expected_record: Callable[[authorization_domain.ParsedAuthorization, Mapping[str, object]], dict[str, object]],
) -> None:
    """用领域 parser 重验合同，再把全部 SQL selector 与 canonical digest 逐项闭合。"""
    contract = _decode_canonical_authorization_object(record.get("canonical_contract"))
    parsed = parser(contract)
    expected = expected_record(parsed, contract)
    if frozenset(record) != frozenset(expected) or any(record[name] != value for name, value in expected.items()):
        raise authorization_domain.AuthorizationContractError("authorization persistence mismatch")


def _intent_authorization_record(
    parsed: authorization_domain.ParsedAuthorization,
    contract: Mapping[str, object],
) -> dict[str, object]:
    """从已解析 Intent 合同重建全部 SQLite 投影，防止 raw selector 漂移。"""
    return {
        "intent_authorization_id": contract["intentAuthorizationId"],
        "task_id": contract["taskId"],
        "user_id": contract["userId"],
        "requirement_digest": canonicalize(contract["requirementDigest"]),
        "project_id": contract["projectId"],
        "repository_id": contract["repositoryId"],
        "repository_binding_digest": canonicalize(contract["repositoryBindingDigest"]),
        "baseline_digest": canonicalize(contract["baselineDigest"]),
        "target_stage": contract["targetStage"],
        "stage_capability_map_version": contract["stageCapabilityMapVersion"],
        "allowed_capability_set_digest": canonicalize(contract["allowedCapabilitySetDigest"]),
        "target_binding_digest": canonicalize(contract["targetBindingDigest"]),
        "risk_ceiling": contract["riskCeiling"],
        "estimated_cost_alert_digest": canonicalize(contract["estimatedCostAlertDigest"]),
        "autonomous_execution_budget_ms": contract["autonomousExecutionBudgetMs"],
        "repair_loop_limit": contract["repairLoopLimit"],
        "auto_replan_limit": contract["autoReplanLimit"],
        "attempt_limit": contract["attemptLimit"],
        "issued_at": contract["issuedAt"],
        "expires_at": contract["expiresAt"],
        "revoked_at": contract["revokedAt"],
        "revoke_reason": contract["revokeReason"],
        "contract_schema_id": parsed.schema_id,
        "contract_schema_version": parsed.schema_version,
        "canonical_contract": parsed.canonical_bytes,
        "canonical_contract_digest": canonicalize({"digest": parsed.contract_digest}),
    }


def _execution_authorization_record(
    parsed: authorization_domain.ParsedAuthorization,
    contract: Mapping[str, object],
) -> dict[str, object]:
    """从已解析 Execution 合同重建全部 SQLite 投影，不解释后续消费策略。"""
    return {
        "execution_authorization_id": contract["executionAuthorizationId"],
        "intent_authorization_id": contract["intentAuthorizationId"],
        "plan_revision_id": contract["planRevisionId"],
        "semantic_plan_hash": canonicalize(contract["semanticPlanHash"]),
        "plan_revision_digest": canonicalize(contract["planRevisionDigest"]),
        "stage_capability_map_version": contract["stageCapabilityMapVersion"],
        "stage_capability_map_digest": canonicalize(contract["stageCapabilityMapDigest"]),
        "node_capability_map_version": contract["nodeCapabilityMapVersion"],
        "node_capability_map_digest": canonicalize(contract["nodeCapabilityMapDigest"]),
        "run_id": contract["runId"],
        "step_id": contract["stepId"],
        "attempt_id": contract["attemptId"],
        "node_type": contract["nodeType"],
        "executor_id": contract["executorId"],
        "resource_fingerprint": canonicalize(contract["resourceFingerprint"]),
        "capability_scope_digest": canonicalize(contract["capabilityScopeDigest"]),
        "idempotency_key": canonicalize(contract["idempotencyKey"]),
        "input_bindings": canonicalize(contract["inputBindings"]),
        "action_capability": contract["actionCapability"],
        "action_policy_snapshot_digest": canonicalize(contract["actionPolicySnapshotDigest"]),
        "fencing_token": contract["fencingToken"],
        "control_epoch": contract["controlEpoch"],
        "accepted_control_command_seq": contract["acceptedControlCommandSeq"],
        "max_uses": contract["maxUses"],
        "consumption_state": contract["consumptionState"],
        "issued_at": contract["issuedAt"],
        "expires_at": contract["expiresAt"],
        "revoked_at": contract["revokedAt"],
        "revoke_reason": contract["revokeReason"],
        "contract_schema_id": parsed.schema_id,
        "contract_schema_version": parsed.schema_version,
        "canonical_contract": parsed.canonical_bytes,
        "canonical_contract_digest": canonicalize({"digest": parsed.contract_digest}),
    }


class StaleStateVersionError(FactoryError):
    """CAS rowcount 为零时拒绝自动重读重试。"""

    error_code = "STALE_STATE_VERSION"


class StateEventPairingError(FactoryError):
    """待提交 CAS 与 state event 不是一一双射。"""

    error_code = "STATE_EVENT_PAIRING_MISMATCH"


class StateEventIdentityError(FactoryError):
    """event aggregate identity 或真实 lineage 与投影不一致。"""

    error_code = "STATE_EVENT_IDENTITY_MISMATCH"


class StateEventVersionError(FactoryError):
    """event previous/new version 与 CAS 事实不一致。"""

    error_code = "STATE_EVENT_VERSION_MISMATCH"


class StateEventValidationError(FactoryError):
    """authoritative event schema 或 payload digest 校验失败。"""

    error_code = "INVALID_AUTHORITATIVE_STATE_EVENT"


class StorageIdentifierError(FactoryError):
    """调用方传入未知表/列名时 fail closed，禁止 Mapping key 进入 SQL identifier。"""

    error_code = "INVALID_STORAGE_IDENTIFIER"


_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "projects": frozenset({"project_id"}),
    "tasks": frozenset(
        {
            "task_id",
            "project_id",
            "lifecycle",
            "target_stage",
            "achieved_stage",
            "active_plan_revision_id",
            "active_run_id",
            "state_version",
            "outcome_version",
            "intake_schema_id",
            "intake_schema_version",
            "canonical_intake",
            "intake_digest",
        }
    ),
    "plan_revisions": frozenset(
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
    ),
    "phase_barriers": frozenset(
        {
            "barrier_id",
            "run_id",
            "plan_revision_id",
            "business_phase",
            "barrier_ordinal",
            "required_node_set_digest",
            "settle_timeout_ms",
            "settle_deadline_at",
            "pass_predicate_id",
            "settled",
            "passed",
            "gate_digest",
            "state_version",
        }
    ),
    "runs": frozenset(
        {
            "run_id",
            "task_id",
            "desired_state",
            "observed_state",
            "phase",
            "active_barrier_id",
            "dag_version",
            "run_cursor",
            "durable_cursor",
            "control_command_seq",
            "repair_loop_used",
            "auto_replan_used",
            "requires_user_action",
            "block_reason_code",
            "budget_accumulated_ms",
            "budget_clock_state",
            "budget_clock_boot_id",
            "budget_clock_monotonic_ns",
            "budget_clock_wall_time",
            "budget_suspension_reason",
            "executor_id",
            "host_id",
            "runtime",
            "protocol_version",
            "recovery_target_phase",
            "state_version",
        }
    ),
    "steps": frozenset(
        {
            "step_id",
            "run_id",
            "plan_revision_id",
            "barrier_id",
            "logical_node_id",
            "business_phase",
            "node_type",
            "required",
            "side_effect_class",
            "phase",
            "outcome",
            "dependency_hash",
            "required_artifacts_digest",
            "success_predicate_id",
            "timeout_ms",
            "retry_policy_id",
            "idempotency_key",
            "state_version",
        }
    ),
    "attempts": frozenset(
        {
            "attempt_id",
            "step_id",
            "supersedes_attempt_id",
            "phase",
            "outcome",
            "executor_id",
            "process_session_id",
            "pid",
            "process_start_time",
            "job_object_id",
            "wsl_distro",
            "container_id",
            "image_digest",
            "exit_code",
            "termination_reason",
            "fencing_token",
            "control_epoch",
            "accepted_control_command_seq",
            "interrupt_command_id",
            "drain_state",
            "started_at",
            "ended_at",
            "state_version",
        }
    ),
    "authoritative_state_events": frozenset(
        {
            "state_event_id",
            "schema_version",
            "task_id",
            "scope",
            "aggregate_type",
            "aggregate_id",
            "run_id",
            "step_id",
            "attempt_id",
            "previous_state_version",
            "state_version",
            "payload_digest",
            "canonical_state_event",
        }
    ),
    "intent_authorizations": frozenset(
        {
            "intent_authorization_id",
            "task_id",
            "user_id",
            "requirement_digest",
            "project_id",
            "repository_id",
            "repository_binding_digest",
            "baseline_digest",
            "target_stage",
            "stage_capability_map_version",
            "allowed_capability_set_digest",
            "target_binding_digest",
            "risk_ceiling",
            "estimated_cost_alert_digest",
            "autonomous_execution_budget_ms",
            "repair_loop_limit",
            "auto_replan_limit",
            "attempt_limit",
            "issued_at",
            "expires_at",
            "revoked_at",
            "revoke_reason",
            "contract_schema_id",
            "contract_schema_version",
            "canonical_contract",
            "canonical_contract_digest",
        }
    ),
    "execution_authorizations": frozenset(
        {
            "execution_authorization_id",
            "intent_authorization_id",
            "plan_revision_id",
            "semantic_plan_hash",
            "plan_revision_digest",
            "stage_capability_map_version",
            "stage_capability_map_digest",
            "node_capability_map_version",
            "node_capability_map_digest",
            "run_id",
            "step_id",
            "attempt_id",
            "node_type",
            "executor_id",
            "resource_fingerprint",
            "capability_scope_digest",
            "idempotency_key",
            "input_bindings",
            "action_capability",
            "action_policy_snapshot_digest",
            "fencing_token",
            "control_epoch",
            "accepted_control_command_seq",
            "max_uses",
            "consumption_state",
            "issued_at",
            "expires_at",
            "revoked_at",
            "revoke_reason",
            "contract_schema_id",
            "contract_schema_version",
            "canonical_contract",
            "canonical_contract_digest",
        }
    ),
    "resource_leases": frozenset(
        {
            "resource_key",
            "owner_executor_id",
            "fencing_token",
            "control_epoch",
            "acquired_at",
            "heartbeat_at",
            "expires_at",
            "state_version",
        }
    ),
    "target_guards": frozenset(
        {
            "resource_fingerprint",
            "guard_state",
            "reason_code",
            "evidence_digest",
            "created_by_release_id",
            "created_at",
            "cleared_by",
            "cleared_at",
            "clear_receipt_digest",
            "state_version",
        }
    ),
}


def _quote_identifier(identifier: str) -> str:
    """仅对已通过 allowlist 的 SQLite identifier 加引号；这里不做任意字符串转义。"""
    return f'"{identifier}"'


def _known_columns(table: str) -> frozenset[str]:
    """表名也必须来自仓储内冻结集合，避免 public helper 被扩展成通用 SQL 拼接器。"""
    columns = _TABLE_COLUMNS.get(table)
    if columns is None:
        raise StorageIdentifierError("unknown SQLite storage table")
    return columns


def _validated_columns(table: str, columns: Iterable[str]) -> tuple[str, ...]:
    """校验 insert/select/update 的列名闭集，重复列同样拒绝以避免 SET 覆盖歧义。"""
    known = _known_columns(table)
    validated: list[str] = []
    seen: set[str] = set()
    for column in columns:
        if column not in known or column in seen:
            raise StorageIdentifierError("invalid SQLite storage column")
        validated.append(column)
        seen.add(column)
    return tuple(validated)


def _validated_update_columns(table: str, identity_column: str, columns: Iterable[str]) -> tuple[str, ...]:
    """CAS 只允许更新业务列；identity 与 state_version 由仓储边界固定维护。"""
    validated = _validated_columns(table, columns)
    if not validated or identity_column in validated or "state_version" in validated:
        raise StorageIdentifierError("invalid SQLite storage update column")
    return validated


@dataclass(frozen=True, slots=True)
class _PendingCas:
    aggregate_type: str
    aggregate_id: str
    task_id: str
    run_id: str | None
    step_id: str | None
    attempt_id: str | None
    previous_version: int
    state_version: int


class _SqlStore:
    """所有仓储共享一个 owner connection；表/列只来自冻结常量。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @property
    def connection_identity(self) -> int:
        return id(self._connection)

    def _insert(self, table: str, record: Mapping[str, object]) -> None:
        columns = _validated_columns(table, record)
        quoted_table = _quote_identifier(table)
        quoted = ",".join(_quote_identifier(column) for column in columns)
        placeholders = ",".join("?" for _ in columns)
        self._connection.execute(
            f"INSERT INTO {quoted_table} ({quoted}) VALUES ({placeholders})",  # noqa: S608
            tuple(record[column] for column in columns),
        )

    def _get(self, table: str, identity_column: str, identity: str) -> dict[str, object] | None:
        (identity_column,) = _validated_columns(table, (identity_column,))
        quoted_table = _quote_identifier(table)
        quoted_identity = _quote_identifier(identity_column)
        cursor = self._connection.execute(
            f"SELECT * FROM {quoted_table} WHERE {quoted_identity}=?",  # noqa: S608
            (identity,),
        )
        row = cursor.fetchone()
        return None if row is None else dict(zip((item[0] for item in cursor.description), row, strict=True))


class WorkflowRepository(_SqlStore):
    """工作流 insert/get 与五路版本 CAS；每次成功 CAS 登记待配对事实。"""

    def __init__(self, uow: SqliteUnitOfWork) -> None:
        super().__init__(uow._connection)
        self._uow = uow

    def insert_project(self, record: Mapping[str, object]) -> None:
        self._insert("projects", record)

    def insert_task(self, record: Mapping[str, object]) -> None:
        self._insert("tasks", record)

    def get_task(self, task_id: str) -> dict[str, object] | None:
        return self._get("tasks", "task_id", task_id)

    def append_plan_revision(self, record: Mapping[str, object]) -> None:
        # 持久 selector 与双 canonical blob 必须先经领域 hydrator 重验，SQLite 形状不是信任边界。
        PlanRevisionBundle.from_persistence_record(record)
        self._insert("plan_revisions", record)

    def get_plan_revision(self, plan_revision_id: str) -> dict[str, object] | None:
        record = self._get("plan_revisions", "plan_revision_id", plan_revision_id)
        if record is not None:
            PlanRevisionBundle.from_persistence_record(record)
        return record

    def insert_run(self, record: Mapping[str, object]) -> None:
        self._insert("runs", record)

    def get_run(self, run_id: str) -> dict[str, object] | None:
        return self._get("runs", "run_id", run_id)

    def insert_phase_barrier(self, record: Mapping[str, object]) -> None:
        self._insert("phase_barriers", record)

    def get_phase_barrier(self, barrier_id: str) -> dict[str, object] | None:
        return self._get("phase_barriers", "barrier_id", barrier_id)

    def insert_step(self, record: Mapping[str, object]) -> None:
        self._insert("steps", record)

    def get_step(self, step_id: str) -> dict[str, object] | None:
        return self._get("steps", "step_id", step_id)

    def insert_attempt(self, record: Mapping[str, object]) -> None:
        self._insert("attempts", record)

    def get_attempt(self, attempt_id: str) -> dict[str, object] | None:
        return self._get("attempts", "attempt_id", attempt_id)

    def _cas(
        self,
        *,
        table: str,
        identity_column: str,
        identity: str,
        expected_state_version: int,
        changes: Mapping[str, object],
        aggregate_type: str,
    ) -> dict[str, object]:
        columns = _validated_update_columns(table, identity_column, changes)
        quoted_table = _quote_identifier(table)
        quoted_identity = _quote_identifier(identity_column)
        setters = ",".join(f"{_quote_identifier(column)}=?" for column in columns)
        # 标识符必须先走冻结 allowlist；业务输入始终走参数绑定，避免 Mapping key 注入 SQL 结构。
        cursor = self._connection.execute(
            f"UPDATE {quoted_table} SET {setters}, state_version=state_version+1 "  # noqa: S608
            f"WHERE {quoted_identity}=? AND state_version=?",
            (*(changes[column] for column in columns), identity, expected_state_version),
        )
        if cursor.rowcount != 1:
            raise StaleStateVersionError("stale aggregate version")
        self._uow._failure_probe("after_state_cas")
        row = self._get(table, identity_column, identity)
        if row is None:
            raise StateEventIdentityError("CAS row disappeared before lineage validation")
        task_id, run_id, step_id, attempt_id = self._lineage(aggregate_type, row)
        self._uow._pending_cas.append(
            _PendingCas(
                aggregate_type=aggregate_type,
                aggregate_id=identity,
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                attempt_id=attempt_id,
                previous_version=expected_state_version,
                state_version=expected_state_version + 1,
            )
        )
        return row

    def _lineage(
        self,
        aggregate_type: str,
        row: Mapping[str, object],
    ) -> tuple[str, str | None, str | None, str | None]:
        """从真实外表链计算 task/run/step/attempt，禁止信任 event 自报 lineage。"""
        if aggregate_type == "TASK":
            return str(row["task_id"]), None, None, None
        if aggregate_type == "RUN":
            return str(row["task_id"]), str(row["run_id"]), None, None
        if aggregate_type == "PHASE_BARRIER":
            run = self._get("runs", "run_id", str(row["run_id"]))
            if run is None:
                raise StateEventIdentityError("phase barrier lineage is missing")
            return str(run["task_id"]), str(row["run_id"]), None, None
        if aggregate_type == "STEP":
            run = self._get("runs", "run_id", str(row["run_id"]))
            if run is None:
                raise StateEventIdentityError("step lineage is missing")
            return str(run["task_id"]), str(row["run_id"]), str(row["step_id"]), None
        step = self._get("steps", "step_id", str(row["step_id"]))
        if step is None:
            raise StateEventIdentityError("attempt step lineage is missing")
        run = self._get("runs", "run_id", str(step["run_id"]))
        if run is None:
            raise StateEventIdentityError("attempt run lineage is missing")
        return str(run["task_id"]), str(step["run_id"]), str(row["step_id"]), str(row["attempt_id"])

    def compare_and_set_task(
        self, *, task_id: str, expected_state_version: int, achieved_stage: str, outcome_version: int
    ) -> Task:
        row = self._cas(
            table="tasks",
            identity_column="task_id",
            identity=task_id,
            expected_state_version=expected_state_version,
            changes={"achieved_stage": achieved_stage, "outcome_version": outcome_version},
            aggregate_type="TASK",
        )
        return Task.from_record({key: row[key] for key in Task.__dataclass_fields__})

    def compare_and_set_run(self, *, run_id: str, expected_state_version: int, observed_state: str) -> Run:
        row = self._cas(
            table="runs",
            identity_column="run_id",
            identity=run_id,
            expected_state_version=expected_state_version,
            changes={"observed_state": observed_state},
            aggregate_type="RUN",
        )
        return Run.from_record({key: row[key] for key in Run.__dataclass_fields__})

    def compare_and_set_phase_barrier(
        self, *, barrier_id: str, expected_state_version: int, settled: bool, passed: bool
    ) -> PhaseBarrier:
        row = self._cas(
            table="phase_barriers",
            identity_column="barrier_id",
            identity=barrier_id,
            expected_state_version=expected_state_version,
            changes={"settled": int(settled), "passed": int(passed)},
            aggregate_type="PHASE_BARRIER",
        )
        return PhaseBarrier.from_record(
            {key: row[key] for key in PhaseBarrier.__dataclass_fields__ if not key.startswith("_")}
        )

    def compare_and_set_step(self, *, step_id: str, expected_state_version: int, phase: str) -> Step:
        row = self._cas(
            table="steps",
            identity_column="step_id",
            identity=step_id,
            expected_state_version=expected_state_version,
            changes={"phase": phase},
            aggregate_type="STEP",
        )
        return Step.from_record({key: row[key] for key in Step.__dataclass_fields__})

    def compare_and_set_attempt(self, *, attempt_id: str, expected_state_version: int, phase: str) -> Attempt:
        row = self._cas(
            table="attempts",
            identity_column="attempt_id",
            identity=attempt_id,
            expected_state_version=expected_state_version,
            changes={"phase": phase},
            aggregate_type="ATTEMPT",
        )
        return Attempt.from_record({key: row[key] for key in Attempt.__dataclass_fields__})


class EventRepository(_SqlStore):
    """先缓存 authoritative event，precommit 校验后才执行 INSERT。"""

    def __init__(self, uow: SqliteUnitOfWork) -> None:
        super().__init__(uow._connection)
        self._uow = uow

    def append_authoritative_state_event(self, record: Mapping[str, object]) -> None:
        self._uow._pending_events.append(dict(record))

    def get_authoritative_state_event(self, event_id: str) -> dict[str, object] | None:
        return self._get("authoritative_state_events", "state_event_id", event_id)


class AuthorizationRepository(_SqlStore):
    """授权合同 append/get primitive；消费策略留给后续任务。"""

    def append_intent_authorization(self, record: Mapping[str, object]) -> None:
        _validate_authorization_record(
            record,
            parser=authorization_domain.parse_intent_authorization,
            expected_record=_intent_authorization_record,
        )
        self._insert("intent_authorizations", record)

    def get_intent_authorization(self, authorization_id: str) -> dict[str, object] | None:
        record = self._get("intent_authorizations", "intent_authorization_id", authorization_id)
        if record is not None:
            _validate_authorization_record(
                record,
                parser=authorization_domain.parse_intent_authorization,
                expected_record=_intent_authorization_record,
            )
        return record

    def append_execution_authorization(self, record: Mapping[str, object]) -> None:
        _validate_authorization_record(
            record,
            parser=authorization_domain.parse_execution_authorization,
            expected_record=_execution_authorization_record,
        )
        self._insert("execution_authorizations", record)

    def get_execution_authorization(self, authorization_id: str) -> dict[str, object] | None:
        record = self._get("execution_authorizations", "execution_authorization_id", authorization_id)
        if record is not None:
            _validate_authorization_record(
                record,
                parser=authorization_domain.parse_execution_authorization,
                expected_record=_execution_authorization_record,
            )
        return record


class ResourceRepository(_SqlStore):
    """Resource lease/target guard 的 insert/get/CAS primitive。"""

    def insert_resource_lease(self, record: Mapping[str, object]) -> None:
        self._insert("resource_leases", record)

    def get_resource_lease(self, resource_key: str) -> dict[str, object] | None:
        return self._get("resource_leases", "resource_key", resource_key)

    def compare_and_set_resource_lease(
        self, resource_key: str, expected_state_version: int, changes: Mapping[str, object]
    ) -> ResourceLease:
        row = self._simple_cas("resource_leases", "resource_key", resource_key, expected_state_version, changes)
        return ResourceLease.from_record(row)

    def insert_target_guard(self, record: Mapping[str, object]) -> None:
        self._insert("target_guards", record)

    def get_target_guard(self, resource_fingerprint: str) -> dict[str, object] | None:
        return self._get("target_guards", "resource_fingerprint", resource_fingerprint)

    def compare_and_set_target_guard(
        self, resource_fingerprint: str, expected_state_version: int, changes: Mapping[str, object]
    ) -> TargetGuard:
        row = self._simple_cas(
            "target_guards", "resource_fingerprint", resource_fingerprint, expected_state_version, changes
        )
        return TargetGuard.from_record(row)

    def _simple_cas(
        self,
        table: str,
        identity_column: str,
        identity: str,
        expected_state_version: int,
        changes: Mapping[str, object],
    ) -> dict[str, object]:
        columns = _validated_update_columns(table, identity_column, changes)
        quoted_table = _quote_identifier(table)
        quoted_identity = _quote_identifier(identity_column)
        setters = ",".join(f"{_quote_identifier(column)}=?" for column in columns)
        # Resource CAS 的变化字段来自公开 Mapping，因此必须和 workflow CAS 一样先过 allowlist。
        cursor = self._connection.execute(
            f"UPDATE {quoted_table} SET {setters}, state_version=state_version+1 "  # noqa: S608
            f"WHERE {quoted_identity}=? AND state_version=?",
            (*(changes[column] for column in columns), identity, expected_state_version),
        )
        if cursor.rowcount != 1:
            raise StaleStateVersionError("stale resource version")
        row = self._get(table, identity_column, identity)
        if row is None:
            raise StaleStateVersionError("resource row disappeared after CAS")
        return row


class SqliteUnitOfWork:
    """显式 BEGIN IMMEDIATE/commit/rollback，并在 precommit 强制 CAS↔event 双射。"""

    def __init__(self, connection: sqlite3.Connection, *, failure_probe: Callable[[str], None]) -> None:
        self._connection = connection
        self._failure_probe = failure_probe
        self._pending_cas: list[_PendingCas] = []
        self._pending_events: list[dict[str, object]] = []
        # 依赖 authoritative state event 外键的追加写入必须排在 state event INSERT
        # 之后、COMMIT 之前；回调仍属于同一个 UoW，任一失败都会由 coordinator 回滚。
        self._pending_post_state_event_writes: list[Callable[[], None]] = []
        self.workflow = WorkflowRepository(self)
        self.events = EventRepository(self)
        self.authorization = AuthorizationRepository(connection)
        self.resources = ResourceRepository(connection)

    @property
    def connection_identity(self) -> int:
        return id(self._connection)

    def begin_immediate(self) -> None:
        """取得保留写锁，事务边界不交给 sqlite implicit mode。"""
        started = time.monotonic()
        self._connection.execute("BEGIN IMMEDIATE")
        LOGGER.info(
            "sqlite_transaction_started",
            operation="sqlite_transaction",
            status="started",
            duration_ms=round((time.monotonic() - started) * 1000, 3),
        )

    def _validate_event(self, event: dict[str, object]) -> None:
        try:
            schema = json.loads(AUTHORITATIVE_STATE_EVENT_V1_SCHEMA_JSON)
            jsonschema.Draft7Validator(schema, format_checker=factory_format_checker()).validate(event)
            if event["payloadDigest"] != payload_digest(event["payload"]):
                raise ValueError("payload digest mismatch")
        except Exception:
            LOGGER.warning(
                "sqlite_state_event_rejected",
                operation="sqlite_precommit",
                status="rejected",
                error_code="INVALID_AUTHORITATIVE_STATE_EVENT",
            )
            raise StateEventValidationError("invalid authoritative state event") from None

    def _register_post_state_event_write(self, writer: Callable[[], None]) -> None:
        """登记必须在 authoritative state event 落库后执行的同事务追加写入。"""
        self._pending_post_state_event_writes.append(writer)

    def precommit(self) -> None:
        """核对数量、schema/digest、身份/lineage/version 后才插入 authoritative lane。"""
        started = time.monotonic()
        if len(self._pending_cas) != len(self._pending_events):
            LOGGER.warning(
                "sqlite_state_event_pairing_rejected",
                operation="sqlite_precommit",
                status="rejected",
                error_code="STATE_EVENT_PAIRING_MISMATCH",
                cas_count=len(self._pending_cas),
                event_count=len(self._pending_events),
            )
            raise StateEventPairingError("CAS and state event cardinality differs")
        used: set[int] = set()
        for cas in self._pending_cas:
            matches = [
                (index, event)
                for index, event in enumerate(self._pending_events)
                if index not in used and event.get("aggregateType") == cas.aggregate_type
            ]
            if len(matches) > 1:
                # 同类 CAS 可在一个事务并存；仅此时以完整持久身份、lineage 与版本消歧，
                # 单候选仍交给下方校验以保留 Identity/Version 的稳定错误分类。
                expected_pairing = (
                    cas.aggregate_id,
                    cas.task_id,
                    cas.run_id,
                    cas.step_id,
                    cas.attempt_id,
                    cas.previous_version,
                    cas.state_version,
                )
                matches = [
                    (index, event)
                    for index, event in matches
                    if (
                        event.get("aggregateId"),
                        event.get("taskId"),
                        event.get("runId"),
                        event.get("stepId"),
                        event.get("attemptId"),
                        event.get("previousStateVersion"),
                        event.get("stateVersion"),
                    )
                    == expected_pairing
                ]
            if len(matches) != 1:
                raise StateEventPairingError("CAS and state event types are not bijective")
            index, event = matches[0]
            used.add(index)
            self._validate_event(event)
            expected_identity = (
                cas.aggregate_id,
                cas.task_id,
                cas.run_id,
                cas.step_id,
                cas.attempt_id,
            )
            actual_identity = (
                event["aggregateId"],
                event["taskId"],
                event["runId"],
                event["stepId"],
                event["attemptId"],
            )
            if actual_identity != expected_identity:
                raise StateEventIdentityError("authoritative event identity differs from stored lineage")
            if (event["previousStateVersion"], event["stateVersion"]) != (
                cas.previous_version,
                cas.state_version,
            ):
                raise StateEventVersionError("authoritative event version differs from CAS")
            self._failure_probe("before_state_event_insert")
            canonical = canonicalize(event)
            self.events._insert(
                "authoritative_state_events",
                {
                    "state_event_id": event["stateEventId"],
                    "schema_version": event["schemaVersion"],
                    "task_id": event["taskId"],
                    "scope": event["scope"],
                    "aggregate_type": event["aggregateType"],
                    "aggregate_id": event["aggregateId"],
                    "run_id": event["runId"],
                    "step_id": event["stepId"],
                    "attempt_id": event["attemptId"],
                    "previous_state_version": event["previousStateVersion"],
                    "state_version": event["stateVersion"],
                    "payload_digest": event["payloadDigest"],
                    "canonical_state_event": canonical,
                },
            )
        for writer in self._pending_post_state_event_writes:
            # 预算时钟事件通过 state_event_id 外键绑定当前 state.changed；故意保留
            # 独立故障点，验证 clock insert 失败时 CAS、state event 和 clock 链整体回滚。
            self._failure_probe("before_budget_clock_event_insert")
            writer()
        # 这里仍未向 SQLite 发送 COMMIT；注入故障必须走可回滚的已知失败分支，
        # 不能被 coordinator 误判成提交结果未知。
        self._failure_probe("before_commit")
        LOGGER.info(
            "sqlite_precommit_completed",
            operation="sqlite_precommit",
            status="success",
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            paired_count=len(self._pending_cas),
        )

    def commit(self) -> None:
        """在故障点之后显式提交；成功后清空事务内待配对状态。"""
        started = time.monotonic()
        self._connection.execute("COMMIT")
        paired_count = len(self._pending_cas)
        self._pending_cas.clear()
        self._pending_events.clear()
        self._pending_post_state_event_writes.clear()
        LOGGER.info(
            "sqlite_transaction_committed",
            operation="sqlite_transaction",
            status="committed",
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            paired_count=paired_count,
        )

    def rollback(self) -> None:
        """显式回滚并丢弃仅属于本事务的配对事实。"""
        started = time.monotonic()
        if self._connection.in_transaction:
            self._connection.execute("ROLLBACK")
        paired_count = len(self._pending_cas)
        self._pending_cas.clear()
        self._pending_events.clear()
        self._pending_post_state_event_writes.clear()
        LOGGER.warning(
            "sqlite_transaction_rolled_back",
            operation="sqlite_transaction",
            status="rolled_back",
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            paired_count=paired_count,
        )


__all__ = [
    "SqliteUnitOfWork",
    "StaleStateVersionError",
    "StorageIdentifierError",
    "StateEventIdentityError",
    "StateEventPairingError",
    "StateEventValidationError",
    "StateEventVersionError",
]
