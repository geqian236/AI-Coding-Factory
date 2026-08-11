"""真实 Win32 Global named mutex 与 SQLite 前置顺序 RED 测试。"""

from __future__ import annotations

import ctypes
import hashlib
import importlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, TextIO

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="NamedMutex 依赖真实 Win32 kernel object 与 DACL；其他平台诚实 skip。",
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
AGENT_SRC = REPOSITORY_ROOT / "apps/agent/src"
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED_0 = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
RECEIPT_VERSION = 1
RECEIPT_TIMEOUT_SECONDS = 5.0


class _CleanableChild(Protocol):
    """约束 registry 清理所需的最小 child 表面，便于独立验证聚合失败。"""

    def cleanup(self) -> None:
        """有界回收 child 及其 pipes/pumps。"""


def _mutex_module() -> ModuleType:
    """延迟导入待实现 Win32 adapter，保持 RED 为测试失败而非收集中断。"""
    return importlib.import_module("factory_agent.integrations.windows.named_mutex")


def _database_module() -> ModuleType:
    """跨进程启动顺序测试所需 SQLite database 模块。"""
    return importlib.import_module("factory_agent.storage.sqlite.database")


def _coordinator_module() -> ModuleType:
    """跨进程启动顺序测试所需唯一 writer coordinator。"""
    return importlib.import_module("factory_agent.scheduler.write_coordinator")


def _assert_d_test_path(path: Path) -> Path:
    """所有跨进程 DB/marker/temp 都必须解析到 D:\\codex项目。"""
    resolved = path.resolve()
    try:
        resolved.relative_to(Path("D:/codex项目").resolve())
    except ValueError as exc:
        raise AssertionError(f"Windows 集成测试路径越出 D 盘受控根：{resolved}") from exc
    return resolved


def _case_root(tmp_path: Path) -> Path:
    """为每例创建可精确验证和清理的 UUID 子目录。"""
    root = _assert_d_test_path(tmp_path / uuid.uuid4().hex)
    root.mkdir(parents=False, exist_ok=False)
    return root


@dataclass(slots=True)
class ManagedChild:
    """注册即接管的 child；stdout/stderr 由 pump queue 读取，所有结束路径都有界。"""

    process: subprocess.Popen[str]
    stdout_queue: queue.Queue[str | None] = field(default_factory=queue.Queue)
    stderr_queue: queue.Queue[str | None] = field(default_factory=queue.Queue)
    pumps: list[threading.Thread] = field(default_factory=list)
    stdout_eof_seen: bool = False

    def start_pumps(self) -> None:
        """启动 daemon pump，禁止测试主线程在 readline 上永久阻塞。"""
        assert self.process.stdout is not None
        assert self.process.stderr is not None

        def pump(stream: TextIO, destination: queue.Queue[str | None]) -> None:
            try:
                for line in iter(stream.readline, ""):
                    destination.put(line)
            finally:
                destination.put(None)

        for name, stream, destination in (
            ("stdout", self.process.stdout, self.stdout_queue),
            ("stderr", self.process.stderr, self.stderr_queue),
        ):
            thread = threading.Thread(
                target=pump,
                args=(stream, destination),
                name=f"mutex-child-{self.process.pid}-{name}",
                daemon=True,
            )
            thread.start()
            self.pumps.append(thread)

    def receipt(self, *, kind: str, timeout: float = RECEIPT_TIMEOUT_SECONDS) -> dict[str, Any]:
        """在 5 秒内读取唯一 versioned JSON receipt；已缓冲 trailing stdout 立即失败。"""
        try:
            line = self.stdout_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise AssertionError(f"child {self.process.pid} receipt timeout") from exc
        if line is None:
            raise AssertionError(f"child {self.process.pid} stdout EOF; stderr={self.stderr_text()}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"child {self.process.pid} invalid JSON receipt: {line!r}") from exc
        assert value["receiptVersion"] == RECEIPT_VERSION
        assert value["kind"] == kind
        self._reject_buffered_trailing_stdout()
        return value

    def _reject_buffered_trailing_stdout(self) -> None:
        """receipt 之后只能暂时无输出或 EOF，第二条 stdout 永远是协议错误。"""
        while True:
            try:
                trailing = self.stdout_queue.get_nowait()
            except queue.Empty:
                return
            if trailing is None:
                self.stdout_eof_seen = True
                return
            raise AssertionError(f"child {self.process.pid} trailing stdout after receipt: {trailing!r}")

    def assert_stdout_eof_without_trailing(self, timeout: float = RECEIPT_TIMEOUT_SECONDS) -> None:
        """child 退出后有界等待 pump EOF，并拒绝 receipt 后任何迟到 stdout。"""
        if self.stdout_eof_seen:
            return
        try:
            trailing = self.stdout_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise AssertionError(f"child {self.process.pid} stdout EOF timeout") from exc
        if trailing is not None:
            raise AssertionError(f"child {self.process.pid} trailing stdout after receipt: {trailing!r}")
        self.stdout_eof_seen = True

    def stderr_text(self) -> str:
        """只拼接 pump 已收集 stderr，不再直接阻塞 read。"""
        lines: list[str] = []
        while True:
            try:
                line = self.stderr_queue.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                lines.append(line)
        return "".join(lines)

    def graceful(self, command: str = "release\n") -> None:
        """发送协议化 graceful 命令并在 5 秒内等待退出。"""
        if self.process.poll() is not None:
            return
        assert self.process.stdin is not None
        self.process.stdin.write(command)
        self.process.stdin.flush()
        self.process.stdin.close()
        self.process.wait(timeout=RECEIPT_TIMEOUT_SECONDS)

    def cleanup(self) -> None:
        """固定 graceful→terminate→kill；每个阶段与 pipe/pump 都 best-effort 有界完成。"""
        failures: list[Exception] = []
        if self.process.poll() is None:
            try:
                self.graceful()
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                pass
            except Exception as exc:  # noqa: BLE001 - 清理必须收集后继续。
                failures.append(exc)
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            except Exception as exc:  # noqa: BLE001 - kill 仍须继续。
                failures.append(exc)
        if self.process.poll() is None:
            try:
                self.process.kill()
                self.process.wait(timeout=2)
            except Exception as exc:  # noqa: BLE001 - pipes/pumps 仍须继续。
                failures.append(exc)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except (BrokenPipeError, OSError, ValueError):
                    # Windows 上 child 已异常退出时 close 可能返回 ERROR_INVALID_PARAMETER。
                    pass
        for thread in self.pumps:
            thread.join(timeout=2)
            if thread.is_alive():
                failures.append(AssertionError(f"child pump 未退出：{thread.name}"))
        if failures:
            raise ExceptionGroup(f"child {self.process.pid} cleanup failures", failures)


@dataclass(frozen=True, slots=True)
class ChildResult:
    """已由 ManagedChild pump 收集的 contender 结果。"""

    returncode: int
    receipt: dict[str, Any]
    stderr: str


def _spawn_managed(
    arguments: list[str],
    registry: list[ManagedChild],
    *,
    close_fds: bool = True,
) -> ManagedChild:
    """Popen 后立即登记 owner，再启动 pump；中途断言失败也能由 finally 回收。"""
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=_child_environment(),
        close_fds=close_fds,
    )
    child = ManagedChild(process)
    registry.append(child)
    child.start_pumps()
    return child


def _cleanup_case_root(root: Path, children: Sequence[_CleanableChild] | None = None) -> None:
    """逐 child best-effort 清理并聚合异常；首个坏 child 不得阻断其余回收。"""
    failures: list[Exception] = []
    for child in children or []:
        try:
            child.cleanup()
        except Exception as exc:  # noqa: BLE001 - 必须继续清理 registry 全集。
            failures.append(exc)
    resolved = _assert_d_test_path(root)
    assert len(resolved.name) == 32
    int(resolved.name, 16)
    if resolved.exists():
        shutil.rmtree(resolved)
    assert not resolved.exists()
    if failures:
        raise ExceptionGroup("managed child registry cleanup failures", failures)


@dataclass(slots=True)
class _CleanupProbeChild:
    """记录 best-effort registry 回收顺序，可选注入一个清理失败。"""

    name: str
    calls: list[str]
    failure: Exception | None = None

    def cleanup(self) -> None:
        """记录已尝试回收；失败仅由 registry 在全量回收后聚合。"""
        self.calls.append(self.name)
        if self.failure is not None:
            raise self.failure


def _child_environment() -> dict[str, str]:
    """继承 dev.ps1 的 D 盘 TEMP/TMP，并显式提供 agent src import 根。"""
    environment = dict(os.environ)
    current_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(AGENT_SRC) + (os.pathsep + current_pythonpath if current_pythonpath else "")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    for variable in ("TEMP", "TMP"):
        assert variable in environment
        _assert_d_test_path(Path(environment[variable]))
    return environment


MUTEX_HOLDER_PROGRAM = r"""
import json
import os
import sys
from pathlib import Path

from factory_agent.integrations.windows.named_mutex import NamedMutex

volume_identity, database_path = sys.argv[1], Path(sys.argv[2])
mutex = NamedMutex.for_database(
    volume_identity=volume_identity,
    state_database_path=database_path,
    timeout_ms=5000,
)
lease = mutex.acquire()
print(json.dumps({
    "receiptVersion": 1,
    "kind": "mutex-holder-ready",
    "status": "ready",
    "name": mutex.name,
    "abandoned": lease.abandoned,
    "temp": os.environ.get("TEMP"),
    "tmp": os.environ.get("TMP"),
}), flush=True)
sys.stdin.readline()
lease.release()
lease.close()
"""


ABANDONED_CONTENDER_PROGRAM = r"""
import json
import sys
from pathlib import Path

from factory_agent.integrations.windows.named_mutex import NamedMutex

volume_identity = sys.argv[1]
database_path = Path(sys.argv[2])
waiting_marker = Path(sys.argv[3])
open_marker = Path(sys.argv[4])
mutex = NamedMutex.for_database(
    volume_identity=volume_identity,
    state_database_path=database_path,
    timeout_ms=5000,
)
waiting_marker.write_text("waiting", encoding="utf-8")
lease = mutex.acquire()
try:
    receipt = {
        "receiptVersion": 1,
        "kind": "mutex-abandoned-acquire",
        "status": "ready",
        "abandoned": lease.abandoned,
        "databaseOpenedBeforeAcquire": open_marker.exists(),
    }
    open_marker.write_text("opened-after-abandoned", encoding="utf-8")
finally:
    lease.release()
    lease.close()
print(json.dumps(receipt, separators=(",", ":")), flush=True)
"""


CONTENDER_PROGRAM = r"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from factory_agent.integrations.windows.named_mutex import NamedMutex
from factory_agent.scheduler.write_coordinator import WriteCoordinator
from factory_agent.storage.sqlite.database import SqliteDatabase

volume_identity = sys.argv[1]
database_path = Path(sys.argv[2])
open_marker = Path(sys.argv[3])
epoch_marker = Path(sys.argv[4])
scan_marker = Path(sys.argv[5])
timeout_ms = int(sys.argv[6])

def marked_connect(*args, **kwargs):
    open_marker.write_text("connect-called", encoding="utf-8")
    return sqlite3.connect(*args, **kwargs)

async def bounded(awaitable):
    inner = asyncio.wait_for(awaitable, timeout=4.0)
    return await asyncio.wait_for(inner, timeout=4.5)

async def main():
    mutex = NamedMutex.for_database(
        volume_identity=volume_identity,
        state_database_path=database_path,
        timeout_ms=timeout_ms,
    )
    database = SqliteDatabase(database_path, connect_factory=marked_connect)
    coordinator = WriteCoordinator(database=database, mutex=mutex, queue_capacity=2)
    try:
        readiness = await bounded(coordinator.start())
    except Exception as exc:
        print(json.dumps({
            "receiptVersion": 1,
            "kind": "mutex-contender-result",
            "status": "error",
            "error_code": getattr(exc, "error_code", type(exc).__name__),
        }), flush=True)
        return 23
    epoch_marker.write_text("advanced-after-ready", encoding="utf-8")
    scan_marker.write_text("scan-after-ready", encoding="utf-8")
    await bounded(coordinator.close())
    print(json.dumps({
        "receiptVersion": 1,
        "kind": "mutex-contender-result",
        "status": "ready",
        "foreign_keys": readiness.foreign_keys,
        "journal_mode": readiness.journal_mode,
        "synchronous": readiness.synchronous,
    }), flush=True)
    return 0

raise SystemExit(asyncio.run(main()))
"""


HANDLE_PROBE_PROGRAM = r"""
import ctypes
import json
import sys

handle = int(sys.argv[1])
flags = ctypes.c_ulong()
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
get_handle_information = kernel32.GetHandleInformation
get_handle_information.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
get_handle_information.restype = ctypes.c_bool
valid = bool(get_handle_information(ctypes.c_void_p(handle), ctypes.byref(flags)))
error = 0 if valid else ctypes.get_last_error()
print(json.dumps({
    "receiptVersion": 1,
    "kind": "mutex-handle-inheritance-probe",
    "status": "ready",
    "handleValidInChild": valid,
    "lastError": error,
}), flush=True)
"""


TRAILING_RECEIPT_PROGRAM = r"""
import json

print(json.dumps({
    "receiptVersion": 1,
    "kind": "single-receipt",
    "status": "ready",
}), flush=True)
print("forbidden-trailing-stdout", flush=True)
"""


def _start_holder(
    volume_identity: str,
    database_path: Path,
    registry: list[ManagedChild],
) -> tuple[ManagedChild, dict[str, Any]]:
    """启动真实 child holder，并等到其已取得 mutex 的单行 JSON receipt。"""
    child = _spawn_managed(
        [sys.executable, "-c", MUTEX_HOLDER_PROGRAM, volume_identity, str(database_path)],
        registry,
    )
    receipt = child.receipt(kind="mutex-holder-ready")
    assert receipt["status"] == "ready"
    _assert_d_test_path(Path(receipt["temp"]))
    _assert_d_test_path(Path(receipt["tmp"]))
    return child, receipt


def _graceful_stop_holder(child: ManagedChild) -> None:
    """让 holder 在 owner thread ReleaseMutex 并等待完全退出。"""
    child.graceful()
    assert child.process.returncode == 0
    child.assert_stdout_eof_without_trailing()


def _wait_for_marker(path: Path, timeout: float = RECEIPT_TIMEOUT_SECONDS) -> None:
    """以硬 deadline 等待 child 进入 acquire 前切点，禁止无界轮询。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.01)
    raise AssertionError(f"child marker timeout: {path}")


def _run_contender(
    *,
    volume_identity: str,
    database_path: Path,
    open_marker: Path,
    epoch_marker: Path,
    scan_marker: Path,
    timeout_ms: int,
    registry: list[ManagedChild],
) -> ChildResult:
    """在 child 中运行真实 coordinator，避免同线程 mutex 递归获取掩盖竞争。"""
    child = _spawn_managed(
        [
            sys.executable,
            "-c",
            CONTENDER_PROGRAM,
            volume_identity,
            str(database_path),
            str(open_marker),
            str(epoch_marker),
            str(scan_marker),
            str(timeout_ms),
        ],
        registry,
    )
    receipt = child.receipt(kind="mutex-contender-result")
    child.process.wait(timeout=RECEIPT_TIMEOUT_SECONDS)
    child.assert_stdout_eof_without_trailing()
    return ChildResult(
        returncode=child.process.returncode,
        receipt=receipt,
        stderr=child.stderr_text(),
    )


def _expected_mutex_name(volume_identity: str, database_path: Path) -> str:
    """独立重算 v1 JCS+SHA name，禁止依赖 PID/SID/random/Python hash。"""
    canonical_path = str(database_path.resolve()).replace("/", "\\").casefold()
    material = json.dumps(
        ["factory-agent-singleton-v1", volume_identity, canonical_path],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "Global\\AI-Coding-Factory.Agent." + hashlib.sha256(material).hexdigest()


def test_managed_child_rejects_stdout_after_single_receipt(tmp_path: Path) -> None:
    """receipt protocol 只允许一行 JSON；child 退出后任何 trailing stdout 都失败。"""
    root = _case_root(tmp_path)
    children: list[ManagedChild] = []
    try:
        child = _spawn_managed([sys.executable, "-c", TRAILING_RECEIPT_PROGRAM], children)
        with pytest.raises(AssertionError, match="trailing stdout after receipt"):
            assert child.receipt(kind="single-receipt")["status"] == "ready"
            child.process.wait(timeout=RECEIPT_TIMEOUT_SECONDS)
            child.assert_stdout_eof_without_trailing()
    finally:
        _cleanup_case_root(root, children)


def test_child_registry_cleanup_is_best_effort_and_aggregates_failures(tmp_path: Path) -> None:
    """单个 child 清理失败不得阻断后续 child 与用例目录回收。"""
    root = _case_root(tmp_path)
    calls: list[str] = []
    children = [
        _CleanupProbeChild("first", calls, RuntimeError("first cleanup failed")),
        _CleanupProbeChild("second", calls),
        _CleanupProbeChild("third", calls, OSError("third cleanup failed")),
    ]

    with pytest.raises(ExceptionGroup) as captured:
        _cleanup_case_root(root, children)

    assert calls == ["first", "second", "third"]
    assert [str(error) for error in captured.value.exceptions] == [
        "first cleanup failed",
        "third cleanup failed",
    ]
    assert not root.exists()


def test_mutex_name_is_stable_global_jcs_sha256_and_storage_scoped(tmp_path: Path) -> None:
    """同一 storage identity 跨对象同名，不同 DB 分域，且 name 不含 SID/PID/random。"""
    mutex_module = _mutex_module()
    root = _case_root(tmp_path)
    try:
        database_path = root / "state.sqlite3"
        volume_identity = "volume-guid:factory-test-volume"
        first = mutex_module.NamedMutex.for_database(
            volume_identity=volume_identity,
            state_database_path=database_path,
            timeout_ms=50,
        )
        second = mutex_module.NamedMutex.for_database(
            volume_identity=volume_identity,
            state_database_path=database_path,
            timeout_ms=50,
        )
        other = mutex_module.NamedMutex.for_database(
            volume_identity=volume_identity,
            state_database_path=root / "other.sqlite3",
            timeout_ms=50,
        )

        assert first.name == second.name == _expected_mutex_name(volume_identity, database_path)
        assert first.name.startswith("Global\\AI-Coding-Factory.Agent.")
        assert len(first.name.rsplit(".", 1)[1]) == 64
        assert first.name != other.name
        assert str(os.getpid()) not in first.name
        assert uuid.uuid4().hex not in first.name
    finally:
        _cleanup_case_root(root)


def test_p08_real_mutex_acquire_release_and_reacquire(tmp_path: Path) -> None:
    """真实 Global mutex 正常释放后同一 storage identity 可再次取得。"""
    mutex_module = _mutex_module()
    root = _case_root(tmp_path)
    try:
        path = root / "state.sqlite3"
        mutex = mutex_module.NamedMutex.for_database(
            volume_identity="volume-guid:p08",
            state_database_path=path,
            timeout_ms=100,
        )
        first = mutex.acquire()
        assert first.abandoned is False
        receipt = first.security_receipt
        assert receipt.protected_dacl is True
        assert receipt.inheritable is False
        assert isinstance(receipt.current_sid, str) and receipt.current_sid
        assert receipt.owner_sid == receipt.current_sid
        assert tuple(receipt.allowed_sids) == (receipt.current_sid,)
        assert tuple(receipt.denied_sids) == ()
        assert tuple(receipt.allowed_rights) == ("SYNCHRONIZE", "MUTEX_MODIFY_STATE")
        assert "S-1-1-0" not in receipt.allowed_sids  # Everyone
        assert "S-1-5-11" not in receipt.allowed_sids  # Authenticated Users
        first.release()
        first.close()

        second = mutex.acquire()
        assert second.abandoned is False
        second.release()
        second.close()
    finally:
        _cleanup_case_root(root)


def test_real_mutex_handle_is_noninheritable_even_when_grandchild_inherits_handles(
    tmp_path: Path,
) -> None:
    """父进程显式允许继承其他 HANDLE 时，mutex native handle 在 grandchild 仍必须无效。"""
    mutex_module = _mutex_module()
    root = _case_root(tmp_path)
    children: list[ManagedChild] = []
    lease: Any | None = None
    try:
        mutex = mutex_module.NamedMutex.for_database(
            volume_identity="volume-guid:noninherit",
            state_database_path=root / "state.sqlite3",
            timeout_ms=100,
        )
        lease = mutex.acquire()
        assert lease.security_receipt.inheritable is False
        child = _spawn_managed(
            [sys.executable, "-c", HANDLE_PROBE_PROGRAM, str(lease.native_handle)],
            children,
            close_fds=False,
        )
        receipt = child.receipt(kind="mutex-handle-inheritance-probe")
        child.process.wait(timeout=RECEIPT_TIMEOUT_SECONDS)
        child.assert_stdout_eof_without_trailing()
        assert receipt == {
            "receiptVersion": 1,
            "kind": "mutex-handle-inheritance-probe",
            "status": "ready",
            "handleValidInChild": False,
            "lastError": 6,
        }
    finally:
        if lease is not None:
            lease.release()
            lease.close()
        _cleanup_case_root(root, children)


def test_release_from_non_owner_thread_fails_then_owner_can_release(tmp_path: Path) -> None:
    """ReleaseMutex 只能由取得它的 OS 线程调用；错误尝试不得丢失 owner lease。"""
    mutex_module = _mutex_module()
    root = _case_root(tmp_path)
    try:
        mutex = mutex_module.NamedMutex.for_database(
            volume_identity="volume-guid:owner-thread",
            state_database_path=root / "state.sqlite3",
            timeout_ms=100,
        )
        lease = mutex.acquire()
        result: queue.Queue[BaseException | None] = queue.Queue()

        def wrong_thread_release() -> None:
            try:
                lease.release()
            except BaseException as exc:  # noqa: BLE001 - 测试必须捕获线程内稳定错误。
                result.put(exc)
            else:
                result.put(None)

        thread = threading.Thread(target=wrong_thread_release)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        failure = result.get(timeout=1)
        assert isinstance(failure, mutex_module.MutexOwnershipError)
        assert failure.error_code == "SINGLETON_MUTEX_WRONG_OWNER"
        lease.release()
        lease.close()
    finally:
        _cleanup_case_root(root)


def test_c03_child_contender_timeout_never_opens_sqlite_or_advances_downstream(tmp_path: Path) -> None:
    """真实 holder 存活时 contender timeout，DB/open-marker/epoch/scan 全部保持 before。"""
    root = _case_root(tmp_path)
    children: list[ManagedChild] = []
    try:
        volume_identity = "volume-guid:c03"
        database_path = root / "state.sqlite3"
        open_marker = root / "open-marker"
        epoch_marker = root / "epoch-marker"
        scan_marker = root / "scan-marker"
        epoch_marker.write_text("epoch=7", encoding="utf-8")
        scan_marker.write_text("scan=0", encoding="utf-8")
        holder, _receipt = _start_holder(volume_identity, database_path, children)

        contender = _run_contender(
            volume_identity=volume_identity,
            database_path=database_path,
            open_marker=open_marker,
            epoch_marker=epoch_marker,
            scan_marker=scan_marker,
            timeout_ms=50,
            registry=children,
        )
        assert contender.returncode == 23
        assert contender.receipt == {
            "receiptVersion": 1,
            "kind": "mutex-contender-result",
            "status": "error",
            "error_code": "SINGLETON_MUTEX_TIMEOUT",
        }
        assert not database_path.exists()
        assert not Path(str(database_path) + "-wal").exists()
        assert not Path(str(database_path) + "-shm").exists()
        assert not open_marker.exists()
        assert epoch_marker.read_text(encoding="utf-8") == "epoch=7"
        assert scan_marker.read_text(encoding="utf-8") == "scan=0"
        _graceful_stop_holder(holder)
    finally:
        _cleanup_case_root(root, children)


def test_c04_release_then_real_contender_reaches_sqlite_ready(tmp_path: Path) -> None:
    """holder 正常释放后 contender 才能 open SQLite、读回 PRAGMA 并做后续动作。"""
    root = _case_root(tmp_path)
    children: list[ManagedChild] = []
    try:
        volume_identity = "volume-guid:c04"
        database_path = root / "state.sqlite3"
        open_marker = root / "open-marker"
        epoch_marker = root / "epoch-marker"
        scan_marker = root / "scan-marker"
        holder, _receipt = _start_holder(volume_identity, database_path, children)
        _graceful_stop_holder(holder)

        contender = _run_contender(
            volume_identity=volume_identity,
            database_path=database_path,
            open_marker=open_marker,
            epoch_marker=epoch_marker,
            scan_marker=scan_marker,
            timeout_ms=500,
            registry=children,
        )
        assert contender.returncode == 0, contender.stderr
        assert contender.receipt == {
            "receiptVersion": 1,
            "kind": "mutex-contender-result",
            "status": "ready",
            "foreign_keys": 1,
            "journal_mode": "wal",
            "synchronous": 2,
        }
        assert database_path.is_file()
        assert open_marker.is_file()
        assert epoch_marker.read_text(encoding="utf-8") == "advanced-after-ready"
        assert scan_marker.read_text(encoding="utf-8") == "scan-after-ready"
    finally:
        _cleanup_case_root(root, children)


def test_c05_hard_killed_holder_yields_abandoned_before_successor_db_work(tmp_path: Path) -> None:
    """TerminateProcess 后已等待的 successor 必须观测 ABANDONED，且取得后才可继续。"""
    root = _case_root(tmp_path)
    children: list[ManagedChild] = []
    try:
        volume_identity = "volume-guid:c05"
        database_path = root / "state.sqlite3"
        open_marker = root / "open-marker"
        waiting_marker = root / "waiting-marker"
        holder, _receipt = _start_holder(volume_identity, database_path, children)
        assert str(_receipt["name"]).startswith("Global\\")
        contender = _spawn_managed(
            [
                sys.executable,
                "-c",
                ABANDONED_CONTENDER_PROGRAM,
                volume_identity,
                str(database_path),
                str(waiting_marker),
                str(open_marker),
            ],
            children,
        )
        _wait_for_marker(waiting_marker)
        holder.process.kill()
        holder.process.wait(timeout=RECEIPT_TIMEOUT_SECONDS)
        assert holder.process.poll() is not None
        holder.assert_stdout_eof_without_trailing()
        receipt = contender.receipt(kind="mutex-abandoned-acquire")
        contender.process.wait(timeout=RECEIPT_TIMEOUT_SECONDS)
        assert contender.process.poll() == 0
        contender.assert_stdout_eof_without_trailing()
        assert receipt == {
            "receiptVersion": 1,
            "kind": "mutex-abandoned-acquire",
            "status": "ready",
            "abandoned": True,
            "databaseOpenedBeforeAcquire": False,
        }
        assert open_marker.read_text(encoding="utf-8") == "opened-after-abandoned"
    finally:
        _cleanup_case_root(root, children)


def _create_default_acl_mutex(name: str) -> int:
    """用默认 ACL 预创建同名 mutex，验证生产 adapter 必须核验实际 DACL。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    handle = create_mutex(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _create_named_event(name: str) -> int:
    """预创建同名但不同 kernel object type，CreateMutex 必须 fail closed。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_event = kernel32.CreateEventW
    create_event.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
    create_event.restype = ctypes.c_void_p
    handle = create_event(None, False, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _close_handle(handle: int) -> None:
    """关闭测试预建 kernel handle。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool
    assert close_handle(ctypes.c_void_p(handle))


def test_c06_existing_wrong_acl_is_rejected_without_default_acl_fallback(tmp_path: Path) -> None:
    """已有对象 descriptor 被忽略时，实际 DACL 非 exact current-SID 必须拒绝。"""
    mutex_module = _mutex_module()
    root = _case_root(tmp_path)
    handle: int | None = None
    try:
        mutex = mutex_module.NamedMutex.for_database(
            volume_identity="volume-guid:c06-acl",
            state_database_path=root / "state.sqlite3",
            timeout_ms=50,
        )
        handle = _create_default_acl_mutex(mutex.name)
        with pytest.raises(mutex_module.MutexSecurityError) as caught:
            mutex.acquire()
        assert caught.value.error_code == "SINGLETON_MUTEX_DACL_MISMATCH"
    finally:
        if handle is not None:
            _close_handle(handle)
        _cleanup_case_root(root)


def test_c06_same_name_wrong_kernel_object_is_rejected(tmp_path: Path) -> None:
    """同名 Event 不能被误当 Mutex；ERROR_INVALID_HANDLE 必须稳定失败并闭合。"""
    mutex_module = _mutex_module()
    root = _case_root(tmp_path)
    handle: int | None = None
    try:
        mutex = mutex_module.NamedMutex.for_database(
            volume_identity="volume-guid:c06-object",
            state_database_path=root / "state.sqlite3",
            timeout_ms=50,
        )
        handle = _create_named_event(mutex.name)
        with pytest.raises(mutex_module.MutexCreationError) as caught:
            mutex.acquire()
        assert caught.value.error_code == "SINGLETON_MUTEX_CREATE_FAILED"
    finally:
        if handle is not None:
            _close_handle(handle)
        _cleanup_case_root(root)


class _FakeWin32Api:
    """覆盖不可稳定触发的 ACL/Create/Wait failure 分支，并审计 CloseHandle。"""

    def __init__(
        self,
        *,
        wait_result: int = WAIT_OBJECT_0,
        last_error: int = 0,
        build_security_error: BaseException | None = None,
        create_error: int | None = None,
        dacl_matches: bool = True,
    ) -> None:
        self.wait_result = wait_result
        self.last_error = last_error
        self.build_security_error = build_security_error
        self.create_error = create_error
        self.dacl_matches = dacl_matches
        self.calls: list[tuple[str, object]] = []

    def build_current_user_security(self) -> object:
        """模拟 protected current-SID DACL 构造。"""
        self.calls.append(("build_security", None))
        if self.build_security_error is not None:
            raise self.build_security_error
        return object()

    def create_mutex(self, name: str, security: object) -> int:
        """模拟 CreateMutexExW；Global 失败不允许重试 Local。"""
        self.calls.append(("create_mutex", name))
        if self.create_error is not None:
            raise OSError(self.create_error, "create failed")
        assert security is not None
        return 101

    def verify_current_user_dacl(self, handle: int) -> bool:
        """模拟读取并核验实际 DACL。"""
        self.calls.append(("verify_dacl", handle))
        return self.dacl_matches

    def wait(self, handle: int, timeout_ms: int) -> int:
        """返回指定 WaitForSingleObject 分支。"""
        self.calls.append(("wait", (handle, timeout_ms)))
        return self.wait_result

    def release(self, handle: int) -> None:
        """记录 ReleaseMutex。"""
        self.calls.append(("release", handle))

    def close(self, handle: int) -> None:
        """记录 CloseHandle。"""
        self.calls.append(("close", handle))

    def get_last_error(self) -> int:
        """提供 WAIT_FAILED 的稳定 Win32 code。"""
        return self.last_error


@pytest.mark.parametrize(
    ("api", "error_name", "error_code"),
    [
        (
            _FakeWin32Api(build_security_error=OSError(5, "acl build")),
            "MutexSecurityError",
            "SINGLETON_MUTEX_SECURITY_FAILED",
        ),
        (
            _FakeWin32Api(create_error=5),
            "MutexCreationError",
            "SINGLETON_MUTEX_CREATE_FAILED",
        ),
        (
            _FakeWin32Api(dacl_matches=False),
            "MutexSecurityError",
            "SINGLETON_MUTEX_DACL_MISMATCH",
        ),
        (
            _FakeWin32Api(wait_result=WAIT_TIMEOUT),
            "MutexTimeoutError",
            "SINGLETON_MUTEX_TIMEOUT",
        ),
        (
            _FakeWin32Api(wait_result=WAIT_FAILED, last_error=6),
            "MutexWaitError",
            "SINGLETON_MUTEX_WAIT_FAILED",
        ),
    ],
)
def test_n13_failure_branches_close_handles_and_never_fallback_local(
    tmp_path: Path,
    api: _FakeWin32Api,
    error_name: str,
    error_code: str,
) -> None:
    """ACL/Create/TIMEOUT/WAIT_FAILED 均 fail closed；Global 失败不回退 Local/default ACL。"""
    mutex_module = _mutex_module()
    root = _case_root(tmp_path)
    try:
        mutex = mutex_module.NamedMutex.for_database(
            volume_identity="volume-guid:n13",
            state_database_path=root / "state.sqlite3",
            timeout_ms=37,
            win32_api=api,
        )
        error_type = getattr(mutex_module, error_name)
        with pytest.raises(error_type) as caught:
            mutex.acquire()
        assert caught.value.error_code == error_code
        create_names = [value for call, value in api.calls if call == "create_mutex"]
        assert all(str(name).startswith("Global\\") for name in create_names)
        assert not any(str(name).startswith("Local\\") for name in create_names)
        if any(call in {"verify_dacl", "wait"} for call, _value in api.calls):
            assert ("close", 101) in api.calls
    finally:
        _cleanup_case_root(root)


def test_wait_abandoned_returns_owned_lease_and_normal_release(tmp_path: Path) -> None:
    """WAIT_ABANDONED 已取得 mutex：必须标记 abandoned 后走正常 recovery/release。"""
    mutex_module = _mutex_module()
    root = _case_root(tmp_path)
    api = _FakeWin32Api(wait_result=WAIT_ABANDONED_0)
    try:
        mutex = mutex_module.NamedMutex.for_database(
            volume_identity="volume-guid:abandoned-fake",
            state_database_path=root / "state.sqlite3",
            timeout_ms=37,
            win32_api=api,
        )
        lease = mutex.acquire()
        assert lease.abandoned is True
        lease.release()
        lease.close()
        assert ("release", 101) in api.calls
        assert api.calls[-1] == ("close", 101)
    finally:
        _cleanup_case_root(root)
