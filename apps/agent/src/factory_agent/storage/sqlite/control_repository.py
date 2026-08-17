"""Control command 与 receipt event 的追加型 SQLite 仓储。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from factory_agent.domain.control import ControlCommand, ControlCommandReceiptPhase
from factory_agent.errors import FactoryError
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork


@dataclass(frozen=True, slots=True)
class ControlReceiptEvent:
    """追加 receipt 的完整持久 selector。"""

    command_receipt_event_id: str
    command_id: str
    receipt_seq: int
    phase: ControlCommandReceiptPhase
    attempt_id: str | None
    state_event_id: str | None
    evidence_digest: str | None
    previous_receipt_digest: str | None
    created_at: str


class ControlReceiptRecordError(FactoryError):
    """追加 receipt 的持久类型或闭集字段损坏时抛出。"""

    error_code = "INVALID_CONTROL_RECEIPT_RECORD"


def _receipt_from_record(record: Mapping[str, object]) -> ControlReceiptEvent:
    """把 SQLite 行收敛为闭集 receipt 值对象。"""
    receipt_seq = record["receipt_seq"]
    phase = record["phase"]
    if type(receipt_seq) is not int or not isinstance(phase, str):
        raise ControlReceiptRecordError("control receipt persistence type is invalid")
    return ControlReceiptEvent(
        command_receipt_event_id=str(record["command_receipt_event_id"]),
        command_id=str(record["command_id"]),
        receipt_seq=receipt_seq,
        phase=ControlCommandReceiptPhase(phase),
        attempt_id=None if record["attempt_id"] is None else str(record["attempt_id"]),
        state_event_id=None if record["state_event_id"] is None else str(record["state_event_id"]),
        evidence_digest=None if record["evidence_digest"] is None else str(record["evidence_digest"]),
        previous_receipt_digest=(
            None if record["previous_receipt_digest"] is None else str(record["previous_receipt_digest"])
        ),
        created_at=str(record["created_at"]),
    )


class SqliteControlRepository:
    """仅使用 UoW owner connection；所有值参数绑定，表结构不接受业务输入。"""

    def __init__(self, unit_of_work: SqliteUnitOfWork) -> None:
        self._connection = unit_of_work._connection

    @staticmethod
    def _row(cursor: object, raw: tuple[object, ...] | None) -> dict[str, object] | None:
        """把 sqlite cursor row 转为稳定字典。"""
        if raw is None:
            return None
        description = getattr(cursor, "description")
        return dict(zip((item[0] for item in description), raw, strict=True))

    def get_by_request_id(self, request_id: str) -> ControlCommand | None:
        """按全局唯一 requestId 读取已接受命令。"""
        cursor = self._connection.execute("SELECT * FROM control_commands WHERE request_id=?", (request_id,))
        record = self._row(cursor, cursor.fetchone())
        return None if record is None else ControlCommand.from_record(record)

    def get_command(self, command_id: str) -> ControlCommand | None:
        """按 command identity 读取命令。"""
        cursor = self._connection.execute("SELECT * FROM control_commands WHERE command_id=?", (command_id,))
        record = self._row(cursor, cursor.fetchone())
        return None if record is None else ControlCommand.from_record(record)

    def append_command(self, command: ControlCommand) -> None:
        """追加不可变命令；唯一键冲突由外层事务 fail closed。"""
        fields = tuple(ControlCommand.__dataclass_fields__)
        values = tuple(
            getattr(command, field).value if hasattr(getattr(command, field), "value") else getattr(command, field)
            for field in fields
        )
        columns = tuple(
            {
                "command_id": "command_id",
                "run_id": "run_id",
                "command_seq": "command_seq",
                "request_id": "request_id",
                "command_type": "command_type",
                "actor_id": "actor_id",
                "expected_state_version": "expected_state_version",
                "accepted_state_version": "accepted_state_version",
                "issued_at": "issued_at",
                "acknowledged_attempt_id": "acknowledged_attempt_id",
                "reason_digest": "reason_digest",
            }[field]
            for field in fields
        )
        quoted = ",".join(f'"{column}"' for column in columns)
        placeholders = ",".join("?" for _ in columns)
        self._connection.execute(
            f'INSERT INTO "control_commands" ({quoted}) VALUES ({placeholders})',  # noqa: S608
            values,
        )

    def list_receipts(self, command_id: str) -> tuple[ControlReceiptEvent, ...]:
        """按 receipt_seq 返回不可变追加链。"""
        cursor = self._connection.execute(
            "SELECT * FROM control_command_receipt_events WHERE command_id=? ORDER BY receipt_seq",
            (command_id,),
        )
        columns = tuple(item[0] for item in cursor.description)
        return tuple(_receipt_from_record(dict(zip(columns, row, strict=True))) for row in cursor.fetchall())

    def append_receipt(
        self,
        *,
        receipt_id: str,
        command_id: str,
        phase: ControlCommandReceiptPhase,
        attempt_id: str | None,
        state_event_id: str | None,
        evidence_digest: str | None,
        created_at: str,
    ) -> ControlReceiptEvent:
        """基于上一完整 receipt 摘要追加后继，不原位修改历史事实。"""
        command = self.get_command(command_id)
        if command is None:
            raise ControlReceiptRecordError("control command does not exist")
        receipts = self.list_receipts(command_id)
        try:
            receipt_phase = ControlCommandReceiptPhase(phase)
        except (TypeError, ValueError) as exc:
            raise ControlReceiptRecordError("control receipt phase is invalid") from exc
        if not receipts:
            if receipt_phase is not ControlCommandReceiptPhase.ACKNOWLEDGED:
                raise ControlReceiptRecordError("control receipt chain must start with acknowledgement")
        elif len(receipts) == 1 and receipts[0].phase is ControlCommandReceiptPhase.ACKNOWLEDGED:
            if receipt_phase not in {ControlCommandReceiptPhase.COMPLETED, ControlCommandReceiptPhase.FAILED}:
                raise ControlReceiptRecordError("control receipt terminal phase is invalid")
            if (
                receipts[0].attempt_id != command.acknowledged_attempt_id
                or attempt_id != command.acknowledged_attempt_id
            ):
                raise ControlReceiptRecordError("control receipt attempt lineage is inconsistent")
        else:
            # ACK 后只允许一次终态 receipt；COMPLETED/FAILED 后的竞争追加必须 fail closed。
            raise ControlReceiptRecordError("control receipt chain is already terminal")
        if attempt_id != command.acknowledged_attempt_id:
            raise ControlReceiptRecordError("control acknowledgement attempt lineage is inconsistent")
        previous_digest = None
        if receipts:
            previous = receipts[-1]
            previous_payload = {
                field: getattr(previous, field).value
                if hasattr(getattr(previous, field), "value")
                else getattr(previous, field)
                for field in ControlReceiptEvent.__dataclass_fields__
            }
            previous_digest = "sha256:" + hashlib.sha256(canonicalize(previous_payload)).hexdigest()
        receipt = ControlReceiptEvent(
            command_receipt_event_id=receipt_id,
            command_id=command_id,
            receipt_seq=len(receipts),
            phase=receipt_phase,
            attempt_id=attempt_id,
            state_event_id=state_event_id,
            evidence_digest=evidence_digest,
            previous_receipt_digest=previous_digest,
            created_at=created_at,
        )
        self._connection.execute(
            "INSERT INTO control_command_receipt_events("
            "command_receipt_event_id,command_id,receipt_seq,phase,attempt_id,state_event_id,"
            "evidence_digest,previous_receipt_digest,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                receipt.command_receipt_event_id,
                receipt.command_id,
                receipt.receipt_seq,
                receipt.phase.value,
                receipt.attempt_id,
                receipt.state_event_id,
                receipt.evidence_digest,
                receipt.previous_receipt_digest,
                receipt.created_at,
            ),
        )
        return receipt


__all__ = ["ControlReceiptEvent", "ControlReceiptRecordError", "SqliteControlRepository"]
