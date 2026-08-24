"""SQLite 单写者 coordinator。

Phase 1 要求所有 SQLite 写入经过唯一 owner thread：调用方只把同步
transaction callback 投递到 FIFO 队列，owner thread 按固定顺序执行
``BEGIN IMMEDIATE -> callback -> precommit -> COMMIT``。这样可以把连接
线程亲和性、mutex 生命周期、commit outcome unknown 与 caller cancellation
都压缩到一个可审计边界内。
"""

from __future__ import annotations

import asyncio
import inspect
import queue
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, cast

MAX_QUEUE_CAPACITY = 4096


class _CoordinatorError(Exception):
    """所有 coordinator 稳定错误码的基类。"""

    error_code = "WRITE_COORDINATOR_ERROR"

    def __init__(self, message: str | None = None, *, cause: BaseException | None = None) -> None:
        super().__init__(message or self.error_code)
        if cause is not None:
            self.__cause__ = cause


class InvalidQueueCapacityError(_CoordinatorError):
    error_code = "INVALID_WRITE_QUEUE_CAPACITY"


class CoordinatorClosedError(_CoordinatorError):
    error_code = "WRITE_COORDINATOR_CLOSED"


class ReentrantWriteError(_CoordinatorError):
    error_code = "REENTRANT_WRITE_REJECTED"


class AsyncTransactionCommandError(_CoordinatorError):
    error_code = "ASYNC_TRANSACTION_COMMAND_REJECTED"


class TransactionBeginError(_CoordinatorError):
    error_code = "SQLITE_BEGIN_FAILED"


class CommitOutcomeUnknownError(_CoordinatorError):
    error_code = "SQLITE_COMMIT_OUTCOME_UNKNOWN"


class RollbackFailedError(_CoordinatorError):
    error_code = "SQLITE_ROLLBACK_FAILED"


class CoordinatorFatalError(_CoordinatorError):
    error_code = "WRITE_COORDINATOR_FATAL"


class CoordinatorFailedError(_CoordinatorError):
    error_code = "WRITE_COORDINATOR_FAILED"


class CoordinatorDependencyError(_CoordinatorError):
    """把 mutex/storage 启动错误映射为可传播给待处理 ticket 的 coordinator 错误。"""

    def __init__(self, error_code: str, *, cause: BaseException) -> None:
        self.error_code = error_code
        super().__init__(error_code, cause=cause)


class CoordinatorCloseError(_CoordinatorError):
    """关闭阶段失败；error_code 会按具体清理阶段覆盖。"""

    def __init__(self, error_code: str, *, cause: BaseException | None = None) -> None:
        self.error_code = error_code
        super().__init__(error_code, cause=cause)


class _State(Enum):
    NEW = "NEW"
    STARTING = "STARTING"
    READY = "READY"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


@dataclass(slots=True)
class _Ticket[ResultT]:
    """跨 asyncio loop 与 owner thread 传递的一次写请求。"""

    ticket_id: int
    operation: str
    context: Mapping[str, object]
    command: Callable[[Any], ResultT]
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[ResultT]
    queued_ns: int


@dataclass(slots=True)
class _Lifecycle:
    """记录 ticket 生命周期；sequence 全局单调，便于测试和事故复盘。"""

    values: list[tuple[int, str, int]] = field(default_factory=list)
    sequence: int = 0

    def append(self, ticket_id: int, stage: str) -> None:
        self.sequence += 1
        self.values.append((ticket_id, stage, self.sequence))


@dataclass(slots=True)
class _AdmissionWaiter:
    """等待 queued 容量的 FIFO waiter；未轮到自己时不得抢占后续 slot。"""

    ticket: _Ticket[Any]
    gate: asyncio.Future[None]


class WriteCoordinator:
    """把 SQLite 写事务串行化到唯一 owner thread。

    Args:
        database: 暴露 ``open/new_unit_of_work/poison/close`` 的数据库对象。
        mutex: 暴露 ``acquire`` 的单实例 mutex adapter。
        queue_capacity: 只计算 queued tickets 的有界容量，范围 1..4096。
        monotonic_ns: 测试可注入时钟；生产默认使用 ``time.monotonic_ns``。
    """

    def __init__(
        self,
        *,
        database: object,
        mutex: object,
        queue_capacity: int,
        monotonic_ns: Callable[[], int] | None = None,
        failure_probe: Callable[[str], None] | None = None,
    ) -> None:
        if (
            not isinstance(queue_capacity, int)
            or isinstance(queue_capacity, bool)
            or not 1 <= queue_capacity <= MAX_QUEUE_CAPACITY
        ):
            raise InvalidQueueCapacityError()
        self._database = database
        self._mutex = mutex
        self._queue_capacity = queue_capacity
        self._failure_probe = failure_probe
        self._monotonic_ns = monotonic_ns or time.monotonic_ns
        self._queue: queue.Queue[_Ticket[Any] | None] = queue.Queue()
        self._queued_lock = threading.Lock()
        self._capacity_waiters: deque[_AdmissionWaiter] = deque()
        self._lifecycle_lock = threading.Lock()
        self._lifecycle = _Lifecycle()
        self._ticket_counter = 0
        self._queued_count = 0
        self._state = _State.NEW
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._failure: _CoordinatorError | None = None
        self._lease: object | None = None
        self._readiness: object | None = None
        self._owner_thread_id: int | None = None
        self._thread_started = False
        self.owner_thread = threading.Thread(target=self._owner_main, name="factory-write-coordinator", daemon=False)

    @property
    def queue_capacity(self) -> int:
        return self._queue_capacity

    @property
    def queue_depth(self) -> int:
        with self._queued_lock:
            return self._queued_count

    @property
    def state(self) -> _State:
        return self._state

    def ticket_lifecycle_snapshot(self) -> tuple[tuple[int, str, int], ...]:
        """返回不可变生命周期快照，避免测试/诊断读取时观察半写状态。"""
        with self._lifecycle_lock:
            return tuple(self._lifecycle.values)

    async def start(self) -> object:
        """启动 owner thread，并等待 mutex 与 database 已就绪。"""
        with self._state_lock:
            if self._state is not _State.NEW:
                if self._state is _State.READY:
                    return self._readiness
                if self._state is _State.STARTING:
                    pass
                else:
                    raise CoordinatorClosedError()
            else:
                self._state = _State.STARTING
                # STARTING 对 close() 可见前必须先完成 Thread.start()，否则并发 close()
                # 可能 join 一个尚未启动的 thread 并异常返回。
                try:
                    self.owner_thread.start()
                except BaseException:
                    self._state = _State.CLOSED
                    raise
                self._thread_started = True
        await asyncio.to_thread(self._ready.wait)
        if self._start_error is not None:
            raise self._start_error
        with self._state_lock:
            if self._state is not _State.READY:
                raise CoordinatorClosedError()
        return self._readiness

    async def execute[ResultT](
        self,
        *,
        operation: str,
        context: Mapping[str, object],
        command: Callable[[Any], ResultT],
    ) -> ResultT:
        """提交一次同步写事务并等待结果。

        caller cancellation 只取消调用方 future；owner thread 一旦开始事务仍会
        继续 commit/rollback，避免跨线程中断 SQLite 连接。
        """
        if threading.get_ident() == self._owner_thread_id:
            raise ReentrantWriteError()
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._state is _State.CLOSED or self._state is _State.CLOSING:
                raise CoordinatorClosedError()
            if self._state is _State.FAILED:
                raise CoordinatorFailedError()
            if self._state is not _State.READY:
                raise CoordinatorClosedError()
        ticket_id = self._next_ticket_id()
        self._record(ticket_id, "admission_attempted")
        future: asyncio.Future[ResultT] = loop.create_future()
        ticket = _Ticket(
            ticket_id=ticket_id,
            operation=operation,
            context=context,
            command=command,
            loop=loop,
            future=future,
            queued_ns=self._monotonic_ns(),
        )
        await self._admit(ticket)
        return await future

    async def close(self) -> None:
        """拒绝新写入、drain 已接收队列，并按 DB->mutex->handle 顺序关闭。"""
        waiters: list[_AdmissionWaiter] = []
        close_error = CoordinatorClosedError()
        with self._state_lock:
            if self._state is _State.CLOSED:
                return
            if self._state is _State.NEW:
                self._state = _State.CLOSED
                return
            if self._state is _State.STARTING:
                self._state = _State.CLOSING
            elif self._state in {_State.READY, _State.FAILED}:
                self._state = _State.CLOSING
                waiters = self._fail_capacity_waiters_locked(close_error)
                self._queue.put(None)
        for waiter in waiters:
            self._settle_waiter_exception(waiter, close_error)
        if self._thread_started:
            await asyncio.to_thread(self.owner_thread.join, 5.0)
            if self.owner_thread.is_alive():
                raise CoordinatorCloseError("WRITE_COORDINATOR_CLOSE_TIMEOUT")
        with self._state_lock:
            if not isinstance(self._failure, CoordinatorCloseError):
                self._state = _State.CLOSED
        if isinstance(self._failure, CoordinatorCloseError):
            raise self._failure

    def _next_ticket_id(self) -> int:
        with self._lifecycle_lock:
            self._ticket_counter += 1
            return self._ticket_counter

    def _record(self, ticket_id: int, stage: str) -> None:
        with self._lifecycle_lock:
            self._lifecycle.append(ticket_id, stage)

    async def _admit(self, ticket: _Ticket[Any]) -> None:
        waiter: _AdmissionWaiter | None = None
        with self._state_lock:
            if self._state is _State.CLOSING or self._state is _State.CLOSED:
                self._settle_exception(ticket, CoordinatorClosedError())
                return
            if self._state is _State.FAILED:
                self._settle_exception(ticket, self._failure or CoordinatorFailedError())
                return
            with self._queued_lock:
                if self._queued_count < self._queue_capacity and not self._capacity_waiters:
                    self._enqueue_admitted_locked(ticket)
                    return
                self._record(ticket.ticket_id, "waiting_for_capacity")
                waiter = _AdmissionWaiter(ticket=ticket, gate=ticket.loop.create_future())
                self._capacity_waiters.append(waiter)
        try:
            await waiter.gate
        except asyncio.CancelledError:
            removed = False
            with self._state_lock:
                with self._queued_lock:
                    try:
                        self._capacity_waiters.remove(waiter)
                        removed = True
                    except ValueError:
                        removed = False
            # 如果还在 waiter 队列里，取消代表尚未被接收，直接退出；如果已经被 FIFO
            # 提升入队，owner thread 继续完成事务，调用方取消只是不再等待结果。
            if removed:
                raise
            raise

    def _enqueue_admitted_locked(self, ticket: _Ticket[Any]) -> None:
        """在 state+queued 锁保护下接收 ticket，保证 sentinel/poison 不会插队。"""
        self._queued_count += 1
        self._record(ticket.ticket_id, "admitted")
        self._queue.put(ticket)

    def _release_queued_slot(self) -> None:
        """owner thread 已消费/丢弃 queued ticket 后释放 caller 侧准入容量。"""
        promoted: _AdmissionWaiter | None = None
        with self._state_lock:
            can_promote = self._state is _State.READY
            with self._queued_lock:
                if self._queued_count:
                    self._queued_count -= 1
                if can_promote:
                    promoted = self._promote_next_waiter_locked()
        if promoted is not None:
            self._settle_waiter_result(promoted)

    def _promote_next_waiter_locked(self) -> _AdmissionWaiter | None:
        """严格按等待队列顺序提升一个 waiter；已取消 waiter 不消耗容量。"""
        while self._queued_count < self._queue_capacity and self._capacity_waiters:
            waiter = self._capacity_waiters.popleft()
            if waiter.gate.done():
                continue
            self._enqueue_admitted_locked(waiter.ticket)
            return waiter
        return None

    def _fail_capacity_waiters_locked(self, error: _CoordinatorError) -> list[_AdmissionWaiter]:
        """close/poison 时闭合尚未 admitted 的 waiter，防止容量等待永久悬挂。"""
        failed: list[_AdmissionWaiter] = []
        with self._queued_lock:
            while self._capacity_waiters:
                waiter = self._capacity_waiters.popleft()
                if waiter.gate.done():
                    continue
                self._record(waiter.ticket.ticket_id, "settled")
                failed.append(waiter)
        return failed

    def _owner_main(self) -> None:
        self._owner_thread_id = threading.get_ident()
        try:
            self._lease = self._mutex.acquire()  # type: ignore[attr-defined]
            self._readiness = self._database.open()  # type: ignore[attr-defined]
            with self._state_lock:
                if self._state is _State.CLOSING:
                    self._ready.set()
                    return
                self._state = _State.READY
            self._ready.set()
            self._drain_loop()
        except BaseException as exc:  # noqa: BLE001 - owner thread 边界必须 fail closed。
            # mutex/database.open 可能已经给出稳定 error_code；启动失败时必须保留该
            # 语义，避免上层把 timeout/path/security 全部误判为 generic fatal。
            failure: _CoordinatorError
            if hasattr(exc, "error_code"):
                error_code = str(getattr(exc, "error_code"))
                self._start_error = exc
                failure = CoordinatorDependencyError(error_code, cause=exc)
            else:
                failure = CoordinatorFatalError(cause=exc)
                self._start_error = failure
            with self._state_lock:
                if self._state is not _State.CLOSING:
                    self._state = _State.FAILED
                self._failure = failure
            self._ready.set()
            self._fail_pending(failure)
        finally:
            self._close_resources()

    def _drain_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._release_queued_slot()
            self._record(item.ticket_id, "dequeued")
            self._run_ticket(item)

    def _run_ticket(self, ticket: _Ticket[Any]) -> None:
        started_ns = self._monotonic_ns()
        self._record(ticket.ticket_id, "started")
        uow = None
        try:
            uow = self._database.new_unit_of_work()  # type: ignore[attr-defined]
            try:
                uow.begin_immediate()
            except Exception as exc:  # noqa: BLE001 - BEGIN 未成功时不得 callback/rollback/poison。
                raise TransactionBeginError(cause=exc) from exc
            result = ticket.command(uow)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise AsyncTransactionCommandError()
            uow.precommit()
            precommit_ns = self._monotonic_ns()
            try:
                uow.commit()
            except Exception as exc:  # noqa: BLE001 - COMMIT 已发出后结果未知，必须 poison 而非 rollback。
                self._poison_all(ticket, CommitOutcomeUnknownError(cause=exc))
                return
        except TransactionBeginError as exc:
            self._record(ticket.ticket_id, "settled")
            self._log_ticket(ticket, "sqlite_begin", "failure", exc.error_code, 0, 0, 0)
            self._settle_exception(ticket, exc)
            return
        except AsyncTransactionCommandError as exc:
            self._rollback_or_fail(ticket, uow, exc)
            return
        except Exception as exc:  # noqa: BLE001 - command/precommit 失败均需 rollback 当前事务。
            self._rollback_or_fail(ticket, uow, exc)
            return
        except BaseException as exc:
            self._poison_all(ticket, CoordinatorFatalError(cause=exc))
            return
        # COMMIT 已成功返回后，日志/结果投递都不能再回到 rollback 分支；否则调用方会在
        # 数据已落盘后看到失败并误重试，造成重复写。
        try:
            committed_ns = self._monotonic_ns()
        except Exception:
            committed_ns = precommit_ns
        self._log_ticket(ticket, "sqlite_commit", "success", None, started_ns, precommit_ns, committed_ns)
        self._settle_result(ticket, result)

    def _rollback_or_fail(self, ticket: _Ticket[Any], uow: object | None, original: BaseException) -> None:
        try:
            if uow is not None:
                uow.rollback()  # type: ignore[attr-defined]
        except Exception as rollback_exc:  # noqa: BLE001 - rollback 失败后连接不可再信任。
            self._poison_all(ticket, RollbackFailedError(cause=rollback_exc))
            return
        self._record(ticket.ticket_id, "settled")
        self._log_ticket(
            ticket,
            "sqlite_rollback",
            "failure",
            getattr(original, "error_code", type(original).__name__),
            0,
            0,
            0,
        )
        self._settle_exception(ticket, original)

    def _poison_all(self, active: _Ticket[Any], error: _CoordinatorError) -> None:
        try:
            self._database.poison()  # type: ignore[attr-defined]
        finally:
            with self._state_lock:
                self._state = _State.FAILED
                self._failure = error
            self._record(active.ticket_id, "settled")
            self._log_ticket(active, "sqlite_failure", "failure", error.error_code, 0, 0, 0)
            self._settle_exception(active, error)
            self._fail_pending(error)

    def _fail_pending(self, error: _CoordinatorError) -> None:
        with self._state_lock:
            waiters = self._fail_capacity_waiters_locked(error)
        for waiter in waiters:
            self._settle_waiter_exception(waiter, error)
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is None:
                # close() 可能在 poison drain pending 的窗口塞入关闭哨兵；这里必须把哨兵放回，
                # 让外层 drain loop 正常退出，否则 owner thread 会重新阻塞在 queue.get()。
                self._queue.put(None)
                return
            self._release_queued_slot()
            self._record(item.ticket_id, "settled")
            self._log_ticket(item, "sqlite_failure", "failure", error.error_code, 0, 0, 0)
            self._settle_exception(item, error)

    def _close_resources(self) -> None:
        close_error: CoordinatorCloseError | None = None
        if getattr(self, "_readiness", None) is not None:
            try:
                self._database.close()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                close_error = CoordinatorCloseError("SQLITE_CLOSE_FAILED", cause=exc)
                with self._state_lock:
                    self._state = _State.FAILED
                    self._failure = close_error
        if self._lease is not None:
            if close_error is None:
                try:
                    self._lease.release()  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    close_error = CoordinatorCloseError("SINGLETON_MUTEX_RELEASE_FAILED", cause=exc)
                    with self._state_lock:
                        self._state = _State.FAILED
                        self._failure = close_error
            try:
                self._lease.close()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                close_error = CoordinatorCloseError("SINGLETON_MUTEX_CLOSE_FAILED", cause=exc)
                with self._state_lock:
                    self._state = _State.FAILED
                    self._failure = close_error
        if close_error is None:
            with self._state_lock:
                if self._state is not _State.FAILED:
                    self._state = _State.CLOSED

    def _settle_result[ResultT](self, ticket: _Ticket[ResultT], result: ResultT) -> None:
        self._record(ticket.ticket_id, "settled")

        def settle() -> None:
            if not ticket.future.done():
                ticket.future.set_result(result)

        try:
            ticket.loop.call_soon_threadsafe(settle)
        except RuntimeError:
            return

    def _settle_exception(self, ticket: _Ticket[Any], exc: BaseException) -> None:

        def settle() -> None:
            if not ticket.future.done():
                ticket.future.set_exception(exc)

        try:
            ticket.loop.call_soon_threadsafe(settle)
        except RuntimeError:
            return

    def _settle_waiter_result(self, waiter: _AdmissionWaiter) -> None:
        """跨线程唤醒容量 waiter；loop 已关闭时 ticket 仍按已接收事务继续执行。"""

        def settle() -> None:
            if not waiter.gate.done():
                waiter.gate.set_result(None)

        try:
            waiter.ticket.loop.call_soon_threadsafe(settle)
        except RuntimeError:
            return

    def _settle_waiter_exception(self, waiter: _AdmissionWaiter, exc: BaseException) -> None:
        """让未 admitted waiter 得到稳定错误；调用方已放弃时静默跳过投递。"""

        def settle() -> None:
            if not waiter.gate.done():
                waiter.gate.set_exception(exc)

        try:
            waiter.ticket.loop.call_soon_threadsafe(settle)
        except RuntimeError:
            return

    def _log_ticket(
        self,
        ticket: _Ticket[Any],
        event: str,
        status: str,
        error_code: str | None,
        started_ns: int,
        precommit_ns: int,
        committed_ns: int,
    ) -> None:
        """输出最小稳定事务日志；敏感 context 交给 observability processor 脱敏。"""
        try:
            logger = _get_struct_logger()
            context = dict(ticket.context)
            end_ns = committed_ns or self._monotonic_ns()
            writer_queue_ms = max(0.0, (started_ns - ticket.queued_ns) / 1_000_000) if started_ns else 0.0
            sqlite_commit_ms = max(0.0, (end_ns - precommit_ns) / 1_000_000) if precommit_ns else 0.0
            duration_ms = max(0.0, (end_ns - ticket.queued_ns) / 1_000_000)
            # 事务日志只输出稳定白名单字段；调用方 context 可能包含 SQL、payload 或
            # operation 等业务对象，不能透传到日志事件中。日志失败不能改变事务结果。
            logger.info(
                event,
                operation=ticket.operation,
                correlation_id=str(context.get("correlation_id", "")),
                task_id=str(context.get("task_id", "")),
                run_id=str(context.get("run_id", "")),
                queue_depth=self.queue_depth,
                writer_queue_ms=writer_queue_ms,
                sqlite_commit_ms=sqlite_commit_ms,
                duration_ms=duration_ms,
                status=status,
                error_code=error_code,
            )
        except Exception:
            return


class _StructLogger(Protocol):
    """coordinator 只依赖 info 方法，避免把 structlog 具体类型扩散到调度层。"""

    def info(self, _event: str, **_fields: object) -> None:
        """写入一条结构化日志。"""


def _get_struct_logger() -> _StructLogger:
    """惰性取得 structlog logger，并在未配置时安装生产 JSON stderr 链。

    Phase 1 scheduler 的事务边界必须产生日志；若宿主进程尚未显式配置
    structlog，这里使用 observability 模块的递归脱敏 processor + JSONRenderer，
    并把输出指向 stderr，避免污染 stdout 业务协议。
    """
    try:
        import structlog
    except ModuleNotFoundError:
        return _NoopLogger()
    if not structlog.is_configured():
        from factory_agent.observability.logging import structlog_processor_chain

        structlog.configure(
            processors=cast(Any, structlog_processor_chain(renderer=structlog.processors.JSONRenderer())),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
            cache_logger_on_first_use=False,
        )
    return cast(_StructLogger, structlog.get_logger("factory_agent.scheduler.write_coordinator"))


class _NoopLogger:
    """structlog 尚未安装时的只读降级 logger。"""

    def info(self, _event: str, **_fields: object) -> None:
        """保持调用表面一致；不输出日志，避免 stdout/stderr 污染测试协议。"""
        return None
