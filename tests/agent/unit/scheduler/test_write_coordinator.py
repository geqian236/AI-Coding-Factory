"""FIFO 单 writer coordinator 的线程、事务、关闭与日志 RED 测试。"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Protocol

import pytest

TicketLifecycle = tuple[tuple[int, str, int], ...]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
CHILD_TIMEOUT_SECONDS = 8.0

REENTRANT_CHILD_PROGRAM = r"""
import asyncio
import json
import runpy
import sys

namespace = runpy.run_path(sys.argv[1])

async def bounded(awaitable):
    inner = asyncio.wait_for(awaitable, timeout=2.0)
    return await asyncio.wait_for(inner, timeout=2.5)

async def main():
    module = namespace["_coordinator_module"]()
    stack = namespace["_new_stack"]()
    await bounded(stack.coordinator.start())

    def reentrant(_uow):
        asyncio.run(
            bounded(
                stack.coordinator.execute(
                    operation="nested",
                    context={"correlation_id": "corr-nested"},
                    command=lambda _nested_uow: None,
                )
            )
        )

    try:
        await bounded(stack.coordinator.execute(
            operation="outer",
            context={"correlation_id": "corr-outer"},
            command=reentrant,
        ))
    except module.ReentrantWriteError as exc:
        error_code = exc.error_code
    else:
        raise AssertionError("reentrant execute unexpectedly succeeded")
    finally:
        await bounded(stack.coordinator.close())

    owner_alive = stack.coordinator.owner_thread.is_alive()
    if owner_alive:
        raise AssertionError("reentrant path leaked owner thread")
    print(json.dumps({
        "receiptVersion": 1,
        "kind": "coordinator-reentrant-result",
        "status": "rejected",
        "errorCode": error_code,
        "ownerThreadAlive": owner_alive,
        "trace": [event for event, _thread in stack.database.trace],
    }, separators=(",", ":")), flush=True)

asyncio.run(main())
"""


def _coordinator_module() -> ModuleType:
    """延迟导入待实现 coordinator，使缺实现保持为 pytest failure。"""
    return importlib.import_module("factory_agent.scheduler.write_coordinator")


class _FakeStore:
    """只暴露同连接 identity 的最小 repository fake。"""

    def __init__(self, connection_identity: object) -> None:
        self.connection_identity = connection_identity


class _FakeUnitOfWork:
    """记录显式 BEGIN IMMEDIATE/COMMIT/ROLLBACK 与并发计数。"""

    def __init__(self, database: _FakeDatabase) -> None:
        self._database = database
        self.connection_identity = database.connection_identity
        self.workflow = _FakeStore(self.connection_identity)
        self.events = _FakeStore(self.connection_identity)
        self.authorization = _FakeStore(self.connection_identity)
        self.resources = _FakeStore(self.connection_identity)
        self.active = False

    def begin_immediate(self) -> None:
        """模拟显式 BEGIN IMMEDIATE，并拒绝同时存在第二事务。"""
        self._database.trace.append(("BEGIN IMMEDIATE", threading.get_ident()))
        if self._database.begin_error is not None:
            raise self._database.begin_error
        self.active = True
        self._database.active_transactions += 1
        self._database.max_active_transactions = max(
            self._database.max_active_transactions,
            self._database.active_transactions,
        )

    def commit(self) -> None:
        """模拟真实 COMMIT；异常时 outcome 必须按 unknown 处理。"""
        self._database.trace.append(("COMMIT", threading.get_ident()))
        if self._database.commit_error is not None:
            raise self._database.commit_error
        assert self.active
        self.active = False
        self._database.active_transactions -= 1

    def rollback(self) -> None:
        """模拟显式 ROLLBACK；失败时连接必须 poison。"""
        self._database.trace.append(("ROLLBACK", threading.get_ident()))
        if self._database.rollback_error is not None:
            raise self._database.rollback_error
        if self.active:
            self.active = False
            self._database.active_transactions -= 1

    def precommit(self) -> None:
        """模拟 UoW 权威 state/event 一一配对检查切点。"""
        self._database.trace.append(("PRECOMMIT", threading.get_ident()))
        if self._database.precommit_error is not None:
            raise self._database.precommit_error


class _FatalWriterFault(BaseException):
    """模拟 owner worker 无法继续的 fatal BaseException。"""


class _FakeDatabase:
    """由 coordinator owner 线程独占的 database fake。"""

    def __init__(self) -> None:
        self.connection_identity = object()
        self.trace: list[tuple[str, int]] = []
        self.active_transactions = 0
        self.max_active_transactions = 0
        self.begin_error: BaseException | None = None
        self.commit_error: BaseException | None = None
        self.rollback_error: BaseException | None = None
        self.new_uow_error: BaseException | None = None
        self.precommit_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.owner_thread_id: int | None = None
        self.closed = False
        self.close_attempted = False
        self.poison_calls = 0
        self.lifecycle: list[str] = []

    def open(self) -> SimpleNamespace:
        """记录 owner-thread open 并返回实际 readback 形状的 readiness。"""
        self.owner_thread_id = threading.get_ident()
        self.trace.append(("DATABASE_OPEN", self.owner_thread_id))
        self.lifecycle.append("DATABASE_OPEN")
        return SimpleNamespace(
            owner_thread_id=self.owner_thread_id,
            foreign_keys=1,
            journal_mode="wal",
            synchronous=2,
            schema_version=4,
            applied_migrations=(),
        )

    def new_unit_of_work(self) -> _FakeUnitOfWork:
        """每个 command 新建同连接 UoW；fatal 用于验证 pending future 闭合。"""
        if self.new_uow_error is not None:
            raise self.new_uow_error
        return _FakeUnitOfWork(self)

    def poison(self) -> None:
        """模拟 outcome unknown/rollback failure 后禁止复用并放弃事务状态。"""
        self.trace.append(("DATABASE_POISON", threading.get_ident()))
        self.poison_calls += 1
        self.active_transactions = 0

    def close(self) -> None:
        """只允许 owner thread 在无活动事务时关闭。"""
        self.trace.append(("DATABASE_CLOSE", threading.get_ident()))
        self.lifecycle.append("DATABASE_CLOSE")
        self.close_attempted = True
        assert threading.get_ident() == self.owner_thread_id
        assert self.active_transactions == 0
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class _FakeLease:
    """记录 mutex release/CloseHandle，并强制 owner-thread affinity。"""

    abandoned = False

    def __init__(self, mutex: _FakeMutex) -> None:
        self._mutex = mutex
        self.released = False
        self.handle_closed = False

    def release(self) -> None:
        """DB 完全关闭后才允许释放。"""
        assert self._mutex.database.closed
        assert threading.get_ident() == self._mutex.owner_thread_id
        assert not self.released
        self._mutex.trace.append(("MUTEX_RELEASE", threading.get_ident()))
        self._mutex.database.lifecycle.append("MUTEX_RELEASE")
        if self._mutex.release_error is not None:
            raise self._mutex.release_error
        self.released = True

    def close(self) -> None:
        """模拟最后 CloseHandle。"""
        assert not self.handle_closed
        self.handle_closed = True
        self._mutex.trace.append(("MUTEX_CLOSE", threading.get_ident()))
        self._mutex.database.lifecycle.append("MUTEX_CLOSE")
        if self._mutex.close_error is not None:
            raise self._mutex.close_error


class _FakeMutex:
    """给 coordinator 提供先于 database.open 的 mutex fake。"""

    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database
        self.owner_thread_id: int | None = None
        self.trace: list[tuple[str, int]] = []
        self.release_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.lease: _FakeLease | None = None

    def acquire(self) -> _FakeLease:
        """记录 acquire；同一个 fake 不允许第二 owner。"""
        if self.owner_thread_id is not None:
            raise TimeoutError("mutex already owned")
        self.owner_thread_id = threading.get_ident()
        self.trace.append(("MUTEX_ACQUIRE", self.owner_thread_id))
        self.database.lifecycle.append("MUTEX_ACQUIRE")
        self.lease = _FakeLease(self)
        return self.lease


@dataclass(slots=True)
class _Stack:
    """聚合测试 coordinator、database 与 mutex。"""

    coordinator: Any
    database: _FakeDatabase
    mutex: _FakeMutex


_LIVE_STACKS: list[_Stack] = []


class _ScriptedMonotonic:
    """以预置 ns 值冻结 transaction timing 的采样次数和 exact delta。"""

    def __init__(self) -> None:
        self._values: list[int] = []
        self.calls = 0
        self._scripted = False

    def reset(self, values: list[int]) -> None:
        """在 coordinator start 后注入单 ticket 的冻结采样序列。"""
        self._values = list(values)
        self.calls = 0
        self._scripted = True

    def __call__(self) -> int:
        """每个边界只允许消费一个值；多采样立即暴露 instrumentation drift。"""
        if not self._scripted:
            return time.monotonic_ns()
        if self.calls >= len(self._values):
            raise AssertionError("unexpected monotonic_ns sample")
        value = self._values[self.calls]
        self.calls += 1
        return value

    def stop(self) -> None:
        """事务断言完成后恢复真实 monotonic，避免 close 日志消费测试序列。"""
        self._scripted = False


def _new_stack(
    *,
    queue_capacity: int = 8,
    monotonic_ns: Callable[[], int] | None = None,
) -> _Stack:
    """创建一个未启动的 fake-backed coordinator。"""
    module = _coordinator_module()
    database = _FakeDatabase()
    mutex = _FakeMutex(database)
    coordinator_kwargs: dict[str, object] = {
        "database": database,
        "mutex": mutex,
        "queue_capacity": queue_capacity,
    }
    if monotonic_ns is not None:
        coordinator_kwargs["monotonic_ns"] = monotonic_ns
    coordinator = module.WriteCoordinator(
        **coordinator_kwargs,
    )
    stack = _Stack(coordinator=coordinator, database=database, mutex=mutex)
    _LIVE_STACKS.append(stack)
    return stack


async def _wait_thread_event(event: threading.Event, timeout: float = 2.0) -> None:
    """不阻塞 asyncio loop 地等待 owner-thread barrier。"""
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, timeout), timeout=timeout + 0.5)


async def _bounded[ResultT](awaitable: Awaitable[ResultT], *, timeout: float = 3.0) -> ResultT:
    """所有可能受 owner/queue 影响的 await 均再套外层硬超时，防止 RED 挂死。"""
    inner = asyncio.wait_for(awaitable, timeout=timeout)
    return await asyncio.wait_for(inner, timeout=timeout + 0.5)


def _reentrant_child_receipt() -> dict[str, object]:
    """在可 terminate/kill 的 child 内验证 owner-thread reentrant 拒绝，避免失败 RED 挂死主 pytest。"""
    environment = dict(os.environ)
    agent_src = REPOSITORY_ROOT / "apps/agent/src"
    current_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(agent_src) + (os.pathsep + current_pythonpath if current_pythonpath else "")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    allowed_temp = Path("D:/codex项目").resolve()
    for variable in ("TEMP", "TMP"):
        assert variable in environment
        Path(environment[variable]).resolve().relative_to(allowed_temp)

    process = subprocess.Popen(
        [sys.executable, "-c", REENTRANT_CHILD_PROGRAM, str(Path(__file__).resolve())],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
        close_fds=True,
    )
    timed_out = False
    try:
        try:
            stdout, stderr = process.communicate(timeout=CHILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=2)
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    assert process.poll() is not None
    assert not timed_out, f"reentrant child timeout; stderr={stderr!r}"
    assert process.returncode == 0, stderr
    lines = stdout.splitlines()
    assert len(lines) == 1, f"reentrant child 必须仅有一条 receipt：{lines!r}"
    receipt = json.loads(lines[0])
    assert receipt["receiptVersion"] == 1
    assert receipt["kind"] == "coordinator-reentrant-result"
    assert receipt["ownerThreadAlive"] is False
    return receipt


@pytest.fixture(autouse=True)
async def _bounded_stack_cleanup() -> AsyncIterator[None]:
    """每个测试最终都 bounded close，并断言没有遗留 owner thread。"""
    _LIVE_STACKS.clear()
    yield
    failures: list[Exception] = []
    for stack in reversed(_LIVE_STACKS):
        owner_thread = getattr(stack.coordinator, "owner_thread", None)
        if owner_thread is not None and owner_thread.is_alive():
            try:
                await _bounded(stack.coordinator.close(), timeout=2.0)
            except Exception as exc:  # noqa: BLE001 - teardown 必须聚合所有清理失败
                failures.append(exc)
        if owner_thread is not None and owner_thread.is_alive():
            failures.append(AssertionError("write coordinator owner thread leaked"))
    _LIVE_STACKS.clear()
    if failures:
        raise ExceptionGroup("write coordinator cleanup failed", failures)


class _TicketLifecyclePort(Protocol):
    """只描述测试使用的 primitive ticket receipt snapshot。"""

    def ticket_lifecycle_snapshot(self) -> TicketLifecycle:
        """返回不可变 lifecycle receipt。"""
        ...


async def _wait_for_lifecycle(
    coordinator: _TicketLifecyclePort,
    predicate: Callable[[TicketLifecycle], bool],
    *,
    timeout: float = 2.0,
) -> TicketLifecycle:
    """用内层 deadline 轮询+外层 wait_for 等待 ticket receipt，不使用裸 sleep 作为 oracle。"""

    async def poll() -> TicketLifecycle:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            snapshot = tuple(coordinator.ticket_lifecycle_snapshot())
            if predicate(snapshot):
                return snapshot
            await asyncio.sleep(0)
        raise TimeoutError("ticket lifecycle predicate was not satisfied")

    inner = asyncio.wait_for(poll(), timeout=timeout)
    return await asyncio.wait_for(inner, timeout=timeout + 0.5)


@pytest.mark.asyncio
async def test_p09_fifo_commands_share_one_non_daemon_owner_thread() -> None:
    """所有 callback/BEGIN/COMMIT 都在唯一 non-daemon owner thread 串行执行。"""
    stack = _new_stack()
    await _bounded(stack.coordinator.start())
    callback_threads: list[int] = []
    try:
        first = await _bounded(
            stack.coordinator.execute(
                operation="first",
                context={"correlation_id": "corr-first"},
                command=lambda _uow: callback_threads.append(threading.get_ident()) or "first",
            )
        )
        second = await _bounded(
            stack.coordinator.execute(
                operation="second",
                context={"correlation_id": "corr-second"},
                command=lambda _uow: callback_threads.append(threading.get_ident()) or "second",
            )
        )
    finally:
        await _bounded(stack.coordinator.close())

    assert (first, second) == ("first", "second")
    assert len(set(callback_threads)) == 1
    assert callback_threads[0] == stack.database.owner_thread_id
    assert stack.coordinator.owner_thread.daemon is False
    assert [event for event, _thread in stack.database.trace] == [
        "DATABASE_OPEN",
        "BEGIN IMMEDIATE",
        "PRECOMMIT",
        "COMMIT",
        "BEGIN IMMEDIATE",
        "PRECOMMIT",
        "COMMIT",
        "DATABASE_CLOSE",
    ]
    lifecycle = tuple(stack.coordinator.ticket_lifecycle_snapshot())
    assert [ticket for ticket, stage, _seq in lifecycle if stage == "started"] == [1, 2]
    for ticket in (1, 2):
        assert [stage for actual, stage, _seq in lifecycle if actual == ticket] == [
            "admission_attempted",
            "admitted",
            "dequeued",
            "started",
            "settled",
        ]
    assert [sequence for _ticket, _stage, sequence in lifecycle] == list(range(1, len(lifecycle) + 1))


@pytest.mark.asyncio
async def test_c01_second_submission_does_not_begin_before_first_commits() -> None:
    """第一个 callback 卡在 barrier 时，第二个虽已 submit 也不能 BEGIN。"""
    stack = _new_stack()
    first_entered = threading.Event()
    release_first = threading.Event()
    execution_order: list[str] = []

    def first_command(_uow: _FakeUnitOfWork) -> str:
        execution_order.append("first-start")
        first_entered.set()
        assert release_first.wait(timeout=2)
        execution_order.append("first-end")
        return "first"

    def second_command(_uow: _FakeUnitOfWork) -> str:
        execution_order.append("second")
        return "second"

    await _bounded(stack.coordinator.start())
    first_task = asyncio.create_task(
        stack.coordinator.execute(
            operation="first-blocking",
            context={"correlation_id": "corr-c01-first"},
            command=first_command,
        )
    )
    await _wait_thread_event(first_entered)
    second_task = asyncio.create_task(
        stack.coordinator.execute(
            operation="second-queued",
            context={"correlation_id": "corr-c01-second"},
            command=second_command,
        )
    )
    await asyncio.sleep(0.05)
    assert not second_task.done()
    assert [event for event, _thread in stack.database.trace].count("BEGIN IMMEDIATE") == 1

    release_first.set()
    try:
        assert await _bounded(first_task) == "first"
        assert await _bounded(second_task) == "second"
    finally:
        await _bounded(stack.coordinator.close())
    assert execution_order == ["first-start", "first-end", "second"]


@pytest.mark.asyncio
async def test_c02_many_producers_never_create_two_active_transactions() -> None:
    """并发 producer 只能增加 FIFO 深度，不能打开第二 connection/transaction。"""
    stack = _new_stack(queue_capacity=32)
    await _bounded(stack.coordinator.start())

    def command(value: int) -> int:
        time.sleep(0.002)
        return value

    try:
        results = await _bounded(
            asyncio.gather(
                *(
                    stack.coordinator.execute(
                        operation="multi-producer",
                        context={"correlation_id": f"corr-producer-{index}"},
                        command=lambda _uow, value=index: command(value),
                    )
                    for index in range(20)
                )
            )
        )
    finally:
        await _bounded(stack.coordinator.close())
    assert sorted(results) == list(range(20))
    assert stack.database.max_active_transactions == 1
    assert stack.database.trace.count(("DATABASE_OPEN", stack.database.owner_thread_id or -1)) == 1


@pytest.mark.asyncio
async def test_bounded_admission_awaits_capacity_without_drop_or_second_writer() -> None:
    """容量只计算 queued：active+一个 admitted queue 后，第三项才等待容量。"""
    stack = _new_stack(queue_capacity=1)
    entered = threading.Event()
    release = threading.Event()

    def blocking(_uow: _FakeUnitOfWork) -> str:
        entered.set()
        assert release.wait(timeout=2)
        return "first"

    await _bounded(stack.coordinator.start())
    first = asyncio.create_task(
        stack.coordinator.execute(
            operation="capacity-first",
            context={"correlation_id": "corr-capacity-first"},
            command=blocking,
        )
    )
    await _wait_thread_event(entered)
    second = asyncio.create_task(
        stack.coordinator.execute(
            operation="capacity-second",
            context={"correlation_id": "corr-capacity-second"},
            command=lambda _uow: "second",
        )
    )
    third = asyncio.create_task(
        stack.coordinator.execute(
            operation="capacity-third",
            context={"correlation_id": "corr-capacity-third"},
            command=lambda _uow: "third",
        )
    )
    snapshot = await _wait_for_lifecycle(
        stack.coordinator,
        lambda events: (
            {(ticket, stage) for ticket, stage, _seq in events}
            >= {(1, "started"), (2, "admitted"), (3, "waiting_for_capacity")}
        ),
    )
    assert not second.done()
    assert not third.done()
    assert stack.coordinator.queue_depth == 1
    assert [stage for ticket, stage, _seq in snapshot if ticket == 3] == [
        "admission_attempted",
        "waiting_for_capacity",
    ]
    release.set()
    try:
        assert await _bounded(asyncio.gather(first, second, third)) == ["first", "second", "third"]
    finally:
        await _bounded(stack.coordinator.close())

    lifecycle = tuple(stack.coordinator.ticket_lifecycle_snapshot())
    assert [ticket for ticket, stage, _seq in lifecycle if stage == "admission_attempted"] == [1, 2, 3]
    assert [ticket for ticket, stage, _seq in lifecycle if stage == "waiting_for_capacity"] == [3]
    assert [ticket for ticket, stage, _seq in lifecycle if stage == "admitted"] == [1, 2, 3]
    assert [ticket for ticket, stage, _seq in lifecycle if stage == "dequeued"] == [1, 2, 3]
    assert [ticket for ticket, stage, _seq in lifecycle if stage == "started"] == [1, 2, 3]
    assert [ticket for ticket, stage, _seq in lifecycle if stage == "settled"] == [1, 2, 3]
    for ticket in (1, 2, 3):
        stages = [stage for actual, stage, _seq in lifecycle if actual == ticket]
        expected = ["admission_attempted"]
        if ticket == 3:
            expected.append("waiting_for_capacity")
        expected.extend(("admitted", "dequeued", "started", "settled"))
        assert stages == expected
    assert [sequence for _ticket, _stage, sequence in lifecycle] == list(range(1, len(lifecycle) + 1))


@pytest.mark.asyncio
async def test_capacity_waiters_resume_in_fifo_order_when_multiple_callers_wait() -> None:
    """多个 caller 同时等待 queued 容量时，后到 waiter 不能通过轮询抢先 admission。"""
    stack = _new_stack(queue_capacity=1)
    entered = threading.Event()
    release = threading.Event()
    execution_order: list[int] = []

    def active(_uow: _FakeUnitOfWork) -> int:
        execution_order.append(1)
        entered.set()
        assert release.wait(timeout=2)
        return 1

    def queued(ticket_id: int) -> Callable[[_FakeUnitOfWork], int]:
        def command(_uow: _FakeUnitOfWork) -> int:
            execution_order.append(ticket_id)
            return ticket_id

        return command

    await _bounded(stack.coordinator.start())
    first = asyncio.create_task(
        stack.coordinator.execute(operation="fifo-active", context={"correlation_id": "corr-fifo-1"}, command=active)
    )
    await _wait_thread_event(entered)
    second = asyncio.create_task(
        stack.coordinator.execute(operation="fifo-queued", context={"correlation_id": "corr-fifo-2"}, command=queued(2))
    )
    await _wait_for_lifecycle(stack.coordinator, lambda events: (2, "admitted") in {(t, s) for t, s, _ in events})
    third = asyncio.create_task(
        stack.coordinator.execute(
            operation="fifo-waiter-3", context={"correlation_id": "corr-fifo-3"}, command=queued(3)
        )
    )
    await _wait_for_lifecycle(
        stack.coordinator,
        lambda events: (3, "waiting_for_capacity") in {(t, s) for t, s, _ in events},
    )
    fourth = asyncio.create_task(
        stack.coordinator.execute(
            operation="fifo-waiter-4", context={"correlation_id": "corr-fifo-4"}, command=queued(4)
        )
    )
    await _wait_for_lifecycle(
        stack.coordinator,
        lambda events: (4, "waiting_for_capacity") in {(t, s) for t, s, _ in events},
    )
    release.set()
    try:
        assert await _bounded(asyncio.gather(first, second, third, fourth)) == [1, 2, 3, 4]
    finally:
        await _bounded(stack.coordinator.close())
    lifecycle = tuple(stack.coordinator.ticket_lifecycle_snapshot())
    assert [ticket for ticket, stage, _seq in lifecycle if stage == "admitted"] == [1, 2, 3, 4]
    assert [ticket for ticket, stage, _seq in lifecycle if stage == "started"] == [1, 2, 3, 4]
    assert execution_order == [1, 2, 3, 4]


@pytest.mark.parametrize("invalid_capacity", [None, 0, -1, 4097])
def test_queue_capacity_rejects_unbounded_nonpositive_and_silent_clamping(
    invalid_capacity: int | None,
) -> None:
    """queue capacity 必须是 1..4096 的显式整数；None/无界/负数/静默 clamp 全部拒绝。"""
    module = _coordinator_module()
    database = _FakeDatabase()
    with pytest.raises(module.InvalidQueueCapacityError) as caught:
        module.WriteCoordinator(
            database=database,
            mutex=_FakeMutex(database),
            queue_capacity=invalid_capacity,
        )
    assert caught.value.error_code == "INVALID_WRITE_QUEUE_CAPACITY"
    assert module.MAX_QUEUE_CAPACITY == 4096


@pytest.mark.parametrize("capacity", [1, 17, 4096])
def test_valid_queue_capacity_is_preserved_exactly_without_clamp(capacity: int) -> None:
    """合法容量按请求值原样保存，不偷偷换成默认值。"""
    stack = _new_stack(queue_capacity=capacity)
    assert stack.coordinator.queue_capacity == capacity


def test_close_racing_start_never_joins_unstarted_owner_thread() -> None:
    """start 暴露 STARTING 前必须已调用 Thread.start，避免并发 close join 未启动线程。"""
    stack = _new_stack()
    real_thread_start = stack.coordinator.owner_thread.start
    start_called = threading.Event()
    errors: list[BaseException] = []

    def delayed_thread_start() -> None:
        start_called.set()
        time.sleep(0.05)
        real_thread_start()

    stack.coordinator.owner_thread.start = delayed_thread_start  # type: ignore[method-assign]

    def run_start() -> None:
        try:
            asyncio.run(_bounded(stack.coordinator.start(), timeout=3.0))
        except BaseException as exc:  # noqa: BLE001 - 竞态中 start 可被 close 稳定拒绝。
            errors.append(exc)

    starter = threading.Thread(target=run_start, daemon=False)
    starter.start()
    assert start_called.wait(timeout=2)
    try:
        asyncio.run(_bounded(stack.coordinator.close(), timeout=3.0))
    finally:
        starter.join(timeout=3)
    assert not starter.is_alive()
    assert not stack.coordinator.owner_thread.is_alive()
    assert not any(
        isinstance(exc, RuntimeError) and "cannot join thread before it is started" in str(exc) for exc in errors
    )
    assert stack.coordinator.state.name == "CLOSED"


@pytest.mark.asyncio
async def test_thread_start_failure_is_closed_and_close_never_joins_unstarted_thread() -> None:
    """Thread.start 自身失败时不得留下 STARTING/CLOSING，也不得 join 未启动线程。"""
    stack = _new_stack()

    def fail_to_start() -> None:
        raise RuntimeError("cannot start new thread")

    stack.coordinator.owner_thread.start = fail_to_start  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="cannot start new thread"):
        await _bounded(stack.coordinator.start())
    assert stack.coordinator.state.name == "CLOSED"
    await _bounded(stack.coordinator.close())
    assert stack.coordinator.state.name == "CLOSED"


@pytest.mark.asyncio
async def test_close_after_startup_dependency_failure_reaches_closed_state() -> None:
    """mutex/database 启动失败后 close 仍要给诊断者稳定 CLOSED 终态。"""
    module = _coordinator_module()

    class AcquireError(Exception):
        error_code = "SINGLETON_MUTEX_TIMEOUT"

    class FailingMutex:
        def acquire(self) -> object:
            raise AcquireError("injected-acquire-timeout")

    coordinator = module.WriteCoordinator(database=_FakeDatabase(), mutex=FailingMutex(), queue_capacity=1)
    with pytest.raises(AcquireError):
        await _bounded(coordinator.start())
    assert coordinator.state.name == "FAILED"
    await _bounded(coordinator.close())
    assert coordinator.state.name == "CLOSED"
    await _bounded(coordinator.close())
    assert coordinator.state.name == "CLOSED"


@pytest.mark.asyncio
async def test_close_overlapping_startup_dependency_failure_returns_closed_state() -> None:
    """close 与启动依赖失败重叠时，owner failure 不能把收尾状态覆盖回 FAILED。"""
    module = _coordinator_module()
    entered = threading.Event()
    release = threading.Event()

    class AcquireError(Exception):
        error_code = "SINGLETON_MUTEX_TIMEOUT"

    class BlockingFailingMutex:
        def acquire(self) -> object:
            entered.set()
            assert release.wait(timeout=2)
            raise AcquireError("injected-overlap-timeout")

    coordinator = module.WriteCoordinator(database=_FakeDatabase(), mutex=BlockingFailingMutex(), queue_capacity=1)
    start_task = asyncio.create_task(coordinator.start())
    await _wait_thread_event(entered)
    close_task = asyncio.create_task(coordinator.close())
    await _wait_for_lifecycle(coordinator, lambda _events: coordinator.state.name == "CLOSING")
    release.set()
    await _bounded(close_task)
    with pytest.raises(AcquireError):
        await _bounded(start_task)
    assert coordinator.state.name == "CLOSED"
    assert not coordinator.owner_thread.is_alive()


@pytest.mark.asyncio
async def test_async_transaction_callback_is_rejected_and_rolled_back() -> None:
    """事务 callback 必须同步；返回 awaitable 会跨越事务边界，必须稳定拒绝。"""
    module = _coordinator_module()
    stack = _new_stack()

    async def invalid_callback(_uow: _FakeUnitOfWork) -> str:
        return "must-not-await"

    await _bounded(stack.coordinator.start())
    try:
        with pytest.raises(module.AsyncTransactionCommandError) as caught:
            await _bounded(
                stack.coordinator.execute(
                    operation="invalid-async-command",
                    context={"correlation_id": "corr-async-invalid"},
                    command=invalid_callback,
                )
            )
        assert caught.value.error_code == "ASYNC_TRANSACTION_COMMAND_REJECTED"
    finally:
        await _bounded(stack.coordinator.close())
    assert [event for event, _thread in stack.database.trace].count("ROLLBACK") == 1


def test_owner_thread_reentrant_execute_is_rejected_without_deadlock() -> None:
    """callback 从 owner thread 再 submit 会自死锁，必须在排队前拒绝。"""
    receipt = _reentrant_child_receipt()
    assert receipt["status"] == "rejected"
    assert receipt["errorCode"] == "REENTRANT_WRITE_REJECTED"
    trace = receipt["trace"]
    assert isinstance(trace, list)
    assert trace.count("BEGIN IMMEDIATE") == 1
    assert trace.count("ROLLBACK") == 1


@pytest.mark.asyncio
async def test_n12_submit_after_close_is_stably_rejected() -> None:
    """close 原子拒绝新准入；不得悄悄重启 worker。"""
    module = _coordinator_module()
    stack = _new_stack()
    await _bounded(stack.coordinator.start())
    await _bounded(stack.coordinator.close())

    with pytest.raises(module.CoordinatorClosedError) as caught:
        await _bounded(
            stack.coordinator.execute(
                operation="after-close",
                context={"correlation_id": "corr-after-close"},
                command=lambda _uow: None,
            )
        )
    assert caught.value.error_code == "WRITE_COORDINATOR_CLOSED"


@pytest.mark.asyncio
async def test_close_rejects_new_work_drains_fifo_then_closes_db_before_mutex() -> None:
    """CLOSING 顺序必须是拒新→drain→DB close→mutex release/handle close。"""
    module = _coordinator_module()
    stack = _new_stack()
    entered = threading.Event()
    release = threading.Event()

    def first(_uow: _FakeUnitOfWork) -> str:
        entered.set()
        assert release.wait(timeout=2)
        return "first"

    await _bounded(stack.coordinator.start())
    first_future = asyncio.create_task(
        stack.coordinator.execute(
            operation="close-first",
            context={"correlation_id": "corr-close-first"},
            command=first,
        )
    )
    await _wait_thread_event(entered)
    second_future = asyncio.create_task(
        stack.coordinator.execute(
            operation="close-second",
            context={"correlation_id": "corr-close-second"},
            command=lambda _uow: "second",
        )
    )
    await asyncio.sleep(0)
    close_future = asyncio.create_task(stack.coordinator.close())
    await asyncio.sleep(0.05)
    with pytest.raises(module.CoordinatorClosedError):
        await _bounded(
            stack.coordinator.execute(
                operation="close-rejected",
                context={"correlation_id": "corr-close-rejected"},
                command=lambda _uow: None,
            )
        )
    release.set()
    assert await _bounded(first_future) == "first"
    assert await _bounded(second_future) == "second"
    await _bounded(close_future)
    assert stack.coordinator.state.name == "CLOSED"
    assert not stack.coordinator.owner_thread.is_alive()
    assert stack.database.trace[-1][0] == "DATABASE_CLOSE"
    assert [event for event, _thread in stack.mutex.trace] == [
        "MUTEX_ACQUIRE",
        "MUTEX_RELEASE",
        "MUTEX_CLOSE",
    ]
    assert stack.database.lifecycle[-3:] == ["DATABASE_CLOSE", "MUTEX_RELEASE", "MUTEX_CLOSE"]
    assert stack.mutex.lease is not None
    assert stack.mutex.lease.released is True
    assert stack.mutex.lease.handle_closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_stage", "error_code"),
    [
        ("database_close", "SQLITE_CLOSE_FAILED"),
        ("mutex_release", "SINGLETON_MUTEX_RELEASE_FAILED"),
        ("handle_close", "SINGLETON_MUTEX_CLOSE_FAILED"),
    ],
)
async def test_close_failures_still_attempt_db_release_handle_in_exact_order(
    failure_stage: str,
    error_code: str,
) -> None:
    """close 失败 fail closed：未确认 DB 关闭时不 ReleaseMutex，但始终 CloseHandle。"""
    module = _coordinator_module()
    stack = _new_stack()
    if failure_stage == "database_close":
        stack.database.close_error = sqlite3.OperationalError("secret-db-close")
    elif failure_stage == "mutex_release":
        stack.mutex.release_error = OSError("secret-mutex-release")
    else:
        stack.mutex.close_error = OSError("secret-handle-close")

    await _bounded(stack.coordinator.start())
    with pytest.raises(module.CoordinatorCloseError) as caught:
        await _bounded(stack.coordinator.close())
    assert caught.value.error_code == error_code
    assert stack.coordinator.state.name == "FAILED"
    assert not stack.coordinator.owner_thread.is_alive()
    if failure_stage == "database_close":
        assert stack.database.closed is False
        assert stack.database.lifecycle[-2:] == ["DATABASE_CLOSE", "MUTEX_CLOSE"]
        assert [event for event, _thread in stack.mutex.trace] == ["MUTEX_ACQUIRE", "MUTEX_CLOSE"]
        assert stack.mutex.lease is not None and stack.mutex.lease.released is False
    else:
        assert stack.database.closed is True
        assert stack.database.lifecycle[-3:] == ["DATABASE_CLOSE", "MUTEX_RELEASE", "MUTEX_CLOSE"]
    assert stack.mutex.lease is not None and stack.mutex.lease.handle_closed is True
    assert stack.mutex.lease.released is (failure_stage == "handle_close")


@pytest.mark.asyncio
async def test_c07_close_enqueue_race_has_no_lost_future_or_double_execution() -> None:
    """close/enqueue 竞态中，每项只会执行一次或收到 closed，不得悬挂。"""
    module = _coordinator_module()
    stack = _new_stack(queue_capacity=64)
    executed: list[int] = []
    await _bounded(stack.coordinator.start())

    async def submit(index: int) -> tuple[str, int]:
        try:
            result = await _bounded(
                stack.coordinator.execute(
                    operation="close-race",
                    context={"correlation_id": f"corr-race-{index}"},
                    command=lambda _uow: executed.append(index) or index,
                )
            )
            return "executed", result
        except module.CoordinatorClosedError:
            return "closed", index

    submissions = [asyncio.create_task(submit(index)) for index in range(40)]
    await asyncio.sleep(0)
    close_task = asyncio.create_task(stack.coordinator.close())
    results = await _bounded(asyncio.gather(*submissions))
    await _bounded(close_task)

    assert len(results) == 40
    assert len(executed) == len(set(executed))
    assert {index for _status, index in results} == set(range(40))
    executed_receipts = {index for status, index in results if status == "executed"}
    closed_receipts = {index for status, index in results if status == "closed"}
    assert set(executed) == executed_receipts
    assert executed_receipts.isdisjoint(closed_receipts)
    assert executed_receipts | closed_receipts == set(range(40))
    assert not stack.coordinator.owner_thread.is_alive()


@pytest.mark.asyncio
async def test_f05_begin_failure_rolls_back_nothing_and_keeps_future_bounded() -> None:
    """BEGIN 失败不得运行 callback/ROLLBACK，且连接仍可恢复处理下一 ticket。"""
    module = _coordinator_module()
    stack = _new_stack()
    stack.database.begin_error = sqlite3.OperationalError("injected-begin")
    callback_calls = 0

    def command(_uow: _FakeUnitOfWork) -> None:
        nonlocal callback_calls
        callback_calls += 1

    await _bounded(stack.coordinator.start())
    try:
        with pytest.raises(module.TransactionBeginError) as caught:
            await _bounded(
                stack.coordinator.execute(
                    operation="begin-failure",
                    context={"correlation_id": "corr-begin-failure"},
                    command=command,
                )
            )
        assert caught.value.error_code == "SQLITE_BEGIN_FAILED"
        assert stack.coordinator.state.name == "READY"
        assert stack.database.poison_calls == 0
        stack.database.begin_error = None
        assert (
            await _bounded(
                stack.coordinator.execute(
                    operation="after-begin-failure",
                    context={"correlation_id": "corr-after-begin-failure"},
                    command=lambda _uow: "recovered",
                )
            )
            == "recovered"
        )
    finally:
        await _bounded(stack.coordinator.close())
    assert callback_calls == 0
    assert "ROLLBACK" not in [event for event, _thread in stack.database.trace]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["state_write", "event_write", "precommit"])
async def test_state_event_or_precommit_failure_rolls_back_and_writer_remains_ready(
    failure_stage: str,
) -> None:
    """COMMIT 前 state/event/precommit 任一点失败只回滚当前 ticket，不 poison owner 连接。"""
    stack = _new_stack()
    if failure_stage == "precommit":
        stack.database.precommit_error = ValueError("injected-precommit")

    def failing_command(_uow: _FakeUnitOfWork) -> None:
        if failure_stage in {"state_write", "event_write"}:
            raise ValueError(f"injected-{failure_stage}")

    await _bounded(stack.coordinator.start())
    try:
        with pytest.raises(ValueError, match="injected"):
            await _bounded(
                stack.coordinator.execute(
                    operation=f"recoverable-{failure_stage}",
                    context={"correlation_id": f"corr-{failure_stage}"},
                    command=failing_command,
                )
            )
        assert stack.coordinator.state.name == "READY"
        assert stack.database.poison_calls == 0
        assert [event for event, _thread in stack.database.trace].count("ROLLBACK") == 1
        stack.database.precommit_error = None
        assert (
            await _bounded(
                stack.coordinator.execute(
                    operation=f"after-{failure_stage}",
                    context={"correlation_id": f"corr-after-{failure_stage}"},
                    command=lambda _uow: "ready",
                )
            )
            == "ready"
        )
    finally:
        await _bounded(stack.coordinator.close())


@pytest.mark.asyncio
async def test_f09_commit_outcome_unknown_poisons_writer_and_never_retries() -> None:
    """COMMIT ack 不明时只能 poison；不得声称 rollback 或自动重派 callback。"""
    module = _coordinator_module()
    stack = _new_stack()
    stack.database.commit_error = sqlite3.OperationalError("injected-commit-ack-loss")
    callback_calls = 0

    def command(_uow: _FakeUnitOfWork) -> str:
        nonlocal callback_calls
        callback_calls += 1
        return "possibly-committed"

    await _bounded(stack.coordinator.start())
    with pytest.raises(module.CommitOutcomeUnknownError) as caught:
        await _bounded(
            stack.coordinator.execute(
                operation="commit-unknown",
                context={"correlation_id": "corr-commit-unknown"},
                command=command,
            )
        )
    assert caught.value.error_code == "SQLITE_COMMIT_OUTCOME_UNKNOWN"
    assert callback_calls == 1
    assert stack.coordinator.state.name == "FAILED"
    lifecycle = tuple(stack.coordinator.ticket_lifecycle_snapshot())
    assert [stage for ticket, stage, _seq in lifecycle if ticket == 1] == [
        "admission_attempted",
        "admitted",
        "dequeued",
        "started",
        "settled",
    ]
    assert stack.database.poison_calls == 1
    assert "ROLLBACK" not in [event for event, _thread in stack.database.trace]
    with pytest.raises(module.CoordinatorFailedError):
        await _bounded(
            stack.coordinator.execute(
                operation="must-not-retry",
                context={"correlation_id": "corr-no-retry"},
                command=command,
            )
        )
    await _bounded(stack.coordinator.close())
    assert callback_calls == 1


@pytest.mark.asyncio
async def test_commit_success_never_rolls_back_when_logging_renderer_fails() -> None:
    """COMMIT 成功后日志链失败只能丢弃日志，不能回到 rollback 分支或污染调用方结果。"""
    structlog = importlib.import_module("structlog")
    previous = dict(structlog.get_config())

    def exploding_processor(_logger: object, _method_name: str, _event_dict: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("renderer-down-after-commit")

    structlog.configure(
        processors=(exploding_processor,),
        context_class=dict,
        logger_factory=structlog.ReturnLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(0),
        cache_logger_on_first_use=False,
    )
    stack = _new_stack()
    try:
        await _bounded(stack.coordinator.start())
        assert (
            await _bounded(
                stack.coordinator.execute(
                    operation="commit-log-failure",
                    context={"correlation_id": "corr-commit-log-failure"},
                    command=lambda _uow: "committed",
                )
            )
            == "committed"
        )
    finally:
        try:
            await _bounded(stack.coordinator.close())
        finally:
            structlog.configure(**previous)
    events = [event for event, _thread in stack.database.trace]
    assert events.count("COMMIT") == 1
    assert "ROLLBACK" not in events
    assert stack.database.poison_calls == 0
    assert stack.coordinator.state.name == "CLOSED"
    lifecycle = tuple(stack.coordinator.ticket_lifecycle_snapshot())
    assert [stage for ticket, stage, _seq in lifecycle if ticket == 1].count("settled") == 1


@pytest.mark.asyncio
async def test_commit_success_never_rolls_back_when_post_commit_clock_fails() -> None:
    """COMMIT 成功后 committed timestamp 采样失败也不能回到 rollback 分支。"""
    clock = _ScriptedMonotonic()
    stack = _new_stack(monotonic_ns=clock)
    await _bounded(stack.coordinator.start())
    clock.reset([1_000_000_000, 1_002_000_000, 1_009_000_000])
    try:
        assert (
            await _bounded(
                stack.coordinator.execute(
                    operation="commit-clock-failure",
                    context={"correlation_id": "corr-commit-clock-failure"},
                    command=lambda _uow: "committed",
                )
            )
            == "committed"
        )
    finally:
        clock.stop()
        await _bounded(stack.coordinator.close())
    events = [event for event, _thread in stack.database.trace]
    assert events.count("COMMIT") == 1
    assert "ROLLBACK" not in events
    assert stack.database.poison_calls == 0
    assert stack.coordinator.state.name == "CLOSED"
    assert clock.calls == 3
    lifecycle = tuple(stack.coordinator.ticket_lifecycle_snapshot())
    assert [stage for ticket, stage, _seq in lifecycle if ticket == 1].count("settled") == 1


@pytest.mark.asyncio
async def test_f10_rollback_failure_poisons_writer() -> None:
    """callback 失败后的 ROLLBACK 若也失败，所有后续写必须拒绝。"""
    module = _coordinator_module()
    stack = _new_stack()
    stack.database.rollback_error = sqlite3.OperationalError("injected-rollback")
    await _bounded(stack.coordinator.start())

    def failing(_uow: _FakeUnitOfWork) -> None:
        raise ValueError("callback-failure")

    with pytest.raises(module.RollbackFailedError) as caught:
        await _bounded(
            stack.coordinator.execute(
                operation="rollback-failure",
                context={"correlation_id": "corr-rollback-failure"},
                command=failing,
            )
        )
    assert caught.value.error_code == "SQLITE_ROLLBACK_FAILED"
    assert stack.coordinator.state.name == "FAILED"
    lifecycle = tuple(stack.coordinator.ticket_lifecycle_snapshot())
    assert [stage for ticket, stage, _seq in lifecycle if ticket == 1] == [
        "admission_attempted",
        "admitted",
        "dequeued",
        "started",
        "settled",
    ]
    assert stack.database.poison_calls == 1
    await _bounded(stack.coordinator.close())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_stage", "error_type", "error_code"),
    [
        ("commit", "CommitOutcomeUnknownError", "SQLITE_COMMIT_OUTCOME_UNKNOWN"),
        ("rollback", "RollbackFailedError", "SQLITE_ROLLBACK_FAILED"),
        ("fatal", "CoordinatorFatalError", "WRITE_COORDINATOR_FATAL"),
    ],
)
async def test_poisoning_failure_settles_pre_admitted_pending_with_same_cause_once(
    failure_stage: str,
    error_type: str,
    error_code: str,
) -> None:
    """commit/rollback/fatal 均 poison 一次，并以同因闭合 active 与两个 pending。"""
    module = _coordinator_module()
    structlog = importlib.import_module("structlog")
    logging_module = importlib.import_module("factory_agent.observability.logging")
    capture = structlog.testing.LogCapture()
    previous_logging_config = dict(structlog.get_config())
    processors = logging_module.structlog_processor_chain(renderer=capture)
    assert tuple(processors)[-1] is capture
    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.ReturnLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(0),
        cache_logger_on_first_use=False,
    )
    stack = _new_stack(queue_capacity=4)
    if failure_stage == "commit":
        stack.database.commit_error = sqlite3.OperationalError("injected-commit")
    elif failure_stage == "rollback":
        stack.database.rollback_error = sqlite3.OperationalError("injected-rollback")
    entered = threading.Event()
    release = threading.Event()
    callback_counts = [0, 0, 0]

    def active_command(_uow: _FakeUnitOfWork) -> str:
        callback_counts[0] += 1
        entered.set()
        assert release.wait(timeout=2)
        if failure_stage == "rollback":
            raise ValueError("trigger-rollback")
        if failure_stage == "fatal":
            raise _FatalWriterFault("fatal-owner")
        return "commit-attempt"

    def pending_command(index: int) -> Callable[[_FakeUnitOfWork], int]:
        def command(_uow: _FakeUnitOfWork) -> int:
            callback_counts[index] += 1
            return index

        return command

    operations = {
        0: f"poison-{failure_stage}-active",
        1: f"poison-{failure_stage}-pending-1",
        2: f"poison-{failure_stage}-pending-2",
    }
    correlations = {
        0: f"corr-{failure_stage}-active",
        1: f"corr-{failure_stage}-pending-1",
        2: f"corr-{failure_stage}-pending-2",
    }
    try:
        await _bounded(stack.coordinator.start())
        futures = [
            asyncio.create_task(
                stack.coordinator.execute(
                    operation=operations[0],
                    context=_secret_context(correlations[0]),
                    command=active_command,
                )
            )
        ]
        await _wait_thread_event(entered)
        futures.extend(
            asyncio.create_task(
                stack.coordinator.execute(
                    operation=operations[index],
                    context=_secret_context(correlations[index]),
                    command=pending_command(index),
                )
            )
            for index in (1, 2)
        )
        await _wait_for_lifecycle(
            stack.coordinator,
            lambda events: {(ticket, stage) for ticket, stage, _seq in events} >= {(2, "admitted"), (3, "admitted")},
        )
    except BaseException:
        release.set()
        try:
            await _bounded(stack.coordinator.close())
        finally:
            structlog.configure(**previous_logging_config)
        raise
    release.set()
    try:
        results = await _bounded(asyncio.gather(*futures, return_exceptions=True))
        expected_type = getattr(module, error_type)
        assert all(isinstance(result, expected_type) for result in results)
        assert {result.error_code for result in results} == {error_code}
        fingerprints = {
            (type(result).__name__, result.error_code, str(result), type(result.__cause__).__name__)
            for result in results
        }
        assert len(fingerprints) == 1
        assert callback_counts == [1, 0, 0]
        assert stack.database.poison_calls == 1
        assert stack.coordinator.state.name == "FAILED"
        lifecycle = tuple(stack.coordinator.ticket_lifecycle_snapshot())
        assert [stage for ticket, stage, _seq in lifecycle if ticket == 1] == [
            "admission_attempted",
            "admitted",
            "dequeued",
            "started",
            "settled",
        ]
        for ticket in (2, 3):
            assert [stage for actual, stage, _seq in lifecycle if actual == ticket] == [
                "admission_attempted",
                "admitted",
                "settled",
            ]
        assert [sequence for _ticket, _stage, sequence in lifecycle] == list(range(1, len(lifecycle) + 1))
    finally:
        release.set()
        try:
            await _bounded(stack.coordinator.close())
        finally:
            structlog.configure(**previous_logging_config)
    assert not stack.coordinator.owner_thread.is_alive()
    captured = list(capture.entries)
    for index in range(3):
        ticket_records = [record for record in captured if record.get("correlation_id") == correlations[index]]
        assert ticket_records
        assert {record.get("operation") for record in ticket_records} == {operations[index]}
        assert {record.get("task_id") for record in ticket_records} == {"task-log-sensitive"}
        assert {record.get("run_id") for record in ticket_records} == {"run-log-sensitive"}
        failure_records = [record for record in ticket_records if record.get("status") == "failure"]
        assert failure_records
        assert {record.get("error_code") for record in failure_records} == {error_code}
        assert not any(record.get("status") == "success" for record in ticket_records)
        assert not any(record.get("event") == "sqlite_commit" for record in ticket_records)
        for record in ticket_records:
            _assert_log_record_shape(record)
    _assert_no_secret_shape(json.dumps(captured, ensure_ascii=False, sort_keys=True))


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_interrupt_started_transaction() -> None:
    """caller 取消 future 不得跨线程中断已经 BEGIN 的 SQLite 事务。"""
    stack = _new_stack()
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def command(_uow: _FakeUnitOfWork) -> str:
        entered.set()
        assert release.wait(timeout=2)
        completed.set()
        return "committed"

    await _bounded(stack.coordinator.start())
    task = asyncio.create_task(
        stack.coordinator.execute(
            operation="cancel-started",
            context={"correlation_id": "corr-cancel-started"},
            command=command,
        )
    )
    await _wait_thread_event(entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await _bounded(task)
    release.set()
    await _wait_thread_event(completed)
    await _bounded(stack.coordinator.close())
    assert "COMMIT" in [event for event, _thread in stack.database.trace]


LOG_FIELDS = {
    "event",
    "operation",
    "correlation_id",
    "task_id",
    "run_id",
    "queue_depth",
    "writer_queue_ms",
    "sqlite_commit_ms",
    "duration_ms",
    "status",
    "error_code",
}
TIMING_FIELDS = {"writer_queue_ms", "sqlite_commit_ms", "duration_ms"}
SECRET_VALUES = (
    "Bearer eyJhbGciOiJIUzI1NiJ9.secret.signature",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature",
    "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
    "correct-horse-battery-staple-password",
)


def _secret_context(correlation_id: str) -> dict[str, object]:
    """capture 与真实 renderer 共用同一嵌套敏感语料，禁止两套降级 fixture。"""
    return {
        "correlation_id": correlation_id,
        "task_id": "task-log-sensitive",
        "run_id": "run-log-sensitive",
        "payload": {"authorization": SECRET_VALUES[0], "pat": SECRET_VALUES[1]},
        "sql": {"parameters": [SECRET_VALUES[2]]},
        "operation": {"nested_jwt": SECRET_VALUES[3]},
        "context": {"pem": SECRET_VALUES[4]},
        "exception": {"message": SECRET_VALUES[0]},
    }


def _assert_log_record_shape(record: dict[str, object]) -> None:
    """核对稳定字段、类型和 finite/nonnegative timing。"""
    assert LOG_FIELDS <= set(record)
    assert isinstance(record["event"], str)
    for field in ("operation", "correlation_id", "task_id", "run_id"):
        assert isinstance(record[field], str) and record[field]
    assert isinstance(record["queue_depth"], int) and record["queue_depth"] >= 0
    assert isinstance(record["status"], str)
    assert record["error_code"] is None or isinstance(record["error_code"], str)
    for field in TIMING_FIELDS:
        value = record[field]
        assert isinstance(value, (int, float)) and not isinstance(value, bool)
        assert math.isfinite(value) and value >= 0


def _assert_no_secret_shape(serialized: str) -> None:
    """对 Bearer/PAT/AWS/JWT/PEM 做值和模式双重零命中。"""
    for secret in SECRET_VALUES:
        assert secret not in serialized
    patterns = (
        r"Bearer\s+[A-Za-z0-9._-]+",
        r"ghp_[A-Za-z0-9]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    )
    assert not [pattern for pattern in patterns if re.search(pattern, serialized)]


def test_production_logging_chain_directly_redacts_recursive_event_payload() -> None:
    """直接经生产 processor chain 记录事件，证明嵌套 dict/list 叶值脱敏且 benign 数据保留。"""
    structlog = importlib.import_module("structlog")
    logging_module = importlib.import_module("factory_agent.observability.logging")
    capture = structlog.testing.LogCapture()
    previous = dict(structlog.get_config())
    processors = logging_module.structlog_processor_chain(renderer=capture)
    assert tuple(processors)[-1] is capture
    benign_sentinel = "benign-fourth-round-sentinel"
    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.ReturnLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(0),
        cache_logger_on_first_use=False,
    )
    try:
        structlog.get_logger("direct-redaction-probe").info(
            "direct_redaction_probe",
            benign=benign_sentinel,
            nested={
                "safe": {"sentinel": benign_sentinel},
                "credentials": {
                    "token": SECRET_VALUES[1],
                    "password": SECRET_VALUES[5],
                    "authorization": SECRET_VALUES[0],
                },
                "sequence": [
                    {"safe": benign_sentinel},
                    {"private_key": SECRET_VALUES[4]},
                    {"jwt": SECRET_VALUES[3]},
                    {"aws_access_key": SECRET_VALUES[2]},
                ],
                "mapping_key_probe": {SECRET_VALUES[1]: "secret-as-key", "plain-key": benign_sentinel},
                "set_probe": {SECRET_VALUES[1], benign_sentinel},
            },
        )
    finally:
        structlog.configure(**previous)

    assert len(capture.entries) == 1
    record = capture.entries[0]
    assert record["event"] == "direct_redaction_probe"
    assert record["benign"] == benign_sentinel
    assert record["nested"]["safe"]["sentinel"] == benign_sentinel
    assert record["nested"]["sequence"][0]["safe"] == benign_sentinel
    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)
    assert benign_sentinel in serialized
    _assert_no_secret_shape(serialized)


@pytest.mark.asyncio
async def test_default_structlog_setup_does_not_override_host_configuration() -> None:
    """宿主已显式配置 structlog 时，coordinator 不得全局替换 processor/logger factory。"""
    structlog = importlib.import_module("structlog")
    capture = structlog.testing.LogCapture()
    previous = dict(structlog.get_config())

    def host_processor(_logger: object, _method_name: str, event_dict: dict[str, object]) -> dict[str, object]:
        event_dict["host_processor_marker"] = "preserved"
        return event_dict

    structlog.configure(
        processors=(host_processor, capture),
        context_class=dict,
        logger_factory=structlog.ReturnLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(0),
        cache_logger_on_first_use=False,
    )
    stack = _new_stack()
    try:
        await _bounded(stack.coordinator.start())
        await _bounded(
            stack.coordinator.execute(
                operation="host-structlog",
                context={"correlation_id": "corr-host-structlog"},
                command=lambda _uow: "ok",
            )
        )
    finally:
        try:
            await _bounded(stack.coordinator.close())
        finally:
            config_after = dict(structlog.get_config())
            structlog.configure(**previous)
    assert tuple(config_after["processors"]) == (host_processor, capture)
    assert capture.entries
    assert {entry.get("host_processor_marker") for entry in capture.entries} == {"preserved"}
    assert {entry.get("operation") for entry in capture.entries} == {"host-structlog"}


@pytest.mark.asyncio
async def test_capture_logs_redacts_nested_secrets_and_failure_never_emits_success() -> None:
    """嵌套 payload/sql/operation/context/exception 全脱敏，失败 ticket 后无 success/commit。"""
    structlog = importlib.import_module("structlog")
    logging_module = importlib.import_module("factory_agent.observability.logging")
    nested_context = _secret_context("corr-log-failure")
    capture = structlog.testing.LogCapture()
    previous = dict(structlog.get_config())
    processors = logging_module.structlog_processor_chain(renderer=capture)
    assert tuple(processors)[-1] is capture
    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.ReturnLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(0),
        cache_logger_on_first_use=False,
    )
    stack = _new_stack()
    try:
        await _bounded(stack.coordinator.start())
        try:
            with pytest.raises(ValueError, match="injected-sensitive-failure"):
                await _bounded(
                    stack.coordinator.execute(
                        operation="logged-failure",
                        context=nested_context,
                        command=lambda _uow: (_ for _ in ()).throw(
                            ValueError(f"injected-sensitive-failure {SECRET_VALUES[0]}")
                        ),
                    )
                )
        finally:
            await _bounded(stack.coordinator.close())
    finally:
        structlog.configure(**previous)

    captured = list(capture.entries)
    serialized = json.dumps(captured, ensure_ascii=False, sort_keys=True)
    _assert_no_secret_shape(serialized)
    ticket_records = [record for record in captured if record.get("correlation_id") == "corr-log-failure"]
    assert ticket_records
    assert any(record.get("status") == "failure" for record in ticket_records)
    assert not any(record.get("status") == "success" for record in ticket_records)
    assert not any(record.get("event") == "sqlite_commit" for record in ticket_records)
    for record in ticket_records:
        assert record["operation"] == "logged-failure"
        assert record["correlation_id"] == "corr-log-failure"
        assert record["task_id"] == "task-log-sensitive"
        assert record["run_id"] == "run-log-sensitive"
        _assert_log_record_shape(record)


@pytest.mark.asyncio
async def test_real_json_renderer_emits_exact_typed_transaction_fields() -> None:
    """生产递归脱敏链+JSONRenderer 使用同一 secret corpus 与确定性 monotonic delta。"""
    structlog = importlib.import_module("structlog")
    logging_module = importlib.import_module("factory_agent.observability.logging")
    previous = dict(structlog.get_config())
    stream = io.StringIO()
    renderer = structlog.processors.JSONRenderer(sort_keys=True)
    processors = logging_module.structlog_processor_chain(renderer=renderer)
    assert tuple(processors)[-1] is renderer
    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        wrapper_class=structlog.make_filtering_bound_logger(0),
        cache_logger_on_first_use=False,
    )
    try:
        clock = _ScriptedMonotonic()
        stack = _new_stack(monotonic_ns=clock)
        await _bounded(stack.coordinator.start())
        clock.reset([1_000_000_000, 1_002_000_000, 1_009_000_000, 1_020_000_000])
        try:
            await _bounded(
                stack.coordinator.execute(
                    operation="json-rendered-write",
                    context=_secret_context("corr-json-rendered"),
                    command=lambda _uow: "ok",
                )
            )
            assert clock.calls == 4
        finally:
            clock.stop()
            await _bounded(stack.coordinator.close())
    finally:
        structlog.configure(**previous)

    rendered = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    commits = [record for record in rendered if record.get("event") == "sqlite_commit"]
    assert len(commits) == 1
    assert set(commits[0]) == LOG_FIELDS
    _assert_log_record_shape(commits[0])
    assert commits[0]["operation"] == "json-rendered-write"
    assert commits[0]["correlation_id"] == "corr-json-rendered"
    assert commits[0]["task_id"] == "task-log-sensitive"
    assert commits[0]["run_id"] == "run-log-sensitive"
    assert commits[0]["status"] == "success"
    assert commits[0]["error_code"] is None
    assert commits[0]["writer_queue_ms"] == 2.0
    assert commits[0]["sqlite_commit_ms"] == 11.0
    assert commits[0]["duration_ms"] == 20.0
    _assert_no_secret_shape(stream.getvalue())
