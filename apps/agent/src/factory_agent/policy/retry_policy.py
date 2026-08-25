"""Phase 1 Task 3 的机械重试分类策略。

该模块只接受显式 ``failure_class``，不从 Python 异常文本推断可重试性。策略
结果是带任务/运行/步骤/Attempt 身份的可序列化领域事实，供 scheduler 和
receipt 链路消费；本任务只生成 PARTIAL subcheck，不签发正式 FINAL receipt。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from factory_agent.observability.logging import get_logger
from factory_agent.policy.canonical_json import canonicalize

LOGGER = get_logger(__name__)


class RetryClass(StrEnum):
    """冻结的九类失败原因闭集。"""

    TRANSIENT = "TRANSIENT"
    PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT"
    PROVIDER_QUOTA_EXHAUSTED = "PROVIDER_QUOTA_EXHAUSTED"
    FIXABLE = "FIXABLE"
    CAPACITY = "CAPACITY"
    AUTH_POLICY = "AUTH_POLICY"
    UNKNOWN_STATE = "UNKNOWN_STATE"
    IRREVERSIBLE_RISK = "IRREVERSIBLE_RISK"
    INTERNAL_BUG = "INTERNAL_BUG"


class RetryAction(StrEnum):
    """调度器下一步动作闭集。"""

    RETRY = "RETRY"
    QUEUE = "QUEUE"
    BLOCK = "BLOCK"
    REPAIR = "REPAIR"
    RECONCILE = "RECONCILE"
    PAUSE = "PAUSE"


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """机械分类的可重放决策，不携带异常正文或敏感 payload。"""

    task_id: str
    run_id: str
    step_id: str
    attempt_id: str
    retry_class: RetryClass
    action: RetryAction
    observed_state: str
    should_retry: bool
    requires_user_action: bool
    max_attempts: int
    wake_after_ms: int | None
    busy_poll: bool
    subcheck_id: str
    qualification: str
    final_receipt: bool


def _identity(value: object, label: str) -> str:
    """身份是 subcheckId 的稳定输入，缺失时拒绝生成可能碰撞的事实。"""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    """严格收窄数值输入，禁止 bool、字符串或负数改变策略边界。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _subcheck_id(
    retry_class: RetryClass,
    *,
    task_id: str,
    run_id: str,
    step_id: str,
    attempt_id: str,
    attempt_no: int,
    wake_after_ms: int | None,
    requires_user_action: bool,
) -> str:
    """以完整 lineage 和机械决策输入生成稳定、跨任务不碰撞的 PARTIAL ID。"""
    payload = {
        "testId": "RETRY-001",
        "taskId": task_id,
        "runId": run_id,
        "stepId": step_id,
        "attemptId": attempt_id,
        "retryClass": retry_class.value,
        "attemptNo": attempt_no,
        "wakeAfterMs": wake_after_ms,
        "requiresUserAction": requires_user_action,
    }
    digest = hashlib.sha256(canonicalize(payload)).hexdigest()
    return f"RETRY-001:{retry_class.value.lower()}:{digest[:16]}"


def _transient_delay_ms(
    *,
    task_id: str,
    run_id: str,
    step_id: str,
    attempt_id: str,
    attempt_no: int,
    max_auto_wait_ms: int,
) -> int:
    """生成有界、可重放且带确定性 jitter 的短退避，不在本地 sleep。"""
    exponential: int = min(max_auto_wait_ms, 100 * (2 ** min(attempt_no, 8)))
    seed: bytes = hashlib.sha256(
        canonicalize(
            {
                "taskId": task_id,
                "runId": run_id,
                "stepId": step_id,
                "attemptId": attempt_id,
                "attemptNo": attempt_no,
            }
        )
    ).digest()
    jitter: int = int.from_bytes(seed[:4], "big") % max(1, exponential // 2 + 1)
    delay: int = exponential + jitter
    return max_auto_wait_ms if delay > max_auto_wait_ms else delay


def classify_retry(
    failure_class: object,
    *,
    task_id: str,
    run_id: str,
    step_id: str,
    attempt_id: str,
    side_effect_started: bool = False,
    completion_known: bool = True,
    attempt_no: int = 0,
    remaining_budget_ms: int | None = None,
    retry_after_ms: int | None = None,
    max_auto_wait_ms: int = 60_000,
) -> RetryDecision:
    """按照显式失败分类机械分流，并拒绝副作用后的盲重试。

    Retry-After 缺失、类型非法、超过自动等待上限或无法被剩余预算覆盖时，
    明确进入 BLOCKED/ACTION_REQUIRED，避免产生无唤醒时间的 QUEUED 假出口。
    """
    stable_task_id = _identity(task_id, "task_id")
    stable_run_id = _identity(run_id, "run_id")
    stable_step_id = _identity(step_id, "step_id")
    stable_attempt_id = _identity(attempt_id, "attempt_id")
    safe_attempt = _nonnegative_int(attempt_no, "attempt_no")
    safe_max_wait = _nonnegative_int(max_auto_wait_ms, "max_auto_wait_ms")
    if not isinstance(side_effect_started, bool) or not isinstance(completion_known, bool):
        raise ValueError("side_effect_started and completion_known must be booleans")
    if remaining_budget_ms is not None:
        remaining_budget_ms = _nonnegative_int(remaining_budget_ms, "remaining_budget_ms")
    if retry_after_ms is not None:
        retry_after_ms = _nonnegative_int(retry_after_ms, "retry_after_ms")

    normalized = failure_class if isinstance(failure_class, str) else RetryClass.INTERNAL_BUG.value
    try:
        retry_class = RetryClass(normalized)
    except ValueError:
        retry_class = RetryClass.INTERNAL_BUG

    if side_effect_started and not completion_known:
        # 已经可能产生外部副作用，必须先查事实，禁止按异常类自动重放。
        retry_class = RetryClass.UNKNOWN_STATE

    action = RetryAction.PAUSE
    observed = "BLOCKED"
    should_retry = False
    requires_user_action = False
    max_attempts = 0
    wake_after: int | None = None

    if retry_class is RetryClass.TRANSIENT:
        max_attempts = 3
        if (
            safe_attempt < max_attempts
            and safe_max_wait > 0
            and remaining_budget_ms is not None
            and remaining_budget_ms > 0
        ):
            candidate = _transient_delay_ms(
                task_id=stable_task_id,
                run_id=stable_run_id,
                step_id=stable_step_id,
                attempt_id=stable_attempt_id,
                attempt_no=safe_attempt,
                max_auto_wait_ms=safe_max_wait,
            )
            if candidate < remaining_budget_ms:
                action, observed, should_retry, wake_after = RetryAction.RETRY, "QUEUED", True, candidate
            else:
                requires_user_action = True
        else:
            requires_user_action = True
    elif retry_class is RetryClass.PROVIDER_RATE_LIMIT:
        # Provider 限流没有明确 Retry-After 时不能制造可执行的自动队列。
        if (
            retry_after_ms is not None
            and retry_after_ms > 0
            and safe_max_wait > 0
            and retry_after_ms <= safe_max_wait
            and remaining_budget_ms is not None
            and retry_after_ms < remaining_budget_ms
        ):
            action, observed, should_retry, wake_after = RetryAction.QUEUE, "QUEUED", True, retry_after_ms
        else:
            requires_user_action = True
    elif retry_class is RetryClass.CAPACITY:
        if remaining_budget_ms is not None and remaining_budget_ms > 0:
            action, observed, should_retry = RetryAction.QUEUE, "QUEUED", True
        else:
            requires_user_action = True
    elif retry_class is RetryClass.FIXABLE:
        action, observed = RetryAction.REPAIR, "RUNNING"
    elif retry_class in {
        RetryClass.PROVIDER_QUOTA_EXHAUSTED,
        RetryClass.AUTH_POLICY,
        RetryClass.IRREVERSIBLE_RISK,
    }:
        requires_user_action = True
    elif retry_class is RetryClass.UNKNOWN_STATE:
        action, observed = RetryAction.RECONCILE, "RECONCILING"
    else:
        # INTERNAL_BUG 只暂停受影响任务，并留下脱敏诊断入口。
        requires_user_action = True

    if requires_user_action and retry_class not in {
        RetryClass.UNKNOWN_STATE,
        RetryClass.FIXABLE,
        RetryClass.INTERNAL_BUG,
    }:
        action, observed, should_retry, wake_after = RetryAction.BLOCK, "BLOCKED", False, None

    subcheck_id = _subcheck_id(
        retry_class,
        task_id=stable_task_id,
        run_id=stable_run_id,
        step_id=stable_step_id,
        attempt_id=stable_attempt_id,
        attempt_no=safe_attempt,
        wake_after_ms=wake_after,
        requires_user_action=requires_user_action,
    )
    decision = RetryDecision(
        task_id=stable_task_id,
        run_id=stable_run_id,
        step_id=stable_step_id,
        attempt_id=stable_attempt_id,
        retry_class=retry_class,
        action=action,
        observed_state=observed,
        should_retry=should_retry,
        requires_user_action=requires_user_action,
        max_attempts=max_attempts,
        wake_after_ms=wake_after,
        busy_poll=False,
        subcheck_id=subcheck_id,
        qualification="PARTIAL",
        final_receipt=False,
    )
    LOGGER.info(
        "retry_classified",
        operation="retry.classify",
        status="success",
        task_id=decision.task_id,
        run_id=decision.run_id,
        step_id=decision.step_id,
        attempt_id=decision.attempt_id,
        retry_class=decision.retry_class.value,
        action=decision.action.value,
        should_retry=decision.should_retry,
        attempt_no=safe_attempt,
        wake_after_ms=decision.wake_after_ms,
        qualification=decision.qualification,
        final_receipt=decision.final_receipt,
    )
    return decision


__all__ = ["RetryAction", "RetryClass", "RetryDecision", "classify_retry"]
