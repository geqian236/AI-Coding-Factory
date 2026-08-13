"""SQLite 单连接生命周期、迁移账本与升级前一致性备份。"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, overload

from factory_agent.errors import FactoryError, StorageViolationError
from factory_agent.observability.logging import get_logger
from factory_agent.security import storage_contract

if TYPE_CHECKING:
    from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork

LOGGER = get_logger(__name__)
MIGRATION_NAMES = (
    "0001_workflow.sql",
    "0002_event_store.sql",
    "0003_auth_resources.sql",
    "0004_control_budget.sql",
)
SCHEMA_MIGRATIONS_SQL = (
    "CREATE TABLE schema_migrations ("
    "version INTEGER PRIMARY KEY NOT NULL,"
    "name TEXT NOT NULL,"
    "checksum TEXT NOT NULL,"
    "applied_at TEXT NOT NULL"
    ") STRICT"
)


class DatabaseStartupError(FactoryError):
    """启动数据库时以稳定、脱敏错误码 fail closed。"""

    error_code = "SQLITE_STARTUP_FAILED"

    def __init__(self, error_code: str) -> None:
        super().__init__("sqlite startup failed closed")
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class DatabaseReadiness:
    """同一 owner 连接实际读回的就绪事实。"""

    owner_thread_id: int
    foreign_keys: int
    journal_mode: str
    synchronous: int
    schema_version: int
    applied_migrations: tuple[str, ...]


class SqliteDatabase:
    """只允许 owner 线程持有一个真实 SQLite 连接。"""

    def __init__(
        self,
        database_path: Path,
        *,
        backup_root: Path | None = None,
        connect_factory: Callable[..., sqlite3.Connection] = sqlite3.connect,
        failure_probe: Callable[[str], None] | None = None,
    ) -> None:
        self._database_path = Path(database_path)
        self._backup_root = Path(backup_root) if backup_root is not None else self._database_path.parent / "backups"
        self._connect_factory = connect_factory
        self._probe = failure_probe or (lambda _point: None)
        self._connection: sqlite3.Connection | None = None
        self._owner_thread_id: int | None = None
        self._poisoned = False

    @property
    def connection_identity(self) -> int:
        """返回真实 owner 连接 identity，供 UoW 同连接断言使用。"""
        return id(self._require_connection())

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None or self._poisoned:
            raise RuntimeError("sqlite database is not available")
        if self._owner_thread_id != threading.get_ident():
            raise RuntimeError("sqlite connection accessed outside owner thread")
        return self._connection

    def _validate_paths(self) -> None:
        """在 probe、建目录和连接前复用统一存储合同校验原始路径。"""
        try:
            # 统一合同会将相对路径转为绝对路径，因此先保留调用边界的显式约束，
            # 防止调用方依赖进程 cwd 获得意外的可写位置。
            if (
                not self._database_path.is_absolute()
                or ".." in self._database_path.parts
                or not self._backup_root.is_absolute()
                or ".." in self._backup_root.parts
            ):
                raise StorageViolationError("relative or parent path is not allowed")
            # 备份根若指向状态库文件本身，任何 mkdir/connect 都可能把文件与目录语义混用；
            # 同时比较调用方绝对值和统一合同规范值，阻断大小写或路径规范化后的别名。
            if self._backup_root == self._database_path:
                raise StorageViolationError("backup root must not be database file")
            database_receipt = storage_contract.validate_storage_path(self._database_path)
            backup_receipt = storage_contract.validate_storage_path(self._backup_root)
            if backup_receipt.path == database_receipt.path:
                raise StorageViolationError("backup root must not be database file")
            if backup_receipt.path == database_receipt.path.parent:
                raise StorageViolationError("backup root must be isolated from database directory")
        except (NotImplementedError, OSError, StorageViolationError):
            raise DatabaseStartupError("SQLITE_BACKUP_PATH_INVALID") from None

    def open(self) -> DatabaseReadiness:
        """校验路径后连接、备份、迁移并发布同连接 readback；任一步失败关闭连接。"""
        started = time.monotonic()
        if self._connection is not None or self._owner_thread_id is not None:
            LOGGER.error(
                "sqlite_open_rejected",
                operation="sqlite_open",
                status="failed",
                error_code="SQLITE_ALREADY_OPEN",
                error_type="DatabaseStartupError",
            )
            raise DatabaseStartupError("SQLITE_ALREADY_OPEN")
        try:
            self._validate_paths()
        except DatabaseStartupError as exc:
            LOGGER.error(
                "sqlite_open_rejected",
                operation="sqlite_open",
                status="failed",
                error_code=exc.error_code,
                error_type="DatabaseStartupError",
            )
            raise
        LOGGER.info(
            "sqlite_open_started",
            operation="sqlite_open",
            status="started",
        )
        self._owner_thread_id = threading.get_ident()
        existed = self._database_path.exists()
        try:
            self._probe("sqlite_connect")
            self._connection = self._connect_factory(
                self._database_path,
                isolation_level=None,
                check_same_thread=True,
            )
        except Exception as exc:
            self._connection = None
            LOGGER.error(
                "sqlite_connect_failed",
                operation="sqlite_connect",
                status="failed",
                error_code="SQLITE_CONNECT_FAILED",
                error_type=type(exc).__name__,
            )
            raise DatabaseStartupError("SQLITE_CONNECT_FAILED") from None

        try:
            connection = self._require_connection()
            foreign_keys = self._set_and_read_pragma(connection, "foreign_keys", "ON", 1)
            journal_mode = self._set_and_read_pragma(connection, "journal_mode", "WAL", "wal")
            synchronous = self._set_and_read_pragma(connection, "synchronous", "FULL", 2)
            ledger = self._read_ledger(connection)
            if ledger and len(ledger) < len(MIGRATION_NAMES):
                self._backup_existing_database(connection)
            elif not existed:
                self._probe("backup:not_applicable")
            applied = self._migrate(connection, ledger)
            readiness = DatabaseReadiness(
                owner_thread_id=threading.get_ident(),
                foreign_keys=int(foreign_keys),
                journal_mode=str(journal_mode),
                synchronous=int(synchronous),
                schema_version=len(MIGRATION_NAMES),
                applied_migrations=tuple(applied),
            )
            LOGGER.info(
                "sqlite_open_completed",
                operation="sqlite_open",
                status="success",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                schema_version=readiness.schema_version,
                migration_count=len(applied),
            )
            return readiness
        except DatabaseStartupError as exc:
            LOGGER.error(
                "sqlite_open_failed",
                operation="sqlite_open",
                status="failed",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                error_code=exc.error_code,
                error_type="DatabaseStartupError",
            )
            self.close()
            raise
        except Exception as exc:
            LOGGER.error(
                "sqlite_open_failed",
                operation="sqlite_open",
                status="failed",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                error_type=type(exc).__name__,
            )
            self.close()
            raise DatabaseStartupError("SQLITE_MIGRATION_FAILED") from None

    @overload
    def _set_and_read_pragma(
        self,
        connection: sqlite3.Connection,
        name: str,
        value: str,
        expected: int,
    ) -> int: ...

    @overload
    def _set_and_read_pragma(
        self,
        connection: sqlite3.Connection,
        name: str,
        value: str,
        expected: str,
    ) -> str: ...

    def _set_and_read_pragma(
        self,
        connection: sqlite3.Connection,
        name: str,
        value: str,
        expected: int | str,
    ) -> int | str:
        """逐项校验 PRAGMA 的真实读回类型和值，不信任 setter 或隐式转换。"""
        self._probe(f"pragma_{name}_set")
        connection.execute(f"PRAGMA {name}={value}")
        self._probe(f"pragma_{name}_read")
        actual = connection.execute(f"PRAGMA {name}").fetchone()[0]
        # SQLite adapter 或测试替身若漂移读回类型，即使宽松相等也必须阻断就绪发布。
        if isinstance(expected, str):
            if not isinstance(actual, str) or actual.casefold() != expected:
                raise DatabaseStartupError("SQLITE_PRAGMA_READBACK_FAILED")
            return actual
        if isinstance(actual, bool) or not isinstance(actual, int) or actual != expected:
            raise DatabaseStartupError("SQLITE_PRAGMA_READBACK_FAILED")
        return actual

    def _migration_specs(self) -> tuple[tuple[int, str, bytes, str], ...]:
        """以迁移文件原始字节计算账本摘要，换行或编码变化也视为漂移。"""
        directory = Path(__file__).with_name("migrations")
        specs: list[tuple[int, str, bytes, str]] = []
        for version, name in enumerate(MIGRATION_NAMES, start=1):
            raw = (directory / name).read_bytes()
            specs.append((version, name, raw, "sha256:" + hashlib.sha256(raw).hexdigest()))
        return tuple(specs)

    @staticmethod
    def _declared_object_names(raw: bytes) -> frozenset[str]:
        """只从仓库冻结 DDL 提取对象名，避免为漂移检查另开 SQLite 连接。"""
        pattern = re.compile(
            rb"\bCREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER)\s+"
            rb"(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
            re.IGNORECASE,
        )
        return frozenset(match.group(1).decode("ascii") for match in pattern.finditer(raw))

    def _read_ledger(self, connection: sqlite3.Connection) -> tuple[tuple[int, str, str], ...]:
        """现有库必须是合法连续前缀，且每行 name/raw-byte checksum 精确相等。"""
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        objects = frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table','index','trigger','view') "
                "AND name NOT LIKE 'sqlite_%' AND name IS NOT NULL"
            )
        )
        if table_exists is None:
            if objects:
                raise DatabaseStartupError("MIGRATION_UNREGISTERED_SCHEMA")
            return ()
        rows = tuple(connection.execute("SELECT version,name,checksum FROM schema_migrations ORDER BY version"))
        specs = self._migration_specs()
        if rows and int(rows[-1][0]) > len(specs):
            raise DatabaseStartupError("MIGRATION_FUTURE_VERSION")
        if tuple(int(row[0]) for row in rows) != tuple(range(1, len(rows) + 1)):
            raise DatabaseStartupError("MIGRATION_GAP")
        # 合法旧库只包含迁移前缀；不得用全部 specs 做 strict zip。
        for row, expected in zip(rows, specs[: len(rows)], strict=True):
            version, name, checksum = row
            expected_version, expected_name, _raw, expected_checksum = expected
            if (version, name) != (expected_version, expected_name):
                raise DatabaseStartupError("MIGRATION_NAME_MISMATCH")
            if checksum != expected_checksum:
                raise DatabaseStartupError("MIGRATION_CHECKSUM_MISMATCH")
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != len(rows):
            raise DatabaseStartupError("MIGRATION_UNREGISTERED_SCHEMA")
        expected_objects = {"schema_migrations"}
        for _version, _name, raw, _checksum in specs[: len(rows)]:
            expected_objects.update(self._declared_object_names(raw))
        if objects != expected_objects:
            raise DatabaseStartupError("MIGRATION_UNREGISTERED_SCHEMA")
        return rows

    def _backup_existing_database(self, source: sqlite3.Connection) -> None:
        """用 SQLite backup API 捕获 active WAL，验证后原子保留一个 opaque 文件。"""
        started = time.monotonic()
        final: Path | None = None
        provisional: Path | None = None
        target: sqlite3.Connection | None = None
        try:
            self._probe("backup:start")
            LOGGER.info(
                "sqlite_backup_started",
                operation="sqlite_backup",
                status="started",
            )
            self._backup_root.mkdir(parents=True, exist_ok=True)
            final = self._backup_root / f"{uuid.uuid4().hex}.sqlite3"
            provisional = self._backup_root / f".{uuid.uuid4().hex}.provisional"
            target = sqlite3.connect(provisional, isolation_level=None)
            source.backup(target)
            self._probe("backup:created")
            target.close()
            target = None
            # Windows 的 FlushFileBuffers 要求可写句柄；只读 fd 会以 EBADF 拒绝，
            # 因此用 r+b 打开已完成的 provisional，但不再写入其内容。
            with provisional.open("r+b") as handle:
                os.fsync(handle.fileno())
            self._probe("backup:flushed")
            # Connection context manager 只提交/回滚而不关闭句柄；Windows rename 前
            # 必须显式 close 校验连接，否则 provisional 仍被占用。
            with closing(sqlite3.connect(provisional)) as check:
                if check.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise sqlite3.DatabaseError("backup integrity failed")
            self._probe("backup:verified")
            provisional.replace(final)
            LOGGER.info(
                "sqlite_backup_completed",
                operation="sqlite_backup",
                status="success",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                artifact_count=1,
            )
        except Exception as exc:
            if target is not None:
                target.close()
            if provisional is not None:
                provisional.unlink(missing_ok=True)
            if final is not None:
                final.unlink(missing_ok=True)
            LOGGER.error(
                "sqlite_backup_failed",
                operation="sqlite_backup",
                status="failed",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                error_type=type(exc).__name__,
            )
            raise DatabaseStartupError("SQLITE_BACKUP_FAILED") from None

    @staticmethod
    def _statements(raw: bytes) -> tuple[str, ...]:
        """用 SQLite complete_statement 切分 SQL，保留 trigger body 为单条语句。"""
        text = raw.decode("utf-8")
        buffer = ""
        statements: list[str] = []
        for character in text:
            buffer += character
            if character == ";" and sqlite3.complete_statement(buffer):
                statements.append(buffer.strip().rstrip(";").strip())
                buffer = ""
        if buffer.strip():
            raise sqlite3.DatabaseError("incomplete migration")
        return tuple(statements)

    def _migrate(
        self,
        connection: sqlite3.Connection,
        ledger: tuple[tuple[int, str, str], ...],
    ) -> tuple[str, ...]:
        """所有 pending DDL、账本、完整性检查和 user_version 在一个 BEGIN IMMEDIATE 中执行。"""
        specs = self._migration_specs()
        pending = specs[len(ledger) :]
        if not pending:
            if connection.execute("PRAGMA user_version").fetchone() != (len(specs),):
                raise DatabaseStartupError("MIGRATION_UNREGISTERED_SCHEMA")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise DatabaseStartupError("SQLITE_MIGRATION_FAILED")
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise DatabaseStartupError("SQLITE_MIGRATION_FAILED")
            return ()

        self._probe("migration_transaction:before_begin_immediate")
        connection.execute("BEGIN IMMEDIATE")
        try:
            if not ledger:
                self._probe("migration_ledger:before_create")
                connection.execute(SCHEMA_MIGRATIONS_SQL)
                self._probe("migration_ledger:after_create")
            for version, name, raw, checksum in pending:
                for ordinal, statement in enumerate(self._statements(raw), start=1):
                    self._probe(f"migration:{name}:statement:{ordinal}:before")
                    # migration 由仓库冻结文件读取，业务值绝不进入 SQL 文本。
                    connection.execute(statement)
                    self._probe(f"migration:{name}:statement:{ordinal}:after")
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)",
                    (version, name, checksum, datetime.now(UTC).isoformat()),
                )
                self._probe(f"migration:{name}:after_ledger")
            self._probe("migration_transaction:before_foreign_key_check")
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            self._probe("migration_transaction:after_foreign_key_check")
            self._probe("migration_transaction:before_integrity_check")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            self._probe("migration_transaction:after_integrity_check")
            if foreign_key_rows or integrity != ("ok",):
                raise sqlite3.IntegrityError("migration integrity failed")
            connection.execute(f"PRAGMA user_version={len(specs)}")
            self._probe("migration_transaction:before_commit")
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        LOGGER.info(
            "sqlite_migration_complete",
            operation="sqlite_migrate",
            status="success",
            migration_count=len(pending),
        )
        return tuple(name for _version, name, _raw, _checksum in pending)

    def new_unit_of_work(self) -> SqliteUnitOfWork:
        """为当前 owner 连接构造新的显式事务 UoW。"""
        from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork

        return SqliteUnitOfWork(self._require_connection(), failure_probe=self._probe)

    def poison(self) -> None:
        """将当前连接标为不可复用；用于不可恢复 writer 故障。"""
        self._poisoned = True

    def close(self) -> None:
        """关闭 owner 连接；poisoned outcome unknown 不得被伪写成已回滚。"""
        connection = self._connection
        try:
            if connection is not None:
                try:
                    if connection.in_transaction and not self._poisoned:
                        connection.execute("ROLLBACK")
                finally:
                    connection.close()
        finally:
            self._connection = None
            self._owner_thread_id = None


__all__ = ["DatabaseReadiness", "DatabaseStartupError", "SqliteDatabase"]
