"""工作流五路 aggregate 与不可变 PlanRevision 的持久化 primitive。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from factory_agent.domain.workflow import Attempt, PhaseBarrier, Run, Step, Task


class WorkflowStore(Protocol):
    """冻结 Task 1 所需的 insert/get/CAS 与 append-only plan revision 表面。"""

    def insert_project(self, record: Mapping[str, object]) -> None: ...
    def get_task(self, task_id: str) -> Mapping[str, object] | None: ...
    def insert_task(self, record: Mapping[str, object]) -> None: ...
    def compare_and_set_task(
        self,
        *,
        task_id: str,
        expected_state_version: int,
        achieved_stage: str,
        outcome_version: int,
    ) -> Task: ...
    def get_plan_revision(self, plan_revision_id: str) -> Mapping[str, object] | None: ...
    def append_plan_revision(self, record: Mapping[str, object]) -> None: ...
    def get_run(self, run_id: str) -> Mapping[str, object] | None: ...
    def insert_run(self, record: Mapping[str, object]) -> None: ...
    def compare_and_set_run(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        observed_state: str,
    ) -> Run: ...
    def get_phase_barrier(self, barrier_id: str) -> Mapping[str, object] | None: ...
    def insert_phase_barrier(self, record: Mapping[str, object]) -> None: ...
    def compare_and_set_phase_barrier(
        self,
        *,
        barrier_id: str,
        expected_state_version: int,
        settled: bool,
        passed: bool,
    ) -> PhaseBarrier: ...
    def get_step(self, step_id: str) -> Mapping[str, object] | None: ...
    def insert_step(self, record: Mapping[str, object]) -> None: ...
    def compare_and_set_step(
        self,
        *,
        step_id: str,
        expected_state_version: int,
        phase: str,
    ) -> Step: ...
    def get_attempt(self, attempt_id: str) -> Mapping[str, object] | None: ...
    def insert_attempt(self, record: Mapping[str, object]) -> None: ...
    def compare_and_set_attempt(
        self,
        *,
        attempt_id: str,
        expected_state_version: int,
        phase: str,
    ) -> Attempt: ...


__all__ = ["WorkflowStore"]
