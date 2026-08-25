"""Phase 1 Task 3 重试分类 RED/GREEN 测试。"""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest


def _policy_module() -> ModuleType:
    """延迟导入待实现策略模块，让缺失实现保持为单项 RED。"""
    return importlib.import_module("factory_agent.policy.retry_policy")


def _identity_kwargs(**overrides: str) -> dict[str, str]:
    """每个 RETRY-001 事实都绑定完整 task/run/step/attempt lineage。"""
    values = {
        "task_id": "task-retry-1",
        "run_id": "run-retry-1",
        "step_id": "step-retry-1",
        "attempt_id": "attempt-retry-1",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("failure_class", "action", "observed_state"),
    [
        ("TRANSIENT", "RETRY", "QUEUED"),
        ("PROVIDER_RATE_LIMIT", "BLOCK", "BLOCKED"),
        ("PROVIDER_QUOTA_EXHAUSTED", "BLOCK", "BLOCKED"),
        ("FIXABLE", "REPAIR", "RUNNING"),
        ("CAPACITY", "QUEUE", "QUEUED"),
        ("AUTH_POLICY", "BLOCK", "BLOCKED"),
        ("UNKNOWN_STATE", "RECONCILE", "RECONCILING"),
        ("IRREVERSIBLE_RISK", "BLOCK", "BLOCKED"),
        ("INTERNAL_BUG", "PAUSE", "BLOCKED"),
    ],
)
def test_retry_policy_uses_mechanical_class_to_select_action(
    failure_class: str,
    action: str,
    observed_state: str,
) -> None:
    """九类冻结错误必须机械分流，不能按 Python 异常类猜测重试。"""
    module = _policy_module()

    decision = module.classify_retry(
        failure_class,
        **_identity_kwargs(),
        side_effect_started=False,
        attempt_no=1,
        remaining_budget_ms=30_000,
    )

    assert decision.retry_class.value == failure_class
    assert decision.action.value == action
    assert decision.observed_state == observed_state
    assert decision.qualification == "PARTIAL"
    assert decision.final_receipt is False
    if failure_class in {"TRANSIENT", "CAPACITY"}:
        assert decision.should_retry is True
    else:
        assert decision.should_retry is False


def test_retry_after_requires_bounded_wake_and_never_busy_polls() -> None:
    """短 Retry-After 只能生成带唤醒时间的 QUEUED 决策。"""
    module = _policy_module()

    decision = module.classify_retry(
        "PROVIDER_RATE_LIMIT",
        **_identity_kwargs(),
        retry_after_ms=2_000,
        max_auto_wait_ms=60_000,
        remaining_budget_ms=10_000,
    )

    assert decision.action.value == "QUEUE"
    assert decision.observed_state == "QUEUED"
    assert decision.should_retry is True
    assert decision.wake_after_ms == 2_000
    assert decision.busy_poll is False


def test_quota_exhausted_is_action_required_without_fast_retries() -> None:
    """订阅额度耗尽必须阻断并交给用户，不得套用三次快速重试。"""
    module = _policy_module()

    decision = module.classify_retry(
        "PROVIDER_QUOTA_EXHAUSTED",
        **_identity_kwargs(),
        side_effect_started=False,
        attempt_no=1,
        remaining_budget_ms=30_000,
    )

    assert decision.should_retry is False
    assert decision.requires_user_action is True
    assert decision.max_attempts == 0


def test_side_effect_boundary_blocks_transient_blind_retry() -> None:
    """同一 network 错误跨过副作用边界后必须转 UNKNOWN_STATE。"""
    module = _policy_module()

    decision = module.classify_retry(
        "TRANSIENT",
        **_identity_kwargs(),
        side_effect_started=True,
        completion_known=False,
        remaining_budget_ms=30_000,
    )

    assert decision.retry_class.value == "UNKNOWN_STATE"
    assert decision.action.value == "RECONCILE"
    assert decision.should_retry is False


def test_unknown_python_exception_is_not_retried_by_exception_type() -> None:
    """未经显式分类的异常只能进入 INTERNAL_BUG，禁止按异常类盲重试。"""
    module = _policy_module()

    decision = module.classify_retry(
        RuntimeError("network-like text"),
        **_identity_kwargs(),
        side_effect_started=False,
        remaining_budget_ms=30_000,
    )

    assert decision.retry_class.value == "INTERNAL_BUG"
    assert decision.should_retry is False


def test_side_effect_flags_must_be_booleans_before_classification() -> None:
    """字符串 false 不能绕过副作用未知态的 RECONCILE 门禁。"""
    module = _policy_module()
    with pytest.raises(ValueError, match="booleans"):
        module.classify_retry(
            "TRANSIENT",
            **_identity_kwargs(),
            side_effect_started=True,
            completion_known="false",
            remaining_budget_ms=30_000,
        )


def test_retry_after_missing_or_over_budget_is_action_required() -> None:
    """没有唤醒事实或预算不足时必须 BLOCK，不能生成无出口 QUEUED。"""
    module = _policy_module()

    missing = module.classify_retry(
        "PROVIDER_RATE_LIMIT",
        **_identity_kwargs(),
        remaining_budget_ms=10_000,
    )
    over_budget = module.classify_retry(
        "PROVIDER_RATE_LIMIT",
        **_identity_kwargs(run_id="run-retry-2"),
        retry_after_ms=2_000,
        remaining_budget_ms=1_000,
    )
    unknown_budget = module.classify_retry(
        "PROVIDER_RATE_LIMIT",
        **_identity_kwargs(run_id="run-retry-3"),
        retry_after_ms=1_000,
    )

    for decision in (missing, over_budget, unknown_budget):
        assert decision.action.value == "BLOCK"
        assert decision.observed_state == "BLOCKED"
        assert decision.requires_user_action is True
        assert decision.should_retry is False
        assert decision.wake_after_ms is None

    transient_unknown_budget = module.classify_retry(
        "TRANSIENT",
        **_identity_kwargs(run_id="run-retry-4"),
    )
    capacity_unknown_budget = module.classify_retry(
        "CAPACITY",
        **_identity_kwargs(run_id="run-retry-5"),
    )
    assert transient_unknown_budget.should_retry is False
    assert transient_unknown_budget.requires_user_action is True
    assert capacity_unknown_budget.should_retry is False
    assert capacity_unknown_budget.requires_user_action is True


def test_transient_has_bounded_jitter_and_only_three_attempts() -> None:
    """只有 TRANSIENT 使用三次上限和有界 jitter；结果保持可重放。"""
    module = _policy_module()
    first = module.classify_retry(
        "TRANSIENT",
        **_identity_kwargs(),
        attempt_no=1,
        remaining_budget_ms=10_000,
        max_auto_wait_ms=1_000,
    )
    repeated = module.classify_retry(
        "TRANSIENT",
        **_identity_kwargs(),
        attempt_no=1,
        remaining_budget_ms=10_000,
        max_auto_wait_ms=1_000,
    )
    exhausted = module.classify_retry(
        "TRANSIENT",
        **_identity_kwargs(),
        attempt_no=3,
        remaining_budget_ms=10_000,
    )

    assert first.should_retry is True
    assert first.wake_after_ms is not None
    assert 0 < first.wake_after_ms <= 1_000
    assert repeated.subcheck_id == first.subcheck_id
    assert exhausted.should_retry is False
    assert exhausted.requires_user_action is True


def test_retry_subcheck_id_binds_run_and_attempt_identity() -> None:
    """相同分类/等待/attemptNo 的不同运行不能碰撞 RETRY-001。"""
    module = _policy_module()
    first = module.classify_retry("TRANSIENT", **_identity_kwargs(), remaining_budget_ms=10_000)
    second = module.classify_retry(
        "TRANSIENT",
        **_identity_kwargs(run_id="run-retry-2", attempt_id="attempt-retry-2"),
        remaining_budget_ms=10_000,
    )
    assert first.subcheck_id != second.subcheck_id
