"""Phase 1 Task 3 预算时钟服务。

服务在 coordinator 提供的单 writer UoW 中读取 Clock sample、重建可计费时间、
更新 Run 投影并追加事件。单调时钟只在同 boot 时使用；跨 boot 或合法关机改用
非负 wall anchor，回拨/非法样本 fail closed。USER_PAUSED 与
REQUIRES_USER_ACTION 是唯一暂停计时原因，重启和重规划不会清零累计预算。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from factory_agent.domain.budgets import BudgetClockTransition
from factory_agent.domain.events import payload_digest
from factory_agent.observability.logging import get_logger
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.storage.sqlite.budget_repository import BudgetRepository
from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork

LOGGER = get_logger(__name__)


class BudgetServiceError(RuntimeError):
    """Clock sample、预算参数或 Run 投影不满足 Task 3 边界时抛出。"""

    error_code = "BUDGET_SERVICE_ERROR"


class BudgetCoordinator(Protocol):
    """最小 coordinator 协议；真实实现负责事务开始、precommit、提交和回滚。"""

    async def execute(
        self,
        *,
        operation: str,
        context: dict[str, object],
        command: Callable[[SqliteUnitOfWork], object],
    ) -> object: ...


class Clock(Protocol):
    """返回 boot、monotonic_ns 和 RFC3339 wall_time 的可重放 Clock port。"""

    def sample(self) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class BudgetObservation:
    """预算观察结果；receipt 资格固定为 PARTIAL，不伪造 FINAL。"""

    run_id: str
    task_id: str
    request_id: str
    trace_id: str
    budget_accumulated_ms: int
    observed_state: str
    requires_user_action: bool
    suspension_reason: str | None
    clock_state: str
    clock_anomaly: bool
    budget_exhausted: bool
    subcheck_id: str
    qualification: str
    final_receipt: bool


def _parse_wall(value: object) -> datetime:
    """严格解析带时区 RFC3339，统一到 UTC 后再做时间差。"""
    if not isinstance(value, str) or not value:
        raise BudgetServiceError("budget clock wall time is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BudgetServiceError("budget clock wall time is invalid") from exc
    if parsed.tzinfo is None:
        raise BudgetServiceError("budget clock wall time must include timezone")
    return parsed.astimezone(UTC)


def _normalize_sample(raw: Mapping[str, object]) -> dict[str, object]:
    """将 Clock port 收窄为稳定 primitive，避免第三方异常正文进入日志。"""
    if not isinstance(raw, Mapping):
        raise BudgetServiceError("budget clock sample is invalid")
    boot_id = raw.get("boot_id")
    monotonic_ns = raw.get("monotonic_ns")
    wall_time = raw.get("wall_time")
    if (
        not isinstance(boot_id, str)
        or not boot_id
        or isinstance(monotonic_ns, bool)
        or not isinstance(monotonic_ns, int)
        or monotonic_ns < 0
    ):
        raise BudgetServiceError("budget clock sample is invalid")
    parsed_wall = _parse_wall(wall_time)
    normalized_wall = parsed_wall.isoformat().replace("+00:00", "Z")
    return {"boot_id": boot_id, "monotonic_ns": monotonic_ns, "wall_time": normalized_wall}


def _as_int(value: object, label: str) -> int:
    """从 SQLite/Clock 的 object 边界收窄为非 bool 整数。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BudgetServiceError(f"{label} is invalid")
    return value


def _subcheck_id(run_id: str) -> str:
    """BUDGET-001 每个 run 固定一个可重放的 PARTIAL subcheckId。"""
    digest = hashlib.sha256(canonicalize({"testId": "BUDGET-001", "runId": run_id})).hexdigest()
    return f"BUDGET-001:budget-clock:{digest[:16]}"


class BudgetService:
    """协调 Clock、预算算法和 SQLite 原子投影。"""

    def __init__(
        self,
        *,
        coordinator: BudgetCoordinator,
        clock: Clock,
        state_event_id_factory: Callable[[], str] | None = None,
        max_clock_rollback_ms: int = 1_000,
        max_clock_forward_jump_ms: int = 1_000,
    ) -> None:
        if (
            isinstance(max_clock_rollback_ms, bool)
            or not isinstance(max_clock_rollback_ms, int)
            or isinstance(max_clock_forward_jump_ms, bool)
            or not isinstance(max_clock_forward_jump_ms, int)
            or max_clock_rollback_ms < 0
            or max_clock_forward_jump_ms < 0
        ):
            raise BudgetServiceError("clock thresholds must be non-negative")
        self._coordinator = coordinator
        self._clock = clock
        self._state_event_id_factory = state_event_id_factory or (lambda: f"state-budget-{uuid.uuid4().hex}")
        self._max_clock_rollback_ms = max_clock_rollback_ms
        self._max_clock_forward_jump_ms = max_clock_forward_jump_ms

    async def observe(
        self,
        *,
        run_id: str,
        budget_limit_ms: int,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> BudgetObservation:
        """采样一次预算时钟并在同一事务内持久化可重建投影。"""
        if (
            not isinstance(run_id, str)
            or not run_id
            or isinstance(budget_limit_ms, bool)
            or not isinstance(budget_limit_ms, int)
            or budget_limit_ms <= 0
        ):
            LOGGER.warning(
                "budget_observation_rejected",
                operation="budget.observe",
                status="rejected",
                run_id=run_id if isinstance(run_id, str) else None,
                error_code=BudgetServiceError.error_code,
            )
            raise BudgetServiceError("budget observation input is invalid")
        request_id = _normalize_correlation_id(request_id, "budget-request")
        trace_id = _normalize_correlation_id(trace_id, "budget-trace")
        try:
            sample = _normalize_sample(self._clock.sample())
        except BudgetServiceError:
            LOGGER.warning(
                "budget_clock_sample_rejected",
                operation="budget.observe",
                status="rejected",
                run_id=run_id,
                request_id=request_id,
                trace_id=trace_id,
                error_code=BudgetServiceError.error_code,
            )
            raise
        started = datetime.now(UTC)

        def command(unit_of_work: SqliteUnitOfWork) -> BudgetObservation:
            return self._observe_in_transaction(
                unit_of_work,
                run_id=run_id,
                budget_limit_ms=budget_limit_ms,
                sample=sample,
                request_id=request_id,
                trace_id=trace_id,
            )

        try:
            result = await self._coordinator.execute(
                operation="budget.observe",
                context={
                    "run_id": run_id,
                    "request_id": request_id,
                    "trace_id": trace_id,
                    "subcheck_id": _subcheck_id(run_id),
                },
                command=command,
            )
        except Exception as exc:
            LOGGER.error(
                "budget_observation_failed",
                operation="budget.observe",
                status="error",
                run_id=run_id,
                request_id=request_id,
                trace_id=trace_id,
                error_type=type(exc).__name__,
            )
            raise
        if not isinstance(result, BudgetObservation):
            raise BudgetServiceError("budget coordinator returned invalid result")
        LOGGER.info(
            "budget_observation_finished",
            operation="budget.observe",
            status="success",
            run_id=run_id,
            duration_ms=round((datetime.now(UTC) - started).total_seconds() * 1000, 3),
            budget_accumulated_ms=result.budget_accumulated_ms,
            clock_anomaly=result.clock_anomaly,
            budget_exhausted=result.budget_exhausted,
            qualification=result.qualification,
            task_id=result.task_id,
            request_id=result.request_id,
            trace_id=result.trace_id,
        )
        return result

    def _observe_in_transaction(
        self,
        unit_of_work: SqliteUnitOfWork,
        *,
        run_id: str,
        budget_limit_ms: int,
        sample: Mapping[str, object],
        request_id: str,
        trace_id: str,
    ) -> BudgetObservation:
        repository = BudgetRepository(unit_of_work)
        run = repository.get_run(run_id)
        if run is None:
            raise BudgetServiceError("budget run does not exist")
        run_task_id = run.get("task_id")
        if not isinstance(run_task_id, str) or not run_task_id:
            raise BudgetServiceError("budget run task lineage is invalid")
        current_boot = str(sample["boot_id"])
        current_monotonic = _as_int(sample["monotonic_ns"], "budget clock monotonic_ns")
        current_wall = str(sample["wall_time"])
        previous_boot = run.get("budget_clock_boot_id")
        previous_monotonic = run.get("budget_clock_monotonic_ns")
        previous_wall = run.get("budget_clock_wall_time")
        previous_state = run.get("budget_clock_state")
        current_suspension = self._suspension_reason(run)
        block_reason = run.get("block_reason_code")
        first_sample = previous_boot is None or previous_monotonic is None or previous_wall is None
        prior_clock_anomaly = run.get("block_reason_code") == "CLOCK_ANOMALY"
        elapsed_ms = 0
        clock_anomaly = prior_clock_anomaly
        if not first_sample and not prior_clock_anomaly:
            measured_elapsed_ms, clock_anomaly = self._elapsed_ms(
                previous_boot=str(previous_boot),
                previous_monotonic=_as_int(previous_monotonic, "stored budget monotonic_ns"),
                previous_wall=str(previous_wall),
                current_boot=current_boot,
                current_monotonic=current_monotonic,
                current_wall=current_wall,
            )
            # 暂停/人工介入冻结预算；只有进入本次观察前仍为 RUNNING 的区间计费。
            elapsed_ms = measured_elapsed_ms if previous_state == "RUNNING" else 0

        accumulated = _as_int(run["budget_accumulated_ms"], "stored budget accumulated_ms") + max(0, elapsed_ms)
        observed_state = str(run["observed_state"])
        requires_user_action = bool(run["requires_user_action"])
        if clock_anomaly:
            observed_state = "BLOCKED"
            requires_user_action = True
            block_reason = "CLOCK_ANOMALY"
            clock_state = "BLOCKED"
            suspension_reason = None
        else:
            budget_exhausted = accumulated >= budget_limit_ms
            if budget_exhausted:
                observed_state = "BLOCKED"
                requires_user_action = True
                block_reason = "BUDGET_EXHAUSTED"
                # 预算耗尽本身就是持久人工介入阻断；从本次耗尽观察点起冻结
                # 时钟，并继续用 REQUIRES_USER_ACTION anchor 记录后续观察。
                clock_state = "SUSPENDED"
                suspension_reason = "REQUIRES_USER_ACTION"
            elif current_suspension is not None:
                clock_state = "SUSPENDED"
                suspension_reason = current_suspension
                # 只有两个明确的暂停原因可以冻结预算；其它 desiredState 不进入此分支。
                if current_suspension == "REQUIRES_USER_ACTION":
                    requires_user_action = True
            else:
                clock_state = "RUNNING"
                suspension_reason = None

        state_event_id = self._state_event_id_factory()
        if not isinstance(state_event_id, str) or not state_event_id:
            raise BudgetServiceError("budget state event id is invalid")
        changes: dict[str, object] = {
            "observed_state": observed_state,
            "requires_user_action": int(requires_user_action),
            "block_reason_code": block_reason,
            "budget_accumulated_ms": accumulated,
            "budget_clock_state": clock_state,
            # 时钟异常只记录权威阻断事实，保留最后可信 anchor 让事件链仍可重建。
            "budget_clock_boot_id": previous_boot if clock_anomaly and not first_sample else current_boot,
            "budget_clock_monotonic_ns": (
                previous_monotonic if clock_anomaly and not first_sample else current_monotonic
            ),
            "budget_clock_wall_time": previous_wall if clock_anomaly and not first_sample else current_wall,
            "budget_suspension_reason": suspension_reason,
        }
        state_payload = {
            "kind": "budget.clock.observed",
            "runId": run_id,
            "budgetAccumulatedMs": accumulated,
            "clockState": clock_state,
            "suspensionReason": suspension_reason,
            "elapsedMs": max(0, elapsed_ms),
            "clockAnomaly": clock_anomaly,
            "budgetExhausted": observed_state == "BLOCKED" and block_reason == "BUDGET_EXHAUSTED",
        }
        # 时钟摘要绑定完整 state payload，恢复时即使同步改写 payloadDigest 也不能
        # 改变预算耗尽等业务事实而绕过 append-only clock 链校验。
        clock_event = self._build_clock_event(
            repository,
            run_id=run_id,
            state_event_id=state_event_id,
            sample=sample,
            first_sample=first_sample,
            previous_state=previous_state,
            clock_state=clock_state,
            suspension_reason=suspension_reason,
            elapsed_ms=elapsed_ms,
            clock_anomaly=clock_anomaly,
            state_payload_digest=payload_digest(state_payload),
            # 摘要必须绑定观察开始时读取的 Run.task_id；不能使用 CAS 后可能被外部同步迁移的投影。
            state_event_task_id=run_task_id,
        )
        updated = repository.persist_observation(
            run_id=run_id,
            expected_state_version=_as_int(run["state_version"], "stored budget state_version"),
            changes=changes,
            state_event_id=state_event_id,
            state_payload=state_payload,
            clock_event=clock_event,
            request_id=request_id,
            trace_id=trace_id,
        )
        budget_exhausted = block_reason == "BUDGET_EXHAUSTED"
        return BudgetObservation(
            run_id=run_id,
            task_id=str(updated["task_id"]),
            request_id=request_id,
            trace_id=trace_id,
            budget_accumulated_ms=_as_int(updated["budget_accumulated_ms"], "updated budget accumulated_ms"),
            observed_state=str(updated["observed_state"]),
            requires_user_action=bool(updated["requires_user_action"]),
            suspension_reason=(
                None if updated["budget_suspension_reason"] is None else str(updated["budget_suspension_reason"])
            ),
            clock_state=str(updated["budget_clock_state"]),
            clock_anomaly=clock_anomaly,
            budget_exhausted=budget_exhausted,
            subcheck_id=_subcheck_id(run_id),
            qualification="PARTIAL",
            final_receipt=False,
        )

    def _suspension_reason(self, run: Mapping[str, object]) -> str | None:
        """把两个且仅两个可暂停来源映射为预算时钟 reason。"""
        if bool(run.get("requires_user_action")):
            return "REQUIRES_USER_ACTION"
        if run.get("desired_state") == "PAUSED":
            return "USER_PAUSED"
        return None

    def _elapsed_ms(
        self,
        *,
        previous_boot: str,
        previous_monotonic: int,
        previous_wall: str,
        current_boot: str,
        current_monotonic: int,
        current_wall: str,
    ) -> tuple[int, bool]:
        """计算可信锚点增量；回拨、单调回退或 wall/monotonic 偏差均 fail closed。"""
        previous_wall_dt = _parse_wall(previous_wall)
        current_wall_dt = _parse_wall(current_wall)
        wall_delta_ms = int((current_wall_dt - previous_wall_dt).total_seconds() * 1000)
        if wall_delta_ms < -self._max_clock_rollback_ms:
            return 0, True
        if previous_boot == current_boot:
            monotonic_delta = current_monotonic - previous_monotonic
            if monotonic_delta < 0:
                return 0, True
            monotonic_delta_ms = monotonic_delta // 1_000_000
            if abs(wall_delta_ms - monotonic_delta_ms) > self._max_clock_forward_jump_ms:
                return 0, True
            return monotonic_delta_ms, False
        # 重启/合法关机没有连续 monotonic 证据，只使用非负 wall anchor。
        return max(0, wall_delta_ms), False

    def _build_clock_event(
        self,
        repository: BudgetRepository,
        *,
        run_id: str,
        state_event_id: str,
        sample: Mapping[str, object],
        first_sample: bool,
        previous_state: object,
        clock_state: str,
        suspension_reason: str | None,
        elapsed_ms: int,
        clock_anomaly: bool,
        state_payload_digest: str,
        state_event_task_id: str,
    ) -> dict[str, object] | None:
        """构造可重建时钟链；首个可信观察也必须写入 genesis transition。"""
        if clock_anomaly:
            return None
        if not isinstance(state_event_task_id, str) or not state_event_task_id:
            raise BudgetServiceError("budget clock state event task lineage is invalid")
        transition: BudgetClockTransition | None = None
        if clock_state == "SUSPENDED":
            # 暂停期间每次新 anchor 都要追加事件；否则连续暂停观察会把 Run
            # projection 推到事件链之外，恢复时无法重建暂停区间的边界。
            transition = BudgetClockTransition.SUSPENSION_STARTED
        elif clock_state == "RUNNING":
            transition = BudgetClockTransition.RUNNING_STARTED
        if transition is None:
            return None
        latest = repository.get_latest_clock_event(run_id)
        clock_seq = 1 if latest is None else _as_int(latest["clock_seq"], "stored budget clock_seq") + 1
        previous_digest = None if latest is None else str(latest["clock_digest"])
        raw = {
            "run_id": run_id,
            "clock_seq": clock_seq,
            "transition": transition,
            "suspension_reason": suspension_reason if transition is BudgetClockTransition.SUSPENSION_STARTED else None,
            "boot_id": str(sample["boot_id"]),
            "monotonic_ns": _as_int(sample["monotonic_ns"], "budget clock monotonic_ns"),
            "wall_time": str(sample["wall_time"]),
            "state_event_id": state_event_id,
            "previous_clock_digest": previous_digest,
            "clock_digest": "",
        }
        digest_input = dict(raw)
        digest_input.pop("clock_digest")
        digest_input["transition"] = transition.value
        digest_input["state_event_payload_digest"] = state_payload_digest
        # 把权威 state event 的 Task 身份纳入摘要，防止同步改写 Run/事件/active_run_id 后复用旧链。
        digest_input["state_event_task_id"] = state_event_task_id
        raw["clock_digest"] = "sha256:" + hashlib.sha256(canonicalize(digest_input)).hexdigest()
        return raw


def _normalize_correlation_id(value: str | None, prefix: str) -> str:
    """补齐入口关联 ID；调用方若传值则必须为非空字符串。"""
    if value is not None and (type(value) is not str or not value):
        raise BudgetServiceError("budget correlation id is invalid")
    return value if value is not None else f"{prefix}-{uuid.uuid4().hex}"


__all__ = ["BudgetObservation", "BudgetService", "BudgetServiceError"]
