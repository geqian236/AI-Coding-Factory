"""执行 STATE-001 固定真实场景并生成 catalog 绑定的 FINAL 回执。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory_agent.application.transition_service import StateScenarioEvidenceError
from factory_agent.observability.logging import get_logger
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.testing.receipts import ReceiptAction, TestReceipt
from factory_agent.testing.required_test_catalog import (
    get_test_entry,
    load_catalog,
    scenario_contract_digest,
)

# Linux typeshed 不暴露 CREATE_NO_WINDOW；Any 视图只跨过静态平台差异，
# 条件表达式仍保证仅在 Windows 运行时读取该常量。
_WINDOWS_SUBPROCESS: Any = subprocess

LOGGER = get_logger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[5]
_STATE_001_DIGEST = "sha256:1e13c80a4032d2d6cc823e36db10bd1ee83f49a823dd9e888f0ca2ef8a7d3685"
_STATE_001_OWNER = "codex-reviewer"
_STATE_001_REPLAYS = 1
_PYTEST_TIMEOUT_SECONDS = 300

# 每个分组都固定到真实领域规则或 SQLite service/repository 测试；调用方不能传入
# 布尔结果或替换 node-id，FINAL 资格只由冻结动作的真实退出码和通过数量决定。
_SCENARIO_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "fiveDimensions",
        (
            "tests/agent/unit/state_machine/test_transitions.py::test_each_frozen_observed_transition_is_accepted",
            "tests/agent/unit/state_machine/test_transitions.py::test_every_other_observed_transition_is_rejected",
            "tests/agent/unit/state_machine/test_transitions.py::test_desired_state_accepts_only_frozen_edges",
            "tests/agent/unit/state_machine/test_transitions.py::test_desired_state_rejects_self_loops_and_cancel_reversal",
            "tests/agent/unit/state_machine/test_transitions.py::test_task_lifecycle_has_only_active_to_terminal_edges",
            "tests/agent/unit/state_machine/test_transitions.py::test_run_phase_accepts_normal_progress_and_only_three_reasoned_exceptions",
            "tests/agent/unit/state_machine/test_transitions.py::test_run_phase_rejects_unreasoned_or_wrong_reason_jumps",
            "tests/agent/unit/state_machine/test_transitions.py::test_achieved_stage_is_monotonic_and_allows_idempotent_projection",
            "tests/agent/unit/state_machine/test_transitions.py::test_step_phase_outcome_validator_freezes_the_complete_projection_matrix",
            "tests/agent/unit/state_machine/test_transitions.py::test_attempt_phase_outcome_validator_freezes_the_complete_projection_matrix",
            "tests/agent/unit/state_machine/test_transitions.py::test_executor_phase_only_transitions_accept_only_frozen_reconciling_edges",
            "tests/agent/unit/state_machine/test_transitions.py::test_step_phase_only_transition_rejects_backward_self_terminal_and_terminal_rewrite",
            "tests/agent/unit/state_machine/test_transitions.py::test_attempt_phase_only_transition_rejects_backward_self_terminal_and_terminal_rewrite",
            "tests/agent/unit/state_machine/test_transitions.py::test_attempt_termination_accepts_nonterminal_none_to_non_none",
            "tests/agent/unit/state_machine/test_transitions.py::test_attempt_termination_rejects_terminal_rewrite_or_invalid_outcome",
        ),
    ),
    (
        "samePlanRevisionMultiRun",
        (
            "tests/agent/unit/domain/test_workflow_invariants.py::test_runtime_phase_barrier_is_derived_per_run_not_copied_from_plan",
        ),
    ),
    (
        "logicalNodeReplan",
        (
            # 该 SQLite 场景让新旧 PlanRevision 复用同一 logicalNodeId，并拒绝旧修订伪造成功。
            "tests/agent/integration/control/test_control_commands.py::test_passed_barrier_rejects_step_from_stale_revision_or_phase",
        ),
    ),
    ("barrierSettledPassed", ("tests/agent/unit/state_machine/test_barriers.py",)),
    (
        "unknownSettleTimeout",
        (
            "tests/agent/integration/control/test_control_commands.py::test_unknown_remote_state_timeout_requires_all_expected_versions",
            "tests/agent/integration/control/test_control_commands.py::test_attempt_only_unknown_timeout_persists_blocked_run_event",
        ),
    ),
    (
        "milestoneArtifactAtomicity",
        (
            "tests/agent/integration/control/test_control_commands.py::test_passed_barrier_phase_and_milestone_commit_atomically",
            "tests/agent/integration/control/test_control_commands.py::test_barrier_rejects_missing_required_artifact",
        ),
    ),
    (
        "successPredicateArtifactAtomicity",
        (
            "tests/agent/unit/state_machine/test_barriers.py::test_barrier_is_settled_and_passed_only_when_all_frozen_predicates_hold",
            "tests/agent/unit/domain/test_workflow_invariants.py::test_artifact_gate_eligibility_requires_committed_complete_identity",
            "tests/agent/integration/control/test_control_commands.py::test_barrier_dependency_allows_extra_committed_artifact_from_same_attempt",
            "tests/agent/integration/control/test_control_commands.py::test_barrier_current_step_allows_extra_committed_artifact_from_same_attempt",
        ),
    ),
    (
        "fastPauseResume",
        (
            "tests/agent/integration/control/test_drain_write.py::test_fast_pause_resume_keeps_old_attempt_draining",
            "tests/agent/integration/control/test_drain_write.py::test_old_attempt_cannot_restart_normal_work_after_resume",
        ),
    ),
    (
        "casCompetition",
        (
            "tests/agent/integration/control/test_control_commands.py::test_competing_expected_state_version_allows_exactly_one_command",
            "tests/agent/integration/control/test_control_commands.py::test_idempotent_replay_rejects_orphan_command_ack_without_run_projection",
        ),
    ),
)


@dataclass(frozen=True)
class _GroupExecution:
    """记录一个固定场景组的真实 pytest 观察，不保存可能含敏感文本的原始输出。"""

    name: str
    node_ids: tuple[str, ...]
    return_code: int
    passed_count: int
    output_digest: str
    duration_ms: float
    executed_at: str


def _sha256(value: object) -> str:
    """对固定场景输入或脱敏执行摘要计算 JCS SHA-256。"""
    return "sha256:" + hashlib.sha256(canonicalize(value)).hexdigest()


def _require_temp_root(temp_root: Path) -> Path:
    """创建并限制验收临时根；本机 Windows 明确拒绝 C 盘，防止 pytest 回落。"""
    temp_root.mkdir(parents=True, exist_ok=True)
    resolved = temp_root.resolve()
    if not resolved.is_dir():
        raise StateScenarioEvidenceError("STATE-001 temp root must be an existing directory")
    if os.name == "nt" and resolved.drive.casefold() != "d:":
        raise StateScenarioEvidenceError("STATE-001 temp root must be on D drive")
    return resolved


def _passed_count(output: bytes) -> int:
    """从 pytest 机器执行摘要提取通过用例数；无合法摘要时按零处理并拒绝签发。"""
    matches = re.findall(rb"(?m)(\d+) passed(?:,| in )", output)
    return int(matches[-1]) if matches else 0


def _remove_isolated_run_root(run_root: Path, temp_root: Path) -> None:
    """只清理本次 UUID 子目录；解析后不是临时根的直属子目录就 fail closed。"""
    resolved_root = run_root.resolve()
    if resolved_root.parent != temp_root or not resolved_root.name.startswith("state-001-"):
        raise StateScenarioEvidenceError("STATE-001 cleanup target escaped temp root")
    if resolved_root.exists():
        shutil.rmtree(resolved_root)


def _run_fixed_group(
    name: str,
    node_ids: tuple[str, ...],
    temp_root: Path,
) -> _GroupExecution:
    """在 D 盘隔离 basetemp 中执行一个冻结组，并返回真实退出码与摘要。"""
    run_root = temp_root / f"state-001-{name}-{uuid.uuid4().hex}"
    command = (
        sys.executable,
        "-m",
        "pytest",
        *node_ids,
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(run_root),
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(_REPO_ROOT / "apps" / "agent" / "src")
    environment["TEMP"] = str(run_root)
    environment["TMP"] = str(run_root)
    creation_flags = _WINDOWS_SUBPROCESS.CREATE_NO_WINDOW if os.name == "nt" else 0
    started = time.monotonic()
    output = b""
    return_code = 124
    try:
        run_root.mkdir(parents=False)
        completed = subprocess.run(  # noqa: S603 - 命令与 node-id 均为模块内冻结常量。
            command,
            cwd=_REPO_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            creationflags=creation_flags,
            timeout=_PYTEST_TIMEOUT_SECONDS,
        )
        return_code = completed.returncode
        output = completed.stdout + completed.stderr
    except subprocess.TimeoutExpired as exc:
        # 超时只保留已捕获输出的摘要，绝不把原始日志写进 receipt 或结构化日志。
        output = (exc.stdout or b"") + (exc.stderr or b"")
    finally:
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        _remove_isolated_run_root(run_root, temp_root)
    return _GroupExecution(
        name=name,
        node_ids=node_ids,
        return_code=return_code,
        passed_count=_passed_count(output),
        output_digest="sha256:" + hashlib.sha256(output).hexdigest(),
        duration_ms=duration_ms,
        executed_at=datetime.now(UTC).isoformat(),
    )


def _require_catalog_binding(entry: dict[str, object]) -> None:
    """校验 STATE-001 冻结 owner/replay/digest，catalog 漂移时拒绝伪签 FINAL。"""
    if scenario_contract_digest(entry) != entry.get("scenarioContractDigest"):
        raise StateScenarioEvidenceError("STATE-001 catalog digest is inconsistent")
    if (
        entry.get("scenarioContractDigest") != _STATE_001_DIGEST
        or entry.get("finalPassOwner") != _STATE_001_OWNER
        or entry.get("requiredReplays") != _STATE_001_REPLAYS
    ):
        raise StateScenarioEvidenceError("STATE-001 catalog binding is not frozen")


async def run_state_001_rule_scenario(*, temp_root: Path) -> TestReceipt:
    """执行固定规则/SQLite 场景；任一执行或 receipt 绑定失败都拒绝 FINAL。"""
    isolated_root = _require_temp_root(temp_root)
    if sys.version_info[:2] != (3, 12):
        raise StateScenarioEvidenceError("STATE-001 FINAL requires Python 3.12")
    entry = get_test_entry("STATE-001", load_catalog())
    _require_catalog_binding(entry)

    manifest = {
        "runtime": f"python-{sys.version_info.major}.{sys.version_info.minor}",
        "pythonImplementation": platform.python_implementation(),
        "platformSystem": platform.system(),
        "sqliteVersion": sqlite3.sqlite_version,
        "foreignKeys": True,
        "scenarioGroups": [name for name, _ in _SCENARIO_GROUPS],
        "tempDrive": isolated_root.drive.casefold(),
    }
    LOGGER.info(
        "state_001_scenario_started",
        operation="state_001_scenario",
        status="started",
        duration_ms=0,
        scenario_group_count=len(_SCENARIO_GROUPS),
    )
    started = time.monotonic()
    executions: list[_GroupExecution] = []
    for name, node_ids in _SCENARIO_GROUPS:
        execution = await asyncio.to_thread(_run_fixed_group, name, node_ids, isolated_root)
        executions.append(execution)
        if execution.return_code != 0 or execution.passed_count < 1:
            LOGGER.warning(
                "state_001_scenario_group_rejected",
                operation="state_001_scenario",
                status="rejected",
                scenario_group=name,
                pytest_return_code=execution.return_code,
                passed_count=execution.passed_count,
                output_digest=execution.output_digest,
                duration_ms=execution.duration_ms,
                error_code="STATE_SCENARIO_EVIDENCE_INCOMPLETE",
            )
            raise StateScenarioEvidenceError(f"STATE-001 fixed scenario group failed: {name}")

    catalog_binding: dict[str, object] = {
        "testId": "STATE-001",
        "scenarioContractDigest": _STATE_001_DIGEST,
        "finalPassOwner": _STATE_001_OWNER,
        "requiredReplays": _STATE_001_REPLAYS,
    }
    expected: dict[str, object] = {"catalogBinding": catalog_binding}
    actual: dict[str, object] = {"catalogBinding": dict(catalog_binding)}
    actions: list[ReceiptAction] = []
    for ordinal, execution in enumerate(executions, start=1):
        expected[execution.name] = {
            "pytestExitCode": 0,
            "minimumPassedCount": 1,
            "fixedNodeCount": len(execution.node_ids),
            "allSelectedNodesMustPass": True,
        }
        actual[execution.name] = {
            "pytestExitCode": execution.return_code,
            "passedCount": execution.passed_count,
            "fixedNodeCount": len(execution.node_ids),
            "selectedNodes": list(execution.node_ids),
            "selectedNodesDigest": _sha256(list(execution.node_ids)),
            "outputDigest": execution.output_digest,
            "durationMs": execution.duration_ms,
        }
        actions.append(
            ReceiptAction(
                actionId=f"state-001-{ordinal:02d}-{execution.name}",
                description=f"执行 STATE-001 固定真实场景组 {execution.name}",
                executedAt=execution.executed_at,
                inputDigest=_sha256(list(execution.node_ids)),
            )
        )

    result_material = [
        {
            "name": execution.name,
            "returnCode": execution.return_code,
            "passedCount": execution.passed_count,
            "outputDigest": execution.output_digest,
        }
        for execution in executions
    ]
    receipt = TestReceipt(
        receiptId=f"state-001-{_sha256(result_material).removeprefix('sha256:')}",
        testId="STATE-001",
        environmentManifestDigest=_sha256(manifest),
        actions=actions,
        expected=expected,
        actual=actual,
        sideEffectCount=0,
        artifactDigests=[execution.output_digest for execution in executions],
        result="PASS",
        createdAt=datetime.now(UTC).isoformat(),
        scenarioContractDigest=_STATE_001_DIGEST,
        implementationContributors=list(entry["implementationContributors"]),
        finalPassOwner=_STATE_001_OWNER,
        qualification="FINAL",
        requiredReplays=_STATE_001_REPLAYS,
        runtimeId="python-3.12",
    )
    errors = receipt.validate()
    if errors or (
        receipt.scenarioContractDigest != _STATE_001_DIGEST
        or receipt.finalPassOwner != _STATE_001_OWNER
        or receipt.requiredReplays != _STATE_001_REPLAYS
        or receipt.qualification != "FINAL"
    ):
        raise StateScenarioEvidenceError("STATE-001 final receipt validation failed")
    duration_ms = round((time.monotonic() - started) * 1000, 3)
    LOGGER.info(
        "state_001_scenario_completed",
        operation="state_001_scenario",
        status="committed",
        duration_ms=duration_ms,
        scenario_group_count=len(executions),
        passed_count=sum(execution.passed_count for execution in executions),
        receipt_digest=_sha256(receipt.to_dict()),
    )
    return receipt


__all__ = ["run_state_001_rule_scenario"]
