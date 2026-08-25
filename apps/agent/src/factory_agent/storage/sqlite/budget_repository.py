"""Budget clock 的 SQLite 持久化边界。

预算事件与 Run 投影必须共用调用方传入的 UoW 连接和事务；本模块不打开第二
连接、不自行提交，也不允许修改追加链。所有动态值都走参数绑定，表结构保持在
冻结的 ``0004_control_budget.sql`` 中。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping

from factory_agent.domain.budgets import BudgetClockEvent, BudgetClockTransition
from factory_agent.domain.events import payload_digest
from factory_agent.errors import FactoryError
from factory_agent.observability.logging import get_logger
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork

LOGGER = get_logger(__name__)


def _as_int(value: object, label: str) -> int:
    """从 SQLite row 的 object 边界收窄为非 bool 整数。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BudgetRepositoryError(f"{label} is invalid")
    return value


class BudgetRepositoryError(FactoryError):
    """预算投影、CAS 或追加事件违反稳定存储边界时抛出。"""

    error_code = "BUDGET_REPOSITORY_ERROR"


class BudgetRepository:
    """在既有 UoW owner connection 内原子更新 Run 和 budget clock event。"""

    def __init__(self, unit_of_work: SqliteUnitOfWork) -> None:
        # Task 1 的 WorkflowRepository 同样以 owner connection 承载多仓储；这里
        # 只复用该连接，不创建隐式连接或绕过上层 coordinator 的事务协议。
        self._unit_of_work = unit_of_work
        self._connection = unit_of_work._connection

    def get_run(self, run_id: str) -> dict[str, object] | None:
        """读取 Run 投影；不存在时由服务层转成稳定业务错误。"""
        return self._unit_of_work.workflow.get_run(run_id)

    def get_latest_clock_event(self, run_id: str) -> dict[str, object] | None:
        """读取追加链尾部，恢复时只依赖持久事件而不是进程内缓存。"""
        cursor = self._connection.execute(
            "SELECT * FROM budget_clock_events WHERE run_id=? ORDER BY clock_seq DESC LIMIT 1",
            (run_id,),
        )
        row = cursor.fetchone()
        return None if row is None else dict(zip((column[0] for column in cursor.description), row, strict=True))

    def reconstruct_projection(
        self,
        run_id: str,
        *,
        max_clock_rollback_ms: int = 1_000,
        max_clock_forward_jump_ms: int = 1_000,
    ) -> dict[str, object]:
        """从 genesis 到链尾重算预算投影并核对当前 Run 锚点。

        恢复只信任 append-only 事件：序号、前摘要、事件摘要、state event 外键和
        同 boot 的 wall/monotonic 偏差任一不一致都 fail closed，不用当前 projection
        反推事件链。暂停区间仍校验时钟锚点，但不把时长计入累计预算。
        """
        if max_clock_rollback_ms < 0 or max_clock_forward_jump_ms < 0:
            raise BudgetRepositoryError("budget clock thresholds are invalid")
        run = self.get_run(run_id)
        if run is None:
            raise BudgetRepositoryError("budget run does not exist")
        run_task_id = run.get("task_id")
        if not isinstance(run_task_id, str) or not run_task_id:
            raise BudgetRepositoryError("budget run task lineage is invalid")
        cursor = self._connection.execute(
            "SELECT * FROM budget_clock_events WHERE run_id=? ORDER BY clock_seq ASC",
            (run_id,),
        )
        columns = tuple(item[0] for item in cursor.description)
        records = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        if not records:
            stored_accumulated = _as_int(run["budget_accumulated_ms"], "stored budget accumulated_ms")
            # 空链只能代表尚未建立预算时钟；非零累计、状态、暂停原因或锚点
            # 都必须有 append-only 事件证据，避免恢复继续信任被篡改的 Run 投影。
            if (
                stored_accumulated != 0
                or run.get("budget_clock_state") is not None
                or run.get("budget_suspension_reason") is not None
                or any(
                    run.get(name) is not None
                    for name in ("budget_clock_boot_id", "budget_clock_monotonic_ns", "budget_clock_wall_time")
                )
            ):
                raise BudgetRepositoryError("budget clock genesis is missing")
            return {
                "run_id": run_id,
                "clock_seq": 0,
                "clock_digest": None,
                "clock_state": None,
                "budget_accumulated_ms": stored_accumulated,
            }

        accumulated = 0
        previous_event: BudgetClockEvent | None = None
        previous_running = False
        seen_state_event_ids: set[str] = set()
        last_state_version = 0
        last_clock_state: str | None = None
        for expected_seq, record in enumerate(records, start=1):
            state_event_id = record.get("state_event_id")
            if not isinstance(state_event_id, str) or not state_event_id:
                raise BudgetRepositoryError("budget clock state event identity is invalid")
            # 重建不能只信 clock_event 外键；必须复核 UoW 写入的 Task/Run/Step/Attempt
            # lineage 与 canonical 固定头，事件被外部损坏时保持 fail closed。
            state_row = self._connection.execute(
                "SELECT state_event_id,schema_version,task_id,scope,aggregate_type,aggregate_id,run_id,"
                "step_id,attempt_id,previous_state_version,state_version,payload_digest,canonical_state_event "
                "FROM authoritative_state_events "
                "WHERE state_event_id=?",
                (state_event_id,),
            ).fetchone()
            if (
                state_row is None
                or state_row[0] != state_event_id
                or state_row[2:9]
                != (
                    run_task_id,
                    "RUN",
                    "RUN",
                    run_id,
                    run_id,
                    None,
                    None,
                )
            ):
                raise BudgetRepositoryError("budget clock state event lineage is invalid")
            stored_payload_digest = state_row[11]
            if not isinstance(stored_payload_digest, str) or not stored_payload_digest:
                raise BudgetRepositoryError("budget clock state event payload digest is invalid")
            event = self._validated_clock_event(
                record,
                expected_seq=expected_seq,
                previous_digest=None if previous_event is None else previous_event.clock_digest,
                state_payload_digest=stored_payload_digest,
                # 从同一权威 state event 持久行读取 Task 身份，避免依赖可变 Run 投影重算摘要。
                state_event_task_id=state_row[2],
            )
            if event.state_event_id in seen_state_event_ids or state_row[0] != event.state_event_id:
                raise BudgetRepositoryError("budget clock state event identity is duplicated")
            seen_state_event_ids.add(event.state_event_id)
            schema_version = _as_int(state_row[1], "stored state event schema_version")
            previous_state_version = _as_int(state_row[9], "stored state event previous_state_version")
            state_version = _as_int(state_row[10], "stored state event state_version")
            if previous_state_version < 0 or state_version != previous_state_version + 1:
                raise BudgetRepositoryError("budget clock state event version is invalid")
            if state_version <= last_state_version:
                raise BudgetRepositoryError("budget clock state event versions are not increasing")
            canonical_blob = state_row[12]
            if not isinstance(canonical_blob, bytes):
                raise BudgetRepositoryError("budget clock canonical state event is invalid")
            try:
                state_event = json.loads(canonical_blob.decode("utf-8", errors="strict"))
                if not isinstance(state_event, dict) or canonicalize(state_event) != canonical_blob:
                    raise ValueError("state event canonical mismatch")
            except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise BudgetRepositoryError("budget clock canonical state event is invalid") from exc
            if (
                state_event.get("stateEventId") != event.state_event_id
                or schema_version != 1
                or state_event.get("schemaVersion") != schema_version
                or state_event.get("eventType") != "state.changed"
                or state_event.get("durabilityClass") != "authoritative_state"
                or state_event.get("taskId") != run_task_id
                or state_event.get("runId") != run_id
                or state_event.get("aggregateType") != "RUN"
                or state_event.get("aggregateId") != run_id
                or state_event.get("scope") != "RUN"
                or "stepId" not in state_event
                or state_event["stepId"] is not None
                or "attemptId" not in state_event
                or state_event["attemptId"] is not None
                or state_event.get("previousStateVersion") != previous_state_version
                or state_event.get("stateVersion") != state_version
            ):
                raise BudgetRepositoryError("budget clock state event canonical identity is invalid")
            state_payload = state_event.get("payload")
            if not isinstance(state_payload, dict):
                raise BudgetRepositoryError("budget clock state event payload is invalid")
            if (
                not isinstance(stored_payload_digest, str)
                or state_event.get("payloadDigest") != stored_payload_digest
                or payload_digest(state_payload) != stored_payload_digest
            ):
                raise BudgetRepositoryError("budget clock state event payload digest is invalid")
            if previous_event is not None:
                elapsed_ms = self._elapsed_between_events(
                    previous_event,
                    event,
                    max_clock_rollback_ms=max_clock_rollback_ms,
                    max_clock_forward_jump_ms=max_clock_forward_jump_ms,
                )
                if previous_running:
                    accumulated += elapsed_ms
            expected_elapsed_ms = elapsed_ms if previous_running else 0
            payload_accumulated = state_payload.get("budgetAccumulatedMs")
            payload_elapsed = state_payload.get("elapsedMs")
            payload_clock_state = state_payload.get("clockState")
            payload_suspension_reason = state_payload.get("suspensionReason")
            payload_budget_exhausted = state_payload.get("budgetExhausted")
            if (
                type(payload_accumulated) is not int
                or payload_accumulated < 0
                or type(payload_elapsed) is not int
                or payload_elapsed < 0
                or type(payload_budget_exhausted) is not bool
                or payload_clock_state
                not in ({"RUNNING"} if event.transition is BudgetClockTransition.RUNNING_STARTED else {"SUSPENDED"})
                or (
                    event.transition is BudgetClockTransition.RUNNING_STARTED
                    and (event.suspension_reason is not None or payload_suspension_reason is not None)
                )
                or (
                    event.transition is BudgetClockTransition.SUSPENSION_STARTED
                    and (
                        event.suspension_reason is None
                        or payload_suspension_reason != event.suspension_reason.value
                        or payload_suspension_reason not in {"USER_PAUSED", "REQUIRES_USER_ACTION"}
                    )
                )
                or (payload_budget_exhausted and payload_suspension_reason != "REQUIRES_USER_ACTION")
            ):
                raise BudgetRepositoryError("budget clock state event payload does not match clock transition")
            if (
                state_payload.get("kind") != "budget.clock.observed"
                or state_payload.get("runId") != run_id
                or payload_accumulated != accumulated
                or payload_elapsed != expected_elapsed_ms
                or state_payload.get("clockAnomaly") is not False
            ):
                raise BudgetRepositoryError("budget clock state event payload does not match observation")
            previous_event = event
            previous_running = event.transition is BudgetClockTransition.RUNNING_STARTED
            last_clock_state = str(state_payload["clockState"])
            last_state_version = state_version

        if previous_event is None:
            raise BudgetRepositoryError("budget clock chain is empty")
        if last_clock_state is None:
            raise BudgetRepositoryError("budget clock state is missing")
        last_state = last_clock_state
        stored_accumulated = _as_int(run["budget_accumulated_ms"], "stored budget accumulated_ms")
        if stored_accumulated != accumulated:
            raise BudgetRepositoryError("budget projection does not match clock chain")
        expected_anchor = {
            "budget_clock_boot_id": previous_event.boot_id,
            "budget_clock_monotonic_ns": previous_event.monotonic_ns,
            "budget_clock_wall_time": previous_event.wall_time,
        }
        if any(run.get(name) != value for name, value in expected_anchor.items()):
            raise BudgetRepositoryError("budget clock anchor does not match clock chain")
        run_state_version = _as_int(run["state_version"], "stored budget state_version")
        # clock-linked state event 不能落后于当前 Run 投影，防止 CAS 版本回拨后
        # 重建继续信任旧 projection；链尾之后的合法状态事件仍由 anomaly tail 校验。
        if run_state_version < last_state_version:
            raise BudgetRepositoryError("budget projection state version is behind clock chain")
        stored_state = run.get("budget_clock_state")
        # CLOCK_ANOMALY 不追加新的 clock event，而是保留最后可信 anchor；其它
        # BLOCKED 投影必须与事件链尾态一致，避免把业务阻断态伪装成时钟态恢复。
        anomaly_anchor_exception = False
        if stored_state == "BLOCKED" and run.get("block_reason_code") == "CLOCK_ANOMALY":
            anomaly_anchor_exception = self._has_clock_anomaly_state_event(
                run_id,
                task_id=run_task_id,
                after_state_version=last_state_version,
                through_state_version=run_state_version,
            )
        if stored_state != last_state and not anomaly_anchor_exception:
            raise BudgetRepositoryError("budget clock state does not match clock chain")
        return {
            "run_id": run_id,
            "clock_seq": previous_event.clock_seq,
            "clock_digest": previous_event.clock_digest,
            "clock_state": last_state,
            "budget_accumulated_ms": accumulated,
            "boot_id": previous_event.boot_id,
            "monotonic_ns": previous_event.monotonic_ns,
            "wall_time": previous_event.wall_time,
        }

    def _has_clock_anomaly_state_event(
        self,
        run_id: str,
        *,
        task_id: str,
        after_state_version: int,
        through_state_version: int,
    ) -> bool:
        """核对 CLOCK_ANOMALY 的权威 state event，避免可变投影伪造例外。"""
        if through_state_version <= after_state_version:
            return False
        # 异常例外同样绑定当前 Run 的完整 lineage，不能以可变 block reason 绕过校验。
        cursor = self._connection.execute(
            "SELECT state_event_id,schema_version,task_id,scope,aggregate_type,aggregate_id,run_id,"
            "step_id,attempt_id,previous_state_version,state_version,payload_digest,canonical_state_event "
            "FROM authoritative_state_events "
            "WHERE aggregate_type='RUN' AND aggregate_id=? AND run_id=? "
            "AND state_version>? AND state_version<=? ORDER BY state_version ASC",
            (run_id, run_id, after_state_version, through_state_version),
        )
        expected_state_version = after_state_version + 1
        anomaly_found = False
        for row in cursor.fetchall():
            state_event_id = row[0]
            schema_version = _as_int(row[1], "stored anomaly state event schema_version")
            previous_state_version = _as_int(row[9], "stored anomaly previous_state_version")
            state_version = _as_int(row[10], "stored anomaly state_version")
            if (
                not isinstance(state_event_id, str)
                or not state_event_id
                or schema_version != 1
                or row[2:9] != (task_id, "RUN", "RUN", run_id, run_id, None, None)
                or previous_state_version < 0
                or previous_state_version != expected_state_version - 1
                or state_version != previous_state_version + 1
            ):
                raise BudgetRepositoryError("budget anomaly state event lineage is invalid")
            expected_state_version = state_version + 1
            canonical_blob = row[12]
            if not isinstance(canonical_blob, bytes):
                raise BudgetRepositoryError("budget anomaly canonical state event is invalid")
            try:
                state_event = json.loads(canonical_blob.decode("utf-8", errors="strict"))
                if not isinstance(state_event, dict) or canonicalize(state_event) != canonical_blob:
                    raise ValueError("anomaly state event canonical mismatch")
            except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise BudgetRepositoryError("budget anomaly canonical state event is invalid") from exc
            payload = state_event.get("payload")
            stored_payload_digest = row[11]
            if (
                state_event.get("stateEventId") != state_event_id
                or state_event.get("schemaVersion") != schema_version
                or state_event.get("eventType") != "state.changed"
                or state_event.get("durabilityClass") != "authoritative_state"
                or state_event.get("taskId") != task_id
                or state_event.get("runId") != run_id
                or state_event.get("aggregateType") != "RUN"
                or state_event.get("aggregateId") != run_id
                or state_event.get("scope") != "RUN"
                or "stepId" not in state_event
                or state_event["stepId"] is not None
                or "attemptId" not in state_event
                or state_event["attemptId"] is not None
                or state_event.get("previousStateVersion") != previous_state_version
                or state_event.get("stateVersion") != state_version
                or not isinstance(payload, dict)
                or not isinstance(stored_payload_digest, str)
                or state_event.get("payloadDigest") != stored_payload_digest
                or payload_digest(payload) != stored_payload_digest
            ):
                raise BudgetRepositoryError("budget anomaly state event payload is invalid")
            if (
                payload.get("kind") == "budget.clock.observed"
                and payload.get("runId") == run_id
                and payload.get("clockAnomaly") is True
                and payload.get("clockState") == "BLOCKED"
                and payload.get("suspensionReason") is None
                and self._connection.execute(
                    "SELECT 1 FROM budget_clock_events WHERE state_event_id=? LIMIT 1",
                    (state_event_id,),
                ).fetchone()
                is None
            ):
                anomaly_found = True
        if expected_state_version != through_state_version + 1:
            raise BudgetRepositoryError("budget anomaly state event lineage is invalid")
        return anomaly_found

    def rebuild_projection(
        self,
        run_id: str,
        *,
        max_clock_rollback_ms: int = 1_000,
        max_clock_forward_jump_ms: int = 1_000,
    ) -> dict[str, object]:
        """兼容恢复调用方的别名；实现仍由 reconstruct_projection 统一。"""
        return self.reconstruct_projection(
            run_id,
            max_clock_rollback_ms=max_clock_rollback_ms,
            max_clock_forward_jump_ms=max_clock_forward_jump_ms,
        )

    def persist_observation(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        changes: Mapping[str, object],
        state_event_id: str,
        state_payload: Mapping[str, object],
        clock_event: Mapping[str, object] | None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, object]:
        """以一个事务写入 Run、权威 state.changed 与可选 budget event。

        ``clock_event`` 为 None 只表示首次建立时钟基线或检测到异常；Run 投影
        仍会写入权威状态事件。任何一条 INSERT 失败都会由 coordinator 回滚，
        不留下半条 budget 链或半个 Run 投影。
        """
        allowed = {
            "observed_state",
            "requires_user_action",
            "block_reason_code",
            "budget_accumulated_ms",
            "budget_clock_state",
            "budget_clock_boot_id",
            "budget_clock_monotonic_ns",
            "budget_clock_wall_time",
            "budget_suspension_reason",
        }
        if not changes or set(changes) - allowed:
            raise BudgetRepositoryError("budget projection columns are invalid")
        if expected_state_version < 0 or not run_id or not state_event_id:
            raise BudgetRepositoryError("budget projection identity is invalid")
        if clock_event is not None and (
            clock_event.get("run_id") != run_id or clock_event.get("state_event_id") != state_event_id
        ):
            raise BudgetRepositoryError("budget clock event lineage is invalid")

        # 预算投影必须复用 WorkflowRepository 的公开 CAS 登记路径；该路径负责
        # after_state_cas 故障点、lineage 和待配对 CAS，precommit 才允许落 state event。
        updated_row = self._unit_of_work.workflow._cas(
            table="runs",
            identity_column="run_id",
            identity=run_id,
            expected_state_version=expected_state_version,
            changes=changes,
            aggregate_type="RUN",
        )
        new_state_version = _as_int(updated_row["state_version"], "updated budget state_version")
        payload = dict(state_payload)
        event = {
            "schemaVersion": 1,
            "stateEventId": state_event_id,
            "eventType": "state.changed",
            "durabilityClass": "authoritative_state",
            "taskId": str(updated_row["task_id"]),
            "scope": "RUN",
            "aggregateType": "RUN",
            "aggregateId": run_id,
            "runId": run_id,
            "stepId": None,
            "attemptId": None,
            "previousStateVersion": expected_state_version,
            "stateVersion": new_state_version,
            "payload": payload,
            "payloadDigest": payload_digest(payload),
        }
        # UoW precommit 统一校验 schema/身份/version 并 INSERT state event；预算事件
        # 通过 post-state-event writer 排在该 INSERT 之后，满足外键与同事务回滚。
        self._unit_of_work.events.append_authoritative_state_event(event)

        if clock_event is not None:
            frozen_clock_event = dict(clock_event)

            def append_clock_event(event: Mapping[str, object] = frozen_clock_event) -> None:
                """在 state event 已落库后追加 budget clock event。"""
                self._append_clock_event(event, request_id=request_id, trace_id=trace_id)

            self._unit_of_work._register_post_state_event_write(append_clock_event)
        updated = self.get_run(run_id)
        if updated is None:
            raise BudgetRepositoryError("budget run disappeared after projection")
        LOGGER.info(
            "budget_projection_staged",
            operation="budget.persist_observation",
            status="pending",
            run_id=run_id,
            task_id=str(updated_row["task_id"]),
            request_id=request_id,
            trace_id=trace_id,
            state_version=new_state_version,
            clock_event_appended=clock_event is not None,
        )
        return updated

    def _append_clock_event(
        self,
        raw_event: Mapping[str, object],
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        """验证摘要链并执行 append-only INSERT；UPDATE/DELETE 由 SQLite trigger 拒绝。"""
        latest = self.get_latest_clock_event(str(raw_event.get("run_id", "")))
        state_event_id = raw_event.get("state_event_id")
        if not isinstance(state_event_id, str) or not state_event_id:
            raise BudgetRepositoryError("budget clock state event identity is invalid")
        state_row = self._connection.execute(
            "SELECT task_id,payload_digest FROM authoritative_state_events WHERE state_event_id=?",
            (state_event_id,),
        ).fetchone()
        if (
            state_row is None
            or not isinstance(state_row[0], str)
            or not state_row[0]
            or not isinstance(state_row[1], str)
            or not state_row[1]
        ):
            raise BudgetRepositoryError("budget clock state event payload digest is invalid")
        event = self._validated_clock_event(
            raw_event,
            expected_seq=1 if latest is None else _as_int(latest["clock_seq"], "stored budget clock_seq") + 1,
            previous_digest=None if latest is None else str(latest["clock_digest"]),
            state_payload_digest=state_row[1],
            # 追加时只信任已落库的权威 state event task_id，与同事务 state event 配对。
            state_event_task_id=state_row[0],
        )
        try:
            self._connection.execute(
                "INSERT INTO budget_clock_events("
                "run_id,clock_seq,transition,suspension_reason,boot_id,monotonic_ns,wall_time,state_event_id,"
                "previous_clock_digest,clock_digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    event.run_id,
                    event.clock_seq,
                    event.transition.value,
                    None if event.suspension_reason is None else event.suspension_reason.value,
                    event.boot_id,
                    event.monotonic_ns,
                    event.wall_time,
                    event.state_event_id,
                    event.previous_clock_digest,
                    event.clock_digest,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise BudgetRepositoryError("budget clock append failed") from exc
        task_row = self._connection.execute("SELECT task_id FROM runs WHERE run_id=?", (event.run_id,)).fetchone()
        task_id = task_row[0] if task_row is not None and isinstance(task_row[0], str) else None
        LOGGER.info(
            "budget_clock_event_staged",
            operation="budget.append_clock_event",
            status="pending",
            run_id=event.run_id,
            task_id=task_id,
            request_id=request_id,
            trace_id=trace_id,
            clock_seq=event.clock_seq,
        )

    def _validated_clock_event(
        self,
        raw_event: Mapping[str, object],
        *,
        expected_seq: int,
        previous_digest: str | None,
        state_payload_digest: str,
        state_event_task_id: str,
    ) -> BudgetClockEvent:
        """校验事件 exact-set、序号、前摘要和当前摘要，供写入与恢复共用。"""
        required = {
            "run_id",
            "clock_seq",
            "transition",
            "suspension_reason",
            "boot_id",
            "monotonic_ns",
            "wall_time",
            "state_event_id",
            "previous_clock_digest",
            "clock_digest",
        }
        if set(raw_event) != required:
            raise BudgetRepositoryError("budget clock event fields are invalid")
        if not isinstance(state_payload_digest, str) or not state_payload_digest:
            raise BudgetRepositoryError("budget clock state event payload digest is invalid")
        if not isinstance(state_event_task_id, str) or not state_event_task_id:
            raise BudgetRepositoryError("budget clock state event task lineage is invalid")
        event = BudgetClockEvent.from_record(raw_event)
        if (
            isinstance(event.clock_seq, bool)
            or not isinstance(event.clock_seq, int)
            or event.clock_seq <= 0
            or isinstance(event.monotonic_ns, bool)
            or not isinstance(event.monotonic_ns, int)
            or event.monotonic_ns < 0
            or not event.run_id
            or not event.boot_id
            or not event.wall_time
            or not event.state_event_id
        ):
            raise BudgetRepositoryError("budget clock event values are invalid")
        if event.clock_seq != expected_seq or event.previous_clock_digest != previous_digest:
            raise BudgetRepositoryError("budget clock event sequence is not append-only")
        digest_input = {
            "run_id": event.run_id,
            "clock_seq": event.clock_seq,
            "transition": event.transition.value,
            "suspension_reason": None if event.suspension_reason is None else event.suspension_reason.value,
            "boot_id": event.boot_id,
            "monotonic_ns": event.monotonic_ns,
            "wall_time": event.wall_time,
            "state_event_id": event.state_event_id,
            "previous_clock_digest": event.previous_clock_digest,
            "state_event_payload_digest": state_payload_digest,
            # Task 身份是摘要前像的一部分；保持 state event 与 clock 链不可静默换绑。
            "state_event_task_id": state_event_task_id,
        }
        expected_digest = "sha256:" + hashlib.sha256(canonicalize(digest_input)).hexdigest()
        if event.clock_digest != expected_digest:
            raise BudgetRepositoryError("budget clock digest is invalid")
        return event

    def _elapsed_between_events(
        self,
        previous: BudgetClockEvent,
        current: BudgetClockEvent,
        *,
        max_clock_rollback_ms: int,
        max_clock_forward_jump_ms: int,
    ) -> int:
        """按事件锚点重建一个区间；暂停区间由调用方决定是否计入。"""
        from datetime import UTC, datetime

        try:
            previous_wall = datetime.fromisoformat(previous.wall_time.replace("Z", "+00:00")).astimezone(UTC)
            current_wall = datetime.fromisoformat(current.wall_time.replace("Z", "+00:00")).astimezone(UTC)
        except (TypeError, ValueError) as exc:
            raise BudgetRepositoryError("budget clock wall time is invalid") from exc
        wall_delta_ms = int((current_wall - previous_wall).total_seconds() * 1000)
        if wall_delta_ms < -max_clock_rollback_ms:
            raise BudgetRepositoryError("budget clock rollback exceeds threshold")
        if previous.boot_id == current.boot_id:
            monotonic_delta_ms = (current.monotonic_ns - previous.monotonic_ns) // 1_000_000
            if current.monotonic_ns < previous.monotonic_ns:
                raise BudgetRepositoryError("budget clock monotonic value regressed")
            if abs(wall_delta_ms - monotonic_delta_ms) > max_clock_forward_jump_ms:
                raise BudgetRepositoryError("budget clock wall monotonic skew exceeds threshold")
            return monotonic_delta_ms
        return max(0, wall_delta_ms)


__all__ = ["BudgetRepository", "BudgetRepositoryError"]
