"""Task 1 四份 SQLite migration 的结构与数据库约束 RED 测试。

测试直接执行 migration 文件来审计 DDL；连接生命周期、显式事务与失败注入由
``test_unit_of_work.py`` 和 coordinator 测试覆盖，避免用测试 helper 冒充生产 runner。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from factory_agent.policy.canonical_json import canonicalize

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_DIR = REPOSITORY_ROOT / "apps/agent/src/factory_agent/storage/sqlite/migrations"
MIGRATION_NAMES = (
    "0001_workflow.sql",
    "0002_event_store.sql",
    "0003_auth_resources.sql",
    "0004_control_budget.sql",
)

SHA_ZERO = "sha256:" + "0" * 64
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
GIT_A = "a" * 40

# §11 selector 列必须原样存在；只有契约已冻结的 opaque JCS 才能增加 schema/canonical 列。
# projects 尚未冻结项目契约，因此 Task 1 只保留 identity，不能提前发明 factory.project.v1。
EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects": ("project_id",),
    "tasks": (
        "task_id",
        "project_id",
        "lifecycle",
        "target_stage",
        "achieved_stage",
        "active_plan_revision_id",
        "active_run_id",
        "state_version",
        "outcome_version",
        "intake_schema_id",
        "intake_schema_version",
        "canonical_intake",
        "intake_digest",
    ),
    "plan_revisions": (
        "plan_revision_id",
        "task_id",
        "spec_revision",
        "parent_revision_id",
        "intent_authorization_id",
        "semantic_plan_hash",
        "plan_revision_digest",
        "dag_version",
        "node_capability_map_version",
        "stage_capability_map_version",
        "created_at",
        "run_spec_schema_id",
        "run_spec_schema_version",
        "canonical_run_spec",
        "plan_revision_schema_id",
        "plan_revision_schema_version",
        "canonical_plan_revision",
    ),
    "phase_barriers": (
        "barrier_id",
        "run_id",
        "plan_revision_id",
        "business_phase",
        "barrier_ordinal",
        "required_node_set_digest",
        "settle_timeout_ms",
        "settle_deadline_at",
        "pass_predicate_id",
        "settled",
        "passed",
        "gate_digest",
        "state_version",
    ),
    "runs": (
        "run_id",
        "task_id",
        "desired_state",
        "observed_state",
        "phase",
        "active_barrier_id",
        "dag_version",
        "run_cursor",
        "durable_cursor",
        "control_command_seq",
        "repair_loop_used",
        "auto_replan_used",
        "requires_user_action",
        "block_reason_code",
        "budget_accumulated_ms",
        "budget_clock_state",
        "budget_clock_boot_id",
        "budget_clock_monotonic_ns",
        "budget_clock_wall_time",
        "budget_suspension_reason",
        "executor_id",
        "host_id",
        "runtime",
        "protocol_version",
        "recovery_target_phase",
        "state_version",
    ),
    "steps": (
        "step_id",
        "run_id",
        "plan_revision_id",
        "barrier_id",
        "logical_node_id",
        "business_phase",
        "node_type",
        "required",
        "side_effect_class",
        "phase",
        "outcome",
        "dependency_hash",
        "required_artifacts_digest",
        "success_predicate_id",
        "timeout_ms",
        "retry_policy_id",
        "idempotency_key",
        "state_version",
    ),
    "attempts": (
        "attempt_id",
        "step_id",
        "supersedes_attempt_id",
        "phase",
        "outcome",
        "executor_id",
        "process_session_id",
        "pid",
        "process_start_time",
        "job_object_id",
        "wsl_distro",
        "container_id",
        "image_digest",
        "exit_code",
        "termination_reason",
        "fencing_token",
        "control_epoch",
        "accepted_control_command_seq",
        "interrupt_command_id",
        "drain_state",
        "started_at",
        "ended_at",
        "state_version",
    ),
    "workspaces": (
        "workspace_id",
        "task_id",
        "project_id",
        "distro_identity",
        "linux_worktree_path",
        "linux_git_common_dir",
        "windows_source_repo",
        "factory_bare_repo",
        "base_sha",
        "candidate_sha",
        "candidate_ref",
        "checkpoint_namespace",
        "writer_lease_key",
        "import_bundle_digest",
        "export_bundle_digest",
        "cleanup_state",
        "state_version",
    ),
    "event_chain_heads": (
        "task_id",
        "committed_task_seq",
        "committed_event_digest",
        "pending_batch_id",
        "pending_writer_epoch",
        "pending_claimed_at",
        "state_version",
    ),
    "ingest_batches": (
        "prepared_batch_id",
        "writer_epoch",
        "state",
        "manifest_storage_path",
        "manifest_digest",
        "ordered_ingest_ids_digest",
        "per_task_expected_heads_digest",
        "first_batch_ordinal",
        "last_batch_ordinal",
        "event_count",
        "payload_bytes",
        "oldest_ingested_at",
        "claimed_at",
        "prepared_at",
        "committed_at",
        "recovery_state",
    ),
    "ingest_batch_task_heads": (
        "prepared_batch_id",
        "task_id",
        "expected_task_seq",
        "expected_event_digest",
        "first_batch_ordinal",
        "last_batch_ordinal",
    ),
    "stream_segments": (
        "segment_id",
        "prepared_batch_id",
        "task_id",
        "run_id",
        "attempt_id",
        "stream_id",
        "storage_path",
        "source_span_summary",
        "sanitized_start",
        "sanitized_end",
        "event_count",
        "size_bytes",
        "content_digest",
        "redaction_manifest_digest",
        "predecessor_digest",
        "complete_footer_digest",
        "commit_state",
    ),
    "ingest_queue_metrics": (
        "metric_id",
        "writer_epoch",
        "observed_at",
        "metrics_schema_id",
        "metrics_schema_version",
        "canonical_metrics",
        "metrics_digest",
    ),
    "events": (
        "event_id",
        "ingest_event_id",
        "prepared_batch_id",
        "batch_ordinal",
        "task_id",
        "run_id",
        "task_seq",
        "run_seq",
        "source_seq",
        "source_mapping_precision",
        "sanitized_segment_id",
        "sanitized_start",
        "sanitized_end",
        "durability_class",
        "schema_version",
        "previous_event_digest",
        "event_digest",
        "payload_digest",
        "canonical_event",
    ),
    # AuthoritativeStateEventV1 是 f67 冻结的独立 UoW lane，不伪造 Task 7 batch/segment/attempt。
    "authoritative_state_events": (
        "state_event_id",
        "schema_version",
        "task_id",
        "scope",
        "aggregate_type",
        "aggregate_id",
        "run_id",
        "step_id",
        "attempt_id",
        "previous_state_version",
        "state_version",
        "payload_digest",
        "canonical_state_event",
    ),
    "artifacts": (
        "artifact_id",
        "producer_attempt_id",
        "media_type",
        "confidentiality",
        "commit_state",
        "storage_path",
        "size_bytes",
        "digest",
        "created_at",
    ),
    "review_findings": (
        "finding_id",
        "task_id",
        "plan_revision_id",
        "candidate_sha",
        "status",
        "severity",
        "finding_schema_id",
        "finding_schema_version",
        "canonical_finding",
        "finding_digest",
    ),
    "intent_authorizations": (
        "intent_authorization_id",
        "task_id",
        "user_id",
        "requirement_digest",
        "project_id",
        "repository_id",
        "repository_binding_digest",
        "baseline_digest",
        "target_stage",
        "stage_capability_map_version",
        "allowed_capability_set_digest",
        "target_binding_digest",
        "risk_ceiling",
        "estimated_cost_alert_digest",
        "autonomous_execution_budget_ms",
        "repair_loop_limit",
        "auto_replan_limit",
        "attempt_limit",
        "issued_at",
        "expires_at",
        "revoked_at",
        "revoke_reason",
        "contract_schema_id",
        "contract_schema_version",
        "canonical_contract",
        "canonical_contract_digest",
    ),
    "execution_authorizations": (
        "execution_authorization_id",
        "intent_authorization_id",
        "plan_revision_id",
        "semantic_plan_hash",
        "plan_revision_digest",
        "stage_capability_map_version",
        "stage_capability_map_digest",
        "node_capability_map_version",
        "node_capability_map_digest",
        "run_id",
        "step_id",
        "attempt_id",
        "node_type",
        "executor_id",
        "resource_fingerprint",
        "capability_scope_digest",
        "idempotency_key",
        "input_bindings",
        "action_capability",
        "action_policy_snapshot_digest",
        "fencing_token",
        "control_epoch",
        "accepted_control_command_seq",
        "max_uses",
        "consumption_state",
        "issued_at",
        "expires_at",
        "revoked_at",
        "revoke_reason",
        "contract_schema_id",
        "contract_schema_version",
        "canonical_contract",
        "canonical_contract_digest",
    ),
    "resource_leases": (
        "resource_key",
        "owner_executor_id",
        "fencing_token",
        "control_epoch",
        "acquired_at",
        "heartbeat_at",
        "expires_at",
        "state_version",
    ),
    "actions": (
        "action_id",
        "action_type",
        "idempotency_key",
        "request_digest",
        "resource_fingerprint",
        "current_phase",
        "state_version",
    ),
    "action_receipt_events": (
        "receipt_event_id",
        "action_id",
        "receipt_seq",
        "phase",
        "attempt_id",
        "execution_authorization_id",
        "executor_id",
        "fencing_token",
        "control_epoch",
        "control_command_seq",
        "remote_identity",
        "result_digest",
        "evidence_digest",
        "previous_receipt_digest",
        "created_at",
    ),
    "target_guards": (
        "resource_fingerprint",
        "guard_state",
        "reason_code",
        "evidence_digest",
        "created_by_release_id",
        "created_at",
        "cleared_by",
        "cleared_at",
        "clear_receipt_digest",
        "state_version",
    ),
    "notification_actions": (
        "notification_action_id",
        "task_id",
        "outcome_version",
        "notification_kind",
        "channel",
        "provider_tag",
        "payload_digest",
        "current_phase",
        "state_version",
    ),
    "notification_receipt_events": (
        "notification_receipt_event_id",
        "notification_action_id",
        "receipt_seq",
        "phase",
        "attempt_no",
        "provider_identity",
        "result_digest",
        "previous_receipt_digest",
        "created_at",
    ),
    "credential_refs": (
        "credential_id",
        "credential_type",
        "profile_scope",
        "resource_fingerprint",
        "expires_at",
        "contract_schema_id",
        "contract_schema_version",
        "canonical_contract",
        "canonical_contract_digest",
    ),
    "audit_records": (
        "audit_id",
        "occurred_at",
        "operation",
        "outcome",
        "record_schema_id",
        "record_schema_version",
        "canonical_redacted_record",
        "record_digest",
    ),
    "control_commands": (
        "command_id",
        "run_id",
        "command_seq",
        "request_id",
        "command_type",
        "actor_id",
        "expected_state_version",
        "accepted_state_version",
        "issued_at",
        "acknowledged_attempt_id",
        "reason_digest",
    ),
    "control_command_receipt_events": (
        "command_receipt_event_id",
        "command_id",
        "receipt_seq",
        "phase",
        "attempt_id",
        "state_event_id",
        "evidence_digest",
        "previous_receipt_digest",
        "created_at",
    ),
    "budget_clock_events": (
        "run_id",
        "clock_seq",
        "transition",
        "suspension_reason",
        "boot_id",
        "monotonic_ns",
        "wall_time",
        "state_event_id",
        "previous_clock_digest",
        "clock_digest",
    ),
    # §12.5 要求持久 epoch 而 §11 未列承载表；这是审计批准并明确标注的 v1 新设计。
    "control_plane_epochs": (
        "singleton_id",
        "writer_epoch",
        "control_epoch",
        "state_version",
    ),
}

EXPECTED_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    table: (columns[0],) for table, columns in EXPECTED_COLUMNS.items()
}
EXPECTED_PRIMARY_KEYS["ingest_batch_task_heads"] = ("prepared_batch_id", "task_id")
EXPECTED_PRIMARY_KEYS["budget_clock_events"] = ("run_id", "clock_seq")

EXPECTED_UNIQUES: dict[str, tuple[tuple[str, ...], ...]] = {
    "plan_revisions": (("plan_revision_digest",), ("task_id", "spec_revision")),
    "phase_barriers": (("run_id", "plan_revision_id", "business_phase", "barrier_ordinal"),),
    "steps": (("run_id", "plan_revision_id", "logical_node_id"),),
    "events": (
        ("ingest_event_id",),
        ("prepared_batch_id", "batch_ordinal"),
        ("task_id", "task_seq"),
        ("event_digest",),
    ),
    "authoritative_state_events": (("aggregate_type", "aggregate_id", "state_version"),),
    "actions": (("action_type", "idempotency_key"),),
    "action_receipt_events": (("action_id", "receipt_seq"),),
    "notification_actions": (("task_id", "outcome_version", "notification_kind"),),
    "notification_receipt_events": (("notification_action_id", "receipt_seq"),),
    "control_commands": (("run_id", "command_seq"), ("request_id",)),
    "control_command_receipt_events": (("command_id", "receipt_seq"),),
}

RECOVERY_INDEXES: dict[str, tuple[tuple[str, ...], ...]] = {
    "ingest_batches": (("state", "writer_epoch"),),
    "event_chain_heads": (("pending_batch_id",),),
    "events": (("run_id", "run_seq"),),
    "execution_authorizations": (
        ("run_id",),
        ("plan_revision_id",),
        ("attempt_id",),
        ("consumption_state",),
        ("expires_at",),
    ),
    "intent_authorizations": (("task_id",), ("expires_at",)),
    "resource_leases": (("expires_at",),),
    "actions": (("resource_fingerprint", "current_phase"),),
    "target_guards": (("guard_state",),),
    "audit_records": (("occurred_at",),),
}


@dataclass(frozen=True)
class ColumnSpec:
    """冻结单列的 SQLite declared type、空值、默认值和复合主键序号。"""

    name: str
    declared_type: str
    not_null: bool
    default_sql: str | None
    primary_key_position: int


@dataclass(frozen=True)
class ForeignKeySpec:
    """冻结 child→parent selector 以及更新/删除动作。"""

    columns: tuple[str, ...]
    target_table: str
    target_columns: tuple[str, ...]
    on_update: str = "RESTRICT"
    on_delete: str = "RESTRICT"
    match: str = "NONE"


@dataclass(frozen=True)
class UniqueSpec:
    """冻结非主键 UNIQUE；列顺序必须完整相等，禁止仅匹配前缀。"""

    columns: tuple[str, ...]


@dataclass(frozen=True)
class IndexTermSpec:
    """冻结 ``index_xinfo`` 的完整项，包含辅助 rowid 项。"""

    cid: int
    name: str | None
    collation: str
    descending: bool
    key: bool


@dataclass(frozen=True)
class IndexSpec:
    """冻结索引结构、来源、谓词与完整规范 SQL；自动索引不依赖名称。"""

    name: str | None
    terms: tuple[IndexTermSpec, ...]
    unique: bool
    origin: str
    partial: bool
    where_tokens: tuple[str, ...] = ()
    sql_tokens: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CheckSpec:
    """冻结有业务意义的命名 CHECK 及其规范化表达式。"""

    name: str
    expression: str


@dataclass(frozen=True)
class TriggerSpec:
    """冻结触发器完整规范 SQL，不接受仅 body token 相似。"""

    name: str
    sql: str


@dataclass(frozen=True)
class TableSpec:
    """聚合一张表的 exact DDL oracle；任何多余约束也会导致失败。"""

    columns: tuple[ColumnSpec, ...]
    foreign_keys: tuple[ForeignKeySpec, ...] = ()
    uniques: tuple[UniqueSpec, ...] = ()
    explicit_indexes: tuple[IndexSpec, ...] = ()
    checks: tuple[CheckSpec, ...] = ()
    triggers: tuple[TriggerSpec, ...] = ()
    without_rowid: bool = False
    strict: bool = True


# SQLite 中这些字段是计数器、布尔投影、版本或序号；其余 primitive selector 默认为 TEXT。
INTEGER_COLUMN_NAMES = frozenset(
    {
        "accepted_control_command_seq",
        "accepted_state_version",
        "attempt_limit",
        "attempt_no",
        "auto_replan_limit",
        "auto_replan_used",
        "autonomous_execution_budget_ms",
        "barrier_ordinal",
        "batch_ordinal",
        "budget_accumulated_ms",
        "budget_clock_monotonic_ns",
        "clock_seq",
        "committed_task_seq",
        "command_seq",
        "contract_schema_version",
        "control_command_seq",
        "control_epoch",
        "dag_version",
        "durable_cursor",
        "event_count",
        "exit_code",
        "expected_state_version",
        "expected_task_seq",
        "fencing_token",
        "finding_schema_version",
        "first_batch_ordinal",
        "intake_schema_version",
        "last_batch_ordinal",
        "max_uses",
        "metrics_schema_version",
        "monotonic_ns",
        "outcome_version",
        "passed",
        "payload_bytes",
        "pending_writer_epoch",
        "pid",
        "plan_revision_schema_version",
        "previous_state_version",
        "receipt_seq",
        "record_schema_version",
        "repair_loop_limit",
        "repair_loop_used",
        "required",
        "requires_user_action",
        "run_cursor",
        "run_seq",
        "run_spec_schema_version",
        "sanitized_end",
        "sanitized_start",
        "schema_version",
        "settle_timeout_ms",
        "settled",
        "singleton_id",
        "size_bytes",
        "source_seq",
        "spec_revision",
        "state_version",
        "task_seq",
        "timeout_ms",
        "writer_epoch",
    }
)

# snapshotRef/inputBindings 等闭合对象与 canonical contract 必须保存 JCS bytes，而非可漂移 JSON 文本。
BLOB_COLUMN_PAIRS = frozenset(
    {
        (table, column)
        for table, columns in EXPECTED_COLUMNS.items()
        for column in columns
        if column.startswith("canonical_")
    }
    | {
        ("stream_segments", "source_span_summary"),
        ("intent_authorizations", "requirement_digest"),
        ("intent_authorizations", "repository_binding_digest"),
        ("intent_authorizations", "baseline_digest"),
        ("intent_authorizations", "allowed_capability_set_digest"),
        ("intent_authorizations", "target_binding_digest"),
        ("intent_authorizations", "estimated_cost_alert_digest"),
        ("execution_authorizations", "semantic_plan_hash"),
        ("execution_authorizations", "plan_revision_digest"),
        ("execution_authorizations", "stage_capability_map_digest"),
        ("execution_authorizations", "node_capability_map_digest"),
        ("execution_authorizations", "resource_fingerprint"),
        ("execution_authorizations", "capability_scope_digest"),
        ("execution_authorizations", "idempotency_key"),
        ("execution_authorizations", "input_bindings"),
        ("execution_authorizations", "action_policy_snapshot_digest"),
        ("action_receipt_events", "remote_identity"),
    }
)

# 只有明确表示“尚无值”的投影/终态事实允许 NULL；所有 identity 和 selector 均 fail closed。
NULLABLE_COLUMN_PAIRS = frozenset(
    {
        ("tasks", "active_plan_revision_id"),
        ("tasks", "active_run_id"),
        ("plan_revisions", "parent_revision_id"),
        ("phase_barriers", "settle_deadline_at"),
        ("phase_barriers", "gate_digest"),
        ("runs", "active_barrier_id"),
        ("runs", "run_cursor"),
        ("runs", "durable_cursor"),
        ("runs", "block_reason_code"),
        ("runs", "budget_clock_state"),
        ("runs", "budget_clock_boot_id"),
        ("runs", "budget_clock_monotonic_ns"),
        ("runs", "budget_clock_wall_time"),
        ("runs", "budget_suspension_reason"),
        ("runs", "executor_id"),
        ("runs", "host_id"),
        ("runs", "runtime"),
        ("runs", "recovery_target_phase"),
        ("attempts", "supersedes_attempt_id"),
        ("attempts", "executor_id"),
        ("attempts", "process_session_id"),
        ("attempts", "pid"),
        ("attempts", "process_start_time"),
        ("attempts", "job_object_id"),
        ("attempts", "wsl_distro"),
        ("attempts", "container_id"),
        ("attempts", "image_digest"),
        ("attempts", "exit_code"),
        ("attempts", "termination_reason"),
        ("attempts", "interrupt_command_id"),
        ("attempts", "started_at"),
        ("attempts", "ended_at"),
        ("workspaces", "candidate_sha"),
        ("workspaces", "import_bundle_digest"),
        ("workspaces", "export_bundle_digest"),
        ("event_chain_heads", "pending_batch_id"),
        ("event_chain_heads", "pending_writer_epoch"),
        ("event_chain_heads", "pending_claimed_at"),
        ("event_chain_heads", "committed_task_seq"),
        ("event_chain_heads", "committed_event_digest"),
        ("ingest_batches", "manifest_storage_path"),
        ("ingest_batches", "manifest_digest"),
        ("ingest_batches", "ordered_ingest_ids_digest"),
        ("ingest_batches", "per_task_expected_heads_digest"),
        ("ingest_batches", "claimed_at"),
        ("ingest_batches", "prepared_at"),
        ("ingest_batches", "committed_at"),
        ("ingest_batch_task_heads", "expected_task_seq"),
        ("ingest_batch_task_heads", "expected_event_digest"),
        ("stream_segments", "stream_id"),
        ("authoritative_state_events", "run_id"),
        ("authoritative_state_events", "step_id"),
        ("authoritative_state_events", "attempt_id"),
        ("review_findings", "candidate_sha"),
        ("intent_authorizations", "revoked_at"),
        ("intent_authorizations", "revoke_reason"),
        ("execution_authorizations", "revoked_at"),
        ("execution_authorizations", "revoke_reason"),
        ("action_receipt_events", "result_digest"),
        ("action_receipt_events", "evidence_digest"),
        ("action_receipt_events", "previous_receipt_digest"),
        ("target_guards", "reason_code"),
        ("target_guards", "evidence_digest"),
        ("target_guards", "created_by_release_id"),
        ("target_guards", "cleared_by"),
        ("target_guards", "cleared_at"),
        ("target_guards", "clear_receipt_digest"),
        ("notification_actions", "provider_tag"),
        ("notification_receipt_events", "provider_identity"),
        ("notification_receipt_events", "result_digest"),
        ("notification_receipt_events", "previous_receipt_digest"),
        ("credential_refs", "expires_at"),
        ("credential_refs", "resource_fingerprint"),
        ("control_commands", "acknowledged_attempt_id"),
        ("control_commands", "reason_digest"),
        ("control_command_receipt_events", "attempt_id"),
        ("control_command_receipt_events", "state_event_id"),
        ("control_command_receipt_events", "evidence_digest"),
        ("control_command_receipt_events", "previous_receipt_digest"),
        ("budget_clock_events", "suspension_reason"),
        ("budget_clock_events", "previous_clock_digest"),
    }
)


EXPECTED_FOREIGN_KEYS: dict[str, tuple[ForeignKeySpec, ...]] = {
    "tasks": (
        ForeignKeySpec(("project_id",), "projects", ("project_id",)),
        ForeignKeySpec(("active_plan_revision_id",), "plan_revisions", ("plan_revision_id",)),
        ForeignKeySpec(("active_run_id",), "runs", ("run_id",)),
    ),
    "plan_revisions": (
        ForeignKeySpec(("task_id",), "tasks", ("task_id",)),
        ForeignKeySpec(("parent_revision_id",), "plan_revisions", ("plan_revision_id",)),
        ForeignKeySpec(("intent_authorization_id",), "intent_authorizations", ("intent_authorization_id",)),
    ),
    "phase_barriers": (
        ForeignKeySpec(("run_id",), "runs", ("run_id",)),
        ForeignKeySpec(("plan_revision_id",), "plan_revisions", ("plan_revision_id",)),
    ),
    "runs": (
        ForeignKeySpec(("task_id",), "tasks", ("task_id",)),
        ForeignKeySpec(("active_barrier_id",), "phase_barriers", ("barrier_id",)),
    ),
    "steps": (
        ForeignKeySpec(("run_id",), "runs", ("run_id",)),
        ForeignKeySpec(("plan_revision_id",), "plan_revisions", ("plan_revision_id",)),
        ForeignKeySpec(("barrier_id",), "phase_barriers", ("barrier_id",)),
    ),
    "attempts": (
        ForeignKeySpec(("step_id",), "steps", ("step_id",)),
        ForeignKeySpec(("supersedes_attempt_id",), "attempts", ("attempt_id",)),
        ForeignKeySpec(("interrupt_command_id",), "control_commands", ("command_id",)),
    ),
    "workspaces": (
        ForeignKeySpec(("task_id",), "tasks", ("task_id",)),
        ForeignKeySpec(("project_id",), "projects", ("project_id",)),
    ),
    "event_chain_heads": (
        ForeignKeySpec(("task_id",), "tasks", ("task_id",)),
        ForeignKeySpec(("pending_batch_id",), "ingest_batches", ("prepared_batch_id",)),
    ),
    "ingest_batch_task_heads": (
        ForeignKeySpec(("prepared_batch_id",), "ingest_batches", ("prepared_batch_id",)),
        ForeignKeySpec(("task_id",), "tasks", ("task_id",)),
    ),
    "stream_segments": (
        ForeignKeySpec(("prepared_batch_id",), "ingest_batches", ("prepared_batch_id",)),
        ForeignKeySpec(("task_id",), "tasks", ("task_id",)),
        ForeignKeySpec(("run_id",), "runs", ("run_id",)),
        ForeignKeySpec(("attempt_id",), "attempts", ("attempt_id",)),
    ),
    "events": (
        ForeignKeySpec(("prepared_batch_id",), "ingest_batches", ("prepared_batch_id",)),
        ForeignKeySpec(("task_id",), "tasks", ("task_id",)),
        ForeignKeySpec(("run_id",), "runs", ("run_id",)),
        ForeignKeySpec(("sanitized_segment_id",), "stream_segments", ("segment_id",)),
    ),
    "authoritative_state_events": (
        ForeignKeySpec(("task_id",), "tasks", ("task_id",)),
        ForeignKeySpec(("run_id",), "runs", ("run_id",)),
        ForeignKeySpec(("step_id",), "steps", ("step_id",)),
        ForeignKeySpec(("attempt_id",), "attempts", ("attempt_id",)),
    ),
    "artifacts": (ForeignKeySpec(("producer_attempt_id",), "attempts", ("attempt_id",)),),
    "review_findings": (
        ForeignKeySpec(("task_id",), "tasks", ("task_id",)),
        ForeignKeySpec(("plan_revision_id",), "plan_revisions", ("plan_revision_id",)),
    ),
    "intent_authorizations": (
        ForeignKeySpec(("task_id",), "tasks", ("task_id",)),
        ForeignKeySpec(("project_id",), "projects", ("project_id",)),
    ),
    "execution_authorizations": (
        ForeignKeySpec(("intent_authorization_id",), "intent_authorizations", ("intent_authorization_id",)),
        ForeignKeySpec(("plan_revision_id",), "plan_revisions", ("plan_revision_id",)),
        ForeignKeySpec(("run_id",), "runs", ("run_id",)),
        ForeignKeySpec(("step_id",), "steps", ("step_id",)),
        ForeignKeySpec(("attempt_id",), "attempts", ("attempt_id",)),
    ),
    "action_receipt_events": (
        ForeignKeySpec(("action_id",), "actions", ("action_id",)),
        ForeignKeySpec(("attempt_id",), "attempts", ("attempt_id",)),
        ForeignKeySpec(
            ("execution_authorization_id",),
            "execution_authorizations",
            ("execution_authorization_id",),
        ),
    ),
    "notification_actions": (ForeignKeySpec(("task_id",), "tasks", ("task_id",)),),
    "notification_receipt_events": (
        ForeignKeySpec(("notification_action_id",), "notification_actions", ("notification_action_id",)),
    ),
    "control_commands": (
        ForeignKeySpec(("run_id",), "runs", ("run_id",)),
        ForeignKeySpec(("acknowledged_attempt_id",), "attempts", ("attempt_id",)),
    ),
    "control_command_receipt_events": (
        ForeignKeySpec(("command_id",), "control_commands", ("command_id",)),
        ForeignKeySpec(("attempt_id",), "attempts", ("attempt_id",)),
        ForeignKeySpec(("state_event_id",), "authoritative_state_events", ("state_event_id",)),
    ),
    "budget_clock_events": (
        ForeignKeySpec(("run_id",), "runs", ("run_id",)),
        ForeignKeySpec(("state_event_id",), "authoritative_state_events", ("state_event_id",)),
    ),
}


APPEND_ONLY_TABLES = frozenset(
    {
        "plan_revisions",
        "events",
        "authoritative_state_events",
        "action_receipt_events",
        "notification_receipt_events",
        "control_commands",
        "control_command_receipt_events",
        "budget_clock_events",
    }
)

BUSINESS_PHASES = (
    "PLANNING",
    "DESIGN_REVIEWING",
    "PREPARING_WORKSPACE",
    "IMPLEMENTING",
    "VERIFYING",
    "CODE_REVIEWING",
    "PUBLISHING_PR",
    "MERGING",
    "BUILDING_ARTIFACT",
    "DEPLOYING_STAGING",
    "ACCEPTING_STAGING",
    "DEPLOYING_PRODUCTION",
    "ACCEPTING_PRODUCTION",
    "ROLLING_BACK",
    "FINALIZING",
)
NODE_TYPES = (
    "PLAN",
    "DESIGN_REVIEW",
    "BOOTSTRAP_REPOSITORY",
    "IMPLEMENT",
    "VERIFY",
    "CODE_REVIEW",
    "ATTEST_REVIEW",
    "PUBLISH_PR",
    "MERGE",
    "BUILD_ARTIFACT",
    "DEPLOY_STAGING",
    "ACCEPT_STAGING",
    "DEPLOY_PRODUCTION",
    "ACCEPT_PRODUCTION",
    "ROLLBACK",
    "RECONCILE_TARGET",
    "RESTORE_DRILL",
)
ACTION_CAPABILITIES = (
    "acceptance.fixture.write",
    "build.exec.isolated",
    "check.publish",
    "container.inspect.scoped",
    "db.backup",
    "db.check",
    "db.migrate",
    "db.read",
    "db.restore",
    "forge.observe.scoped",
    "git.local_commit",
    "git.push",
    "http.check.scoped",
    "log.read.scoped",
    "network.egress.scoped",
    "nginx.switch",
    "pr.create",
    "pr.update",
    "registry.observe.scoped",
    "registry.push",
    "remote.observe.scoped",
    "remote.write.scoped",
    "repo.bootstrap",
    "repo.merge",
    "repo.read",
    "restore.validation.instance",
    "rollback",
    "service.restart.scoped",
    "ssh.exec.scoped",
    "target.guard.clear",
    "test.exec.isolated",
    "traffic.switch.scoped",
    "worktree.write",
)
ACTION_PHASES = ("STARTED", "COMPLETED", "ABSENT_CONFIRMED", "FAILED_RECONCILED")
NOTIFICATION_PHASES = (
    "PREPARED",
    "DISPATCH_STARTED",
    "ACCEPTED_BY_ADAPTER",
    "FAILED_KNOWN",
    "DISPATCH_UNKNOWN",
)

ENUM_CHECKS: dict[tuple[str, str], tuple[str, ...]] = {
    ("tasks", "lifecycle"): (
        "ACTIVE",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "ROLLED_BACK",
        "FAILED_NEEDS_INTERVENTION",
    ),
    ("tasks", "target_stage"): (
        "DESIGN_APPROVED",
        "CODEX_APPROVED",
        "PR_READY",
        "MERGED",
        "STAGING_ACCEPTED",
        "PRODUCTION_ACCEPTED",
    ),
    ("tasks", "achieved_stage"): (
        "NONE",
        "DESIGN_APPROVED",
        "CODEX_APPROVED",
        "PR_READY",
        "MERGED",
        "STAGING_ACCEPTED",
        "PRODUCTION_ACCEPTED",
    ),
    ("runs", "desired_state"): ("RUNNING", "PAUSED", "CANCELLED"),
    ("runs", "runtime"): ("local-windows", "wsl-docker"),
    ("runs", "observed_state"): (
        "QUEUED",
        "RUNNING",
        "PAUSING",
        "PAUSED",
        "STOPPING",
        "INTERRUPTED",
        "RECONCILING",
        "BLOCKED",
        "TERMINATED",
    ),
    ("runs", "phase"): (
        "CREATED",
        "PREFLIGHT",
        "BOOTSTRAPPING_REPOSITORY",
        "PLANNING",
        "DESIGN_REVIEWING",
        "PREPARING_WORKSPACE",
        "IMPLEMENTING",
        "VERIFYING",
        "CODE_REVIEWING",
        "PUBLISHING_PR",
        "MERGING",
        "BUILDING_ARTIFACT",
        "DEPLOYING_STAGING",
        "ACCEPTING_STAGING",
        "DEPLOYING_PRODUCTION",
        "ACCEPTING_PRODUCTION",
        "ROLLING_BACK",
        "FINALIZING",
    ),
    ("phase_barriers", "business_phase"): BUSINESS_PHASES,
    ("steps", "business_phase"): BUSINESS_PHASES,
    ("steps", "node_type"): NODE_TYPES,
    ("steps", "phase"): ("PENDING", "READY", "DISPATCHED", "RUNNING", "RECONCILING", "TERMINAL"),
    ("steps", "outcome"): (
        "NONE",
        "SUCCEEDED",
        "FAILED",
        "INTERRUPTED",
        "SKIPPED",
        "CANCELLED",
        "UNKNOWN_REMOTE_STATE",
    ),
    ("attempts", "phase"): (
        "CREATED",
        "STARTING",
        "RUNNING",
        "INTERRUPTING",
        "RECONCILING",
        "TERMINATED",
    ),
    ("attempts", "outcome"): (
        "NONE",
        "SUCCEEDED",
        "FAILED",
        "INTERRUPTED",
        "KILLED",
        "LOST",
        "UNKNOWN_REMOTE_STATE",
    ),
    ("attempts", "drain_state"): ("NONE", "DRAINING", "DRAINED"),
    ("ingest_batches", "state"): ("CLAIMED", "PREPARING", "PREPARED", "COMMITTED", "ABANDONED"),
    ("stream_segments", "commit_state"): ("COMMITTED", "QUARANTINED"),
    ("events", "source_mapping_precision"): ("byte", "field", "frame", "none"),
    ("events", "durability_class"): (
        "authoritative_state",
        "side_effect_receipt",
        "provider_source",
        "derived",
    ),
    ("authoritative_state_events", "scope"): ("TASK", "RUN", "STEP", "ATTEMPT"),
    ("authoritative_state_events", "aggregate_type"): ("TASK", "RUN", "PHASE_BARRIER", "STEP", "ATTEMPT"),
    ("artifacts", "commit_state"): ("STAGING", "COMMITTED"),
    ("intent_authorizations", "target_stage"): (
        "DESIGN_APPROVED",
        "CODEX_APPROVED",
        "PR_READY",
        "MERGED",
        "STAGING_ACCEPTED",
        "PRODUCTION_ACCEPTED",
    ),
    ("intent_authorizations", "risk_ceiling"): ("low", "medium", "high", "critical"),
    ("execution_authorizations", "node_type"): NODE_TYPES,
    ("execution_authorizations", "action_capability"): ACTION_CAPABILITIES,
    ("execution_authorizations", "consumption_state"): ("AVAILABLE", "CONSUMED"),
    ("actions", "current_phase"): ACTION_PHASES,
    ("action_receipt_events", "phase"): ACTION_PHASES,
    ("target_guards", "guard_state"): ("OPEN", "INTERVENTION_REQUIRED"),
    ("notification_actions", "current_phase"): NOTIFICATION_PHASES,
    ("notification_receipt_events", "phase"): NOTIFICATION_PHASES,
    ("credential_refs", "credential_type"): (
        "SSH_KEY",
        "REGISTRY_TOKEN",
        "API_KEY",
        "GITHUB_APP",
        "FACTORY_PROFILE_TOKEN",
    ),
    ("control_commands", "command_type"): ("SOFT_PAUSE", "IMMEDIATE_STOP", "RESUME", "CANCEL"),
    ("control_command_receipt_events", "phase"): ("ACKNOWLEDGED", "COMPLETED", "FAILED"),
    ("budget_clock_events", "transition"): ("RUNNING_STARTED", "SUSPENSION_STARTED"),
    ("budget_clock_events", "suspension_reason"): ("USER_PAUSED", "REQUIRES_USER_ACTION"),
}

BOOLEAN_COLUMN_PAIRS = frozenset(
    {
        ("phase_barriers", "settled"),
        ("phase_barriers", "passed"),
        ("runs", "requires_user_action"),
        ("steps", "required"),
    }
)


def _sql_tokens(sql: str) -> tuple[str, ...]:
    """词法规范化 SQL，同时原样保留 string literal 的大小写与转义。"""
    tokens: list[str] = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise AssertionError("SQL block comment 未闭合")
            index = end + 2
            continue
        if character == "'":
            end = index + 1
            while end < len(sql):
                if sql[end] != "'":
                    end += 1
                    continue
                if end + 1 < len(sql) and sql[end + 1] == "'":
                    end += 2
                    continue
                end += 1
                break
            else:
                raise AssertionError("SQL string literal 未闭合")
            tokens.append(sql[index:end])
            index = end
            continue
        if character in {'"', "`", "["}:
            closing = "]" if character == "[" else character
            end = index + 1
            decoded: list[str] = []
            while end < len(sql):
                if sql[end] != closing:
                    decoded.append(sql[end])
                    end += 1
                    continue
                if closing != "]" and end + 1 < len(sql) and sql[end + 1] == closing:
                    decoded.append(closing)
                    end += 2
                    continue
                end += 1
                break
            else:
                raise AssertionError("SQL quoted identifier 未闭合")
            tokens.append("".join(decoded).casefold())
            index = end
            continue
        if character.isalnum() or character in {"_", "$"}:
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] in {"_", "$"}):
                end += 1
            tokens.append(sql[index:end].casefold())
            index = end
            continue
        operator = next(
            (
                candidate
                for candidate in ("->>", ">=", "<=", "<>", "!=", "==", "||", "->")
                if sql.startswith(candidate, index)
            ),
            None,
        )
        if operator is not None:
            tokens.append(operator)
            index += len(operator)
            continue
        tokens.append(character)
        index += 1
    return tuple(tokens)


def _balanced_check_definitions(sql: str) -> dict[str, tuple[str, ...]]:
    """抽取每个命名 CHECK 的完整平衡表达式，拒绝匿名或重复定义。"""
    tokens = _sql_tokens(sql)
    definitions: dict[str, tuple[str, ...]] = {}
    check_count = 0
    cursor = 0
    while cursor < len(tokens):
        if tokens[cursor] != "check":
            cursor += 1
            continue
        check_count += 1
        assert cursor >= 2 and tokens[cursor - 2] == "constraint", "禁止匿名 CHECK"
        name = tokens[cursor - 1]
        assert cursor + 1 < len(tokens) and tokens[cursor + 1] == "(", f"{name} CHECK 缺少左括号"
        depth = 1
        end = cursor + 2
        while end < len(tokens) and depth:
            if tokens[end] == "(":
                depth += 1
            elif tokens[end] == ")":
                depth -= 1
            end += 1
        assert depth == 0, f"{name} CHECK 括号未闭合"
        assert name not in definitions, f"重复 CHECK 名称：{name}"
        definitions[name] = tokens[cursor + 2 : end - 1]
        cursor = end
    assert check_count == len(definitions), "存在未命名或重复 CHECK"
    return definitions


def _index_name(table: str, columns: tuple[str, ...]) -> str:
    """生成冻结、可读且不依赖 SQLite autoindex 名称的显式索引名。"""
    return f"idx_{table}__{'__'.join(columns)}"


def _covered_by_key(table: str, columns: tuple[str, ...]) -> bool:
    """判断 child/query selector 是否已由 PK/UNIQUE 的完整左前缀覆盖。"""
    candidate_keys = (EXPECTED_PRIMARY_KEYS[table], *EXPECTED_UNIQUES.get(table, ()))
    return any(key[: len(columns)] == columns for key in candidate_keys)


def _index_terms(table: str, columns: tuple[str, ...]) -> tuple[IndexTermSpec, ...]:
    """按 SQLite rowid 表的 ``index_xinfo`` 形状构造 key 与辅助 rowid 项。"""
    terms = [
        IndexTermSpec(
            cid=EXPECTED_COLUMNS[table].index(column),
            name=column,
            collation="BINARY",
            descending=False,
            key=True,
        )
        for column in columns
    ]
    terms.append(IndexTermSpec(cid=-1, name=None, collation="BINARY", descending=False, key=False))
    return tuple(terms)


def _explicit_index_spec(
    table: str,
    columns: tuple[str, ...],
    *,
    unique: bool = False,
    name: str | None = None,
    where: str | None = None,
) -> IndexSpec:
    """构造显式索引的名称、xinfo、partial 谓词及完整 SQL oracle。"""
    resolved_name = name or _index_name(table, columns)
    uniqueness = "UNIQUE " if unique else ""
    column_sql = ", ".join(columns)
    where_sql = "" if where is None else f" WHERE {where}"
    sql = f"CREATE {uniqueness}INDEX {resolved_name} ON {table} ({column_sql}){where_sql}"
    return IndexSpec(
        name=resolved_name,
        terms=_index_terms(table, columns),
        unique=unique,
        origin="c",
        partial=where is not None,
        where_tokens=() if where is None else _sql_tokens(where),
        sql_tokens=_sql_tokens(sql),
    )


def _index_sort_key(spec: IndexSpec) -> tuple[object, ...]:
    """为 frozen index oracle 提供不依赖 dataclass ordering 的稳定排序键。"""
    terms = tuple((term.cid, term.name or "", term.collation, term.descending, term.key) for term in spec.terms)
    return (spec.origin, spec.name or "", terms, spec.unique, spec.partial, spec.where_tokens)


def _expected_indexes(table: str) -> tuple[IndexSpec, ...]:
    """由 FK 与已冻结恢复查询生成 exact 非冗余显式索引集合。"""
    requested = [foreign_key.columns for foreign_key in EXPECTED_FOREIGN_KEYS.get(table, ())]
    requested.extend(RECOVERY_INDEXES.get(table, ()))
    columns_seen: set[tuple[str, ...]] = set()
    indexes: list[IndexSpec] = []
    for columns in requested:
        if columns in columns_seen or _covered_by_key(table, columns):
            continue
        columns_seen.add(columns)
        indexes.append(_explicit_index_spec(table, columns))
    if table == "action_receipt_events":
        indexes.append(
            _explicit_index_spec(
                table,
                ("action_id",),
                unique=True,
                name="ux_action_receipt_events__action_id__completed",
                where="phase='COMPLETED'",
            )
        )
    return tuple(sorted(indexes, key=lambda item: item.name or ""))


def _digest_checks(table: str) -> tuple[CheckSpec, ...]:
    """为 primitive sha256 selector 冻结小写、长度和字符集约束。"""
    checks: list[CheckSpec] = []
    for column in EXPECTED_COLUMNS[table]:
        if ("digest" not in column and not column.endswith("_hash")) or (table, column) in BLOB_COLUMN_PAIRS:
            continue
        if (table, column) in NULLABLE_COLUMN_PAIRS:
            expression = (
                f"{column} IS NULL OR (length({column})=71 AND substr({column},1,7)='sha256:' "
                f"AND substr({column},8) NOT GLOB '*[^0-9a-f]*')"
            )
        else:
            expression = (
                f"length({column})=71 AND substr({column},1,7)='sha256:' AND substr({column},8) NOT GLOB '*[^0-9a-f]*'"
            )
        checks.append(CheckSpec(f"ck_{table}__{column}__sha256", expression))
    return tuple(checks)


def _table_specific_checks(table: str) -> tuple[CheckSpec, ...]:
    """冻结父审裁明确要求的跨列、版本与有限数值边界。"""
    maximum = 9_007_199_254_740_991
    checks: dict[str, tuple[CheckSpec, ...]] = {
        "event_chain_heads": (
            CheckSpec(
                "ck_event_chain_heads__committed_pair",
                "(committed_task_seq IS NULL AND committed_event_digest IS NULL) OR "
                "(committed_task_seq IS NOT NULL AND committed_task_seq>=0 AND committed_event_digest IS NOT NULL)",
            ),
        ),
        "ingest_batch_task_heads": (
            CheckSpec(
                "ck_ingest_batch_task_heads__expected_pair",
                "(expected_task_seq IS NULL AND expected_event_digest IS NULL) OR "
                "(expected_task_seq IS NOT NULL AND expected_task_seq>=0 AND expected_event_digest IS NOT NULL)",
            ),
        ),
        "events": (CheckSpec("ck_events__schema_version", "schema_version=2"),),
        "authoritative_state_events": (
            CheckSpec("ck_authoritative_state_events__schema_version", "schema_version=1"),
            CheckSpec(
                "ck_authoritative_state_events__version_increment",
                "previous_state_version>=0 AND state_version=previous_state_version+1",
            ),
        ),
        "intent_authorizations": (
            CheckSpec(
                "ck_intent_authorizations__revocation_pair",
                "(revoked_at IS NULL AND revoke_reason IS NULL) OR "
                "(revoked_at IS NOT NULL AND revoke_reason IS NOT NULL AND length(revoke_reason)>0)",
            ),
            CheckSpec(
                "ck_intent_authorizations__autonomous_budget_bounds",
                f"autonomous_execution_budget_ms BETWEEN 1 AND {maximum}",
            ),
            CheckSpec(
                "ck_intent_authorizations__repair_loop_bounds",
                f"repair_loop_limit BETWEEN 0 AND {maximum}",
            ),
            CheckSpec(
                "ck_intent_authorizations__auto_replan_bounds",
                f"auto_replan_limit BETWEEN 0 AND {maximum}",
            ),
            CheckSpec(
                "ck_intent_authorizations__attempt_bounds",
                f"attempt_limit BETWEEN 1 AND {maximum}",
            ),
            CheckSpec("ck_intent_authorizations__contract_schema_version", "contract_schema_version=1"),
        ),
        "execution_authorizations": (
            CheckSpec(
                "ck_execution_authorizations__revocation_pair",
                "(revoked_at IS NULL AND revoke_reason IS NULL) OR "
                "(revoked_at IS NOT NULL AND revoke_reason IS NOT NULL AND length(revoke_reason)>0)",
            ),
            CheckSpec(
                "ck_execution_authorizations__fencing_token_bounds",
                f"fencing_token BETWEEN 1 AND {maximum}",
            ),
            CheckSpec(
                "ck_execution_authorizations__control_epoch_bounds",
                f"control_epoch BETWEEN 0 AND {maximum}",
            ),
            CheckSpec(
                "ck_execution_authorizations__command_seq_bounds",
                f"accepted_control_command_seq BETWEEN 0 AND {maximum}",
            ),
            CheckSpec(
                "ck_execution_authorizations__max_uses_bounds",
                f"max_uses BETWEEN 0 AND {maximum}",
            ),
            CheckSpec(
                "ck_execution_authorizations__consumption_uses",
                "(consumption_state='AVAILABLE' AND max_uses>=1) OR (consumption_state='CONSUMED' AND max_uses=0)",
            ),
            CheckSpec("ck_execution_authorizations__contract_schema_version", "contract_schema_version=1"),
        ),
        "credential_refs": (CheckSpec("ck_credential_refs__contract_schema_version", "contract_schema_version=1"),),
        "budget_clock_events": (
            CheckSpec(
                "ck_budget_clock_events__transition_reason",
                "(transition='RUNNING_STARTED' AND suspension_reason IS NULL) OR "
                "(transition='SUSPENSION_STARTED' AND "
                "suspension_reason IN ('USER_PAUSED','REQUIRES_USER_ACTION'))",
            ),
        ),
    }
    return checks.get(table, ())


def _expected_checks(table: str) -> tuple[CheckSpec, ...]:
    """构造 exact 命名 CHECK 集合，额外或遗漏约束均视为 schema drift。"""
    checks = list(_digest_checks(table))
    for primary_key_column in EXPECTED_PRIMARY_KEYS[table]:
        if _declared_type(table, primary_key_column) == "TEXT":
            checks.append(
                CheckSpec(
                    f"ck_{table}__{primary_key_column}__nonempty",
                    f"length({primary_key_column})>0",
                )
            )
    for (check_table, column), values in ENUM_CHECKS.items():
        if check_table != table:
            continue
        allowed = ",".join(repr(value) for value in values)
        checks.append(CheckSpec(f"ck_{table}__{column}__enum", f"{column} IN ({allowed})"))
    for check_table, column in BOOLEAN_COLUMN_PAIRS:
        if check_table == table:
            checks.append(CheckSpec(f"ck_{table}__{column}__boolean", f"{column} IN (0,1)"))
    if "state_version" in EXPECTED_COLUMNS[table]:
        checks.append(CheckSpec(f"ck_{table}__state_version__nonnegative", "state_version>=0"))
    checks.extend(_table_specific_checks(table))
    if table == "workspaces":
        checks.extend(
            (
                CheckSpec(
                    "ck_workspaces__base_sha__git_sha",
                    "length(base_sha)=40 AND base_sha NOT GLOB '*[^0-9a-f]*' AND base_sha<>printf('%040d',0)",
                ),
                CheckSpec(
                    "ck_workspaces__candidate_sha__git_sha",
                    "candidate_sha IS NULL OR (length(candidate_sha)=40 AND candidate_sha NOT GLOB '*[^0-9a-f]*' "
                    "AND candidate_sha<>printf('%040d',0))",
                ),
            )
        )
    if table == "control_plane_epochs":
        checks.append(CheckSpec("ck_control_plane_epochs__singleton", "singleton_id=1"))
    return tuple(sorted(checks, key=lambda item: item.name))


def _expected_triggers(table: str) -> tuple[TriggerSpec, ...]:
    """冻结 append-only、singleton 与 achieved_stage 单调保护触发器集合。"""
    triggers: list[TriggerSpec] = []
    if table in APPEND_ONLY_TABLES:
        triggers.extend(
            (
                TriggerSpec(
                    f"trg_{table}__reject_update",
                    f"CREATE TRIGGER trg_{table}__reject_update BEFORE UPDATE ON {table} "
                    "FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END",
                ),
                TriggerSpec(
                    f"trg_{table}__reject_delete",
                    f"CREATE TRIGGER trg_{table}__reject_delete BEFORE DELETE ON {table} "
                    "FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END",
                ),
            )
        )
    if table == "tasks":
        ranks = (
            "WHEN 'NONE' THEN 0 WHEN 'DESIGN_APPROVED' THEN 1 WHEN 'CODEX_APPROVED' THEN 2 "
            "WHEN 'PR_READY' THEN 3 WHEN 'MERGED' THEN 4 WHEN 'STAGING_ACCEPTED' THEN 5 "
            "WHEN 'PRODUCTION_ACCEPTED' THEN 6 ELSE -1 END"
        )
        valid_stages = (
            "'NONE','DESIGN_APPROVED','CODEX_APPROVED','PR_READY','MERGED','STAGING_ACCEPTED','PRODUCTION_ACCEPTED'"
        )
        triggers.append(
            TriggerSpec(
                "trg_tasks__achieved_stage_monotonic",
                "CREATE TRIGGER trg_tasks__achieved_stage_monotonic "
                "BEFORE UPDATE OF achieved_stage ON tasks FOR EACH ROW "
                f"WHEN OLD.achieved_stage IN ({valid_stages}) "
                f"AND NEW.achieved_stage IN ({valid_stages}) "
                f"AND (CASE OLD.achieved_stage {ranks}) > (CASE NEW.achieved_stage {ranks}) "
                "BEGIN SELECT RAISE(ABORT,'achieved_stage_regression'); END",
            )
        )
    if table == "control_plane_epochs":
        triggers.append(
            TriggerSpec(
                "trg_control_plane_epochs__reject_delete",
                "CREATE TRIGGER trg_control_plane_epochs__reject_delete "
                "BEFORE DELETE ON control_plane_epochs FOR EACH ROW "
                "BEGIN SELECT RAISE(ABORT,'control_plane_epoch_singleton'); END",
            )
        )
    return tuple(sorted(triggers, key=lambda item: item.name))


def _declared_type(table: str, column: str) -> str:
    """把每个冻结列机械映射到唯一 SQLite declared type。"""
    if (table, column) in BLOB_COLUMN_PAIRS:
        return "BLOB"
    if column in INTEGER_COLUMN_NAMES:
        return "INTEGER"
    return "TEXT"


def _column_specs(table: str) -> tuple[ColumnSpec, ...]:
    """由冻结列顺序构造 exact 列规格；Task 1 禁止隐式 DEFAULT 真源。"""
    primary_key = EXPECTED_PRIMARY_KEYS[table]
    return tuple(
        ColumnSpec(
            name=column,
            declared_type=_declared_type(table, column),
            not_null=(table, column) not in NULLABLE_COLUMN_PAIRS,
            default_sql=None,
            primary_key_position=(primary_key.index(column) + 1 if column in primary_key else 0),
        )
        for column in EXPECTED_COLUMNS[table]
    )


def _expected_table_specs() -> dict[str, TableSpec]:
    """建立覆盖 0001–0004 全部表的声明式 exact oracle。"""
    return {
        table: TableSpec(
            columns=_column_specs(table),
            foreign_keys=EXPECTED_FOREIGN_KEYS.get(table, ()),
            uniques=tuple(UniqueSpec(columns) for columns in EXPECTED_UNIQUES.get(table, ())),
            explicit_indexes=_expected_indexes(table),
            checks=_expected_checks(table),
            triggers=_expected_triggers(table),
        )
        for table in EXPECTED_COLUMNS
    }


SCHEMA_MIGRATIONS_COLUMNS = (
    ColumnSpec("version", "INTEGER", True, None, 1),
    ColumnSpec("name", "TEXT", True, None, 0),
    ColumnSpec("checksum", "TEXT", True, None, 0),
    ColumnSpec("applied_at", "TEXT", True, None, 0),
)
SCHEMA_MIGRATIONS_SPEC = TableSpec(columns=SCHEMA_MIGRATIONS_COLUMNS)
TABLE_SPECS = {**_expected_table_specs(), "schema_migrations": SCHEMA_MIGRATIONS_SPEC}
CHECK_MUTATIONS = (
    ("event-head-half", "event_chain_heads", {"committed_event_digest": SHA_A}),
    (
        "ingest-head-negative",
        "ingest_batch_task_heads",
        {"expected_task_seq": -1, "expected_event_digest": SHA_A},
    ),
    ("event-schema-version", "events", {"schema_version": 1}),
    (
        "state-version-increment",
        "authoritative_state_events",
        {"previous_state_version": 1, "state_version": 1},
    ),
    ("intent-revocation-half", "intent_authorizations", {"revoked_at": "2026-08-12T00:00:00Z"}),
    ("intent-budget-zero", "intent_authorizations", {"autonomous_execution_budget_ms": 0}),
    ("intent-repair-negative", "intent_authorizations", {"repair_loop_limit": -1}),
    ("intent-replan-negative", "intent_authorizations", {"auto_replan_limit": -1}),
    ("intent-attempt-zero", "intent_authorizations", {"attempt_limit": 0}),
    ("execution-revocation-half", "execution_authorizations", {"revoked_at": "2026-08-12T00:00:00Z"}),
    ("execution-token-zero", "execution_authorizations", {"fencing_token": 0}),
    ("execution-epoch-negative", "execution_authorizations", {"control_epoch": -1}),
    ("execution-command-negative", "execution_authorizations", {"accepted_control_command_seq": -1}),
    ("execution-consumption", "execution_authorizations", {"consumption_state": "CONSUMED", "max_uses": 1}),
    (
        "budget-reason-link",
        "budget_clock_events",
        {"transition": "RUNNING_STARTED", "suspension_reason": "USER_PAUSED"},
    ),
)


def test_sql_oracle_preserves_literals_and_balanced_check_boundaries() -> None:
    """测试 oracle 自身不得把 literal 大小写折叠或截断嵌套 CHECK。"""
    upper = _sql_tokens("CHECK (phase='COMPLETED')")
    lower = _sql_tokens("CHECK (phase='completed')")
    assert upper != lower
    definitions = _balanced_check_definitions(
        "CREATE TABLE sample (value TEXT, CONSTRAINT ck_nested CHECK ((value IN ('A','B')) OR value IS NULL))"
    )
    assert definitions == {
        "ck_nested": _sql_tokens("(value IN ('A','B')) OR value IS NULL"),
    }
    assert "expected_task_seq" not in EXPECTED_COLUMNS["control_commands"]
    assert _declared_type("ingest_batch_task_heads", "expected_task_seq") == "INTEGER"


def _assert_d_test_path(path: Path) -> Path:
    """测试产生的 SQLite/temp 必须解析到 D:\\codex项目 物理前缀。"""
    resolved = path.resolve()
    allowed = Path("D:/codex项目").resolve()
    try:
        resolved.relative_to(allowed)
    except ValueError as exc:
        raise AssertionError(f"测试路径越出 D 盘受控根：{resolved}") from exc
    return resolved


def _canonical_blob(value: object) -> bytes:
    """生成测试用 JCS BLOB；不把任意 JSON 文本冒充 canonical bytes。"""
    return canonicalize(value)


def _digest_blob(blob: bytes) -> str:
    """计算 canonical BLOB 的稳定 SHA-256 wire 值。"""
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _canonical_digest_selector(blob: bytes) -> bytes:
    """将 primitive digest 封装为闭合 JCS selector，避免向 canonical_* 列写入 TEXT。"""
    selector = {"digest": _digest_blob(blob)}
    canonical_selector = _canonical_blob(selector)
    assert isinstance(canonical_selector, bytes)
    assert json.loads(canonical_selector.decode("utf-8")) == selector
    return canonical_selector


def _migration_paths() -> tuple[Path, ...]:
    """冻结迁移集合与顺序，不允许吞入 Task 2+ 或 Deployment 表。"""
    paths = tuple(MIGRATIONS_DIR / name for name in MIGRATION_NAMES)
    assert [path.name for path in paths] == list(MIGRATION_NAMES)
    for path in paths:
        assert path.is_file(), f"缺少 Task 1 migration：{path}"
    actual = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))
    assert actual == list(MIGRATION_NAMES)
    return paths


@contextmanager
def _migrated_connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """在受控 D 盘测试库执行四份 DDL，并在 finally 关闭连接。"""
    database_path = _assert_d_test_path(tmp_path / "schema.sqlite3")
    connection = sqlite3.connect(database_path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        for migration_path in _migration_paths():
            # 这里只审计 DDL 的最终结构；生产 runner 禁止依赖 executescript 的隐式事务。
            connection.executescript(migration_path.read_text(encoding="utf-8"))
        yield connection
    finally:
        connection.close()


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """按 DDL 顺序读取表列名。"""
    return tuple(row[1] for row in connection.execute(f'PRAGMA table_xinfo("{table}")'))


def _actual_column_specs(connection: sqlite3.Connection, table: str) -> tuple[ColumnSpec, ...]:
    """读取完整 xinfo，并拒绝 generated/hidden 列逃逸 exact column oracle。"""
    rows = connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
    assert all(int(row[6]) == 0 for row in rows), f"{table} 含隐藏或生成列"
    return tuple(
        ColumnSpec(
            name=str(row[1]),
            declared_type=str(row[2]).upper(),
            not_null=bool(row[3]),
            default_sql=None if row[4] is None else str(row[4]),
            primary_key_position=int(row[5]),
        )
        for row in rows
    )


def _index_columns(connection: sqlite3.Connection, table: str) -> list[tuple[bool, tuple[str, ...]]]:
    """返回表上所有索引的唯一性与有序列集合。"""
    indexes: list[tuple[bool, tuple[str, ...]]] = []
    for row in connection.execute(f'PRAGMA index_list("{table}")'):
        index_name = row[1]
        columns = tuple(
            str(item[2])
            for item in connection.execute(f'PRAGMA index_xinfo("{index_name}")')
            if bool(item[5]) and item[2] is not None
        )
        indexes.append((bool(row[2]), columns))
    return indexes


def _actual_foreign_keys(connection: sqlite3.Connection, table: str) -> tuple[ForeignKeySpec, ...]:
    """按 FK id/seq 组合复合键并读取精确 parent selector 与动作。"""
    grouped: dict[int, list[sqlite3.Row | tuple[object, ...]]] = {}
    for row in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
        grouped.setdefault(int(row[0]), []).append(row)
    foreign_keys: list[ForeignKeySpec] = []
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda item: int(item[1]))
        foreign_keys.append(
            ForeignKeySpec(
                columns=tuple(str(item[3]) for item in ordered),
                target_table=str(ordered[0][2]),
                target_columns=tuple(str(item[4]) for item in ordered),
                on_update=str(ordered[0][5]).upper(),
                on_delete=str(ordered[0][6]).upper(),
                match=str(ordered[0][7]).upper(),
            )
        )
    return tuple(sorted(foreign_keys, key=lambda item: (item.columns, item.target_table)))


def _actual_indexes(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[IndexSpec, ...], tuple[IndexSpec, ...]]:
    """读取 index_list/xinfo/完整 SQL；自动索引仅忽略不稳定名称。"""
    explicit: list[IndexSpec] = []
    automatic: list[IndexSpec] = []
    for row in connection.execute(f'PRAGMA index_list("{table}")'):
        name = str(row[1])
        unique = bool(row[2])
        origin = str(row[3])
        partial = bool(row[4])
        terms = tuple(
            IndexTermSpec(
                cid=int(item[1]),
                name=None if item[2] is None else str(item[2]),
                collation=str(item[4]).upper(),
                descending=bool(item[3]),
                key=bool(item[5]),
            )
            for item in connection.execute(f'PRAGMA index_xinfo("{name}")')
        )
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        sql = None if sql_row is None or sql_row[0] is None else str(sql_row[0])
        sql_tokens = None if sql is None else _sql_tokens(sql)
        where_tokens: tuple[str, ...] = ()
        if sql_tokens is not None and "where" in sql_tokens:
            where_tokens = sql_tokens[sql_tokens.index("where") + 1 :]
        spec = IndexSpec(
            name=name if origin == "c" else None,
            terms=terms,
            unique=unique,
            origin=origin,
            partial=partial,
            where_tokens=where_tokens,
            sql_tokens=sql_tokens,
        )
        if origin == "c":
            explicit.append(spec)
        else:
            automatic.append(spec)
    return (
        tuple(sorted(explicit, key=lambda item: item.name or "")),
        tuple(sorted(automatic, key=_index_sort_key)),
    )


def _expected_automatic_indexes(table: str) -> tuple[IndexSpec, ...]:
    """冻结自动索引 origin/partial/xinfo，且不依赖 sqlite_autoindex 名称。"""
    automatic = [
        IndexSpec(
            name=None,
            terms=_index_terms(table, unique),
            unique=True,
            origin="u",
            partial=False,
        )
        for unique in EXPECTED_UNIQUES.get(table, ())
    ]
    primary_key = tuple(
        column.name
        for column in sorted(
            (item for item in TABLE_SPECS[table].columns if item.primary_key_position),
            key=lambda item: item.primary_key_position,
        )
    )
    assert primary_key
    first_pk_type = next(spec.declared_type for spec in TABLE_SPECS[table].columns if spec.name == primary_key[0])
    if len(primary_key) > 1 or first_pk_type != "INTEGER":
        automatic.append(
            IndexSpec(
                name=None,
                terms=_index_terms(table, primary_key),
                unique=True,
                origin="pk",
                partial=False,
            )
        )
    return tuple(sorted(automatic, key=_index_sort_key))


def _assert_exact_checks(connection: sqlite3.Connection, table: str, expected: tuple[CheckSpec, ...]) -> None:
    """核对命名 CHECK 的 exact 集合和表达式，并拒绝额外匿名 CHECK。"""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    assert row is not None and row[0] is not None
    actual = _balanced_check_definitions(str(row[0]))
    expected_map = {item.name.casefold(): _sql_tokens(item.expression) for item in expected}
    assert actual == expected_map


def _assert_exact_triggers(connection: sqlite3.Connection, table: str, expected: tuple[TriggerSpec, ...]) -> None:
    """核对 trigger exact-set 与完整 canonical SQL token 流。"""
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name",
        (table,),
    ).fetchall()
    assert tuple(row[0] for row in rows) == tuple(sorted(item.name for item in expected))
    by_name = {str(row[0]): _sql_tokens(str(row[1])) for row in rows}
    for item in expected:
        assert by_name[item.name] == _sql_tokens(item.sql)


def _assert_index_prefix(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    *,
    unique: bool | None = None,
) -> None:
    """断言存在以前缀覆盖查询或约束列的索引。"""
    expected = tuple(columns)
    matches = [
        is_unique for is_unique, actual in _index_columns(connection, table) if actual[: len(expected)] == expected
    ]
    assert matches, f"{table} 缺少索引前缀 {expected}"
    if unique is not None:
        assert unique in matches, f"{table}{expected} unique={unique} 不符"


def _insert(connection: sqlite3.Connection, table: str, values: Mapping[str, object]) -> None:
    """只给精确列生成参数化 INSERT，测试输出不打印参数或 canonical payload。"""
    columns = tuple(values)
    placeholders = ",".join("?" for _ in columns)
    quoted_columns = ",".join(f'"{column}"' for column in columns)
    # 表名与列名只来自本文件冻结的测试元数据，业务值始终使用参数绑定。
    connection.execute(
        f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',  # noqa: S608
        tuple(values[column] for column in columns),
    )


def _constraint_bypassed_row(table: str) -> dict[str, object]:
    """只为触发器行为隔离生成满足 type/null 的一行，不冒充业务有效 fixture。"""
    row: dict[str, object] = {}
    for column in TABLE_SPECS[table].columns:
        pair = (table, column.name)
        if pair in NULLABLE_COLUMN_PAIRS:
            row[column.name] = None
        elif column.declared_type == "BLOB":
            row[column.name] = _canonical_blob(None)
        elif column.declared_type == "INTEGER":
            row[column.name] = 1
        elif pair in ENUM_CHECKS:
            row[column.name] = ENUM_CHECKS[pair][0]
        elif "digest" in column.name or column.name.endswith("_hash"):
            row[column.name] = SHA_A
        elif column.name in {"base_sha", "candidate_sha"}:
            row[column.name] = GIT_A
        else:
            row[column.name] = f"{table}-{column.name}"
    if table == "events":
        row["schema_version"] = 2
    elif table == "authoritative_state_events":
        row.update(previous_state_version=0, schema_version=1, state_version=1)
    elif table in {"intent_authorizations", "execution_authorizations", "credential_refs"}:
        row["contract_schema_version"] = 1
    return row


@contextmanager
def _insert_for_trigger_probe(
    connection: sqlite3.Connection,
    table: str,
) -> Iterator[tuple[str, object]]:
    """在完整变更窗口旁路 FK/CHECK，只让 UPDATE/DELETE trigger 决定探针结果。"""
    previous_foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    previous_ignore_checks = int(connection.execute("PRAGMA ignore_check_constraints").fetchone()[0])
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("PRAGMA ignore_check_constraints=ON")
    try:
        row = _constraint_bypassed_row(table)
        _insert(connection, table, row)
        primary_key = EXPECTED_PRIMARY_KEYS[table][0]
        yield primary_key, row[primary_key]
    finally:
        # 探针允许业务无效行，但离开窗口必须恢复连接原门禁，避免污染后续断言。
        connection.execute(f"PRAGMA ignore_check_constraints={previous_ignore_checks}")
        connection.execute(f"PRAGMA foreign_keys={previous_foreign_keys}")


def _seed_workflow_graph(connection: sqlite3.Connection) -> None:
    """插入可用于 FK/trigger/PK 负例的最小有效工作流图。"""
    intake_blob = _canonical_blob({"requirementText": "test", "targetStage": "DESIGN_APPROVED"})
    _insert(connection, "projects", {"project_id": "project-1"})
    _insert(
        connection,
        "tasks",
        {
            "task_id": "task-1",
            "project_id": "project-1",
            "lifecycle": "ACTIVE",
            "target_stage": "DESIGN_APPROVED",
            "achieved_stage": "NONE",
            "active_plan_revision_id": None,
            "active_run_id": None,
            "state_version": 0,
            "outcome_version": 0,
            "intake_schema_id": "task-intake.v1",
            "intake_schema_version": 1,
            "canonical_intake": intake_blob,
            "intake_digest": _digest_blob(intake_blob),
        },
    )
    auth_blob = _canonical_blob({"intentAuthorizationId": "intent-1"})
    _insert(
        connection,
        "intent_authorizations",
        {
            "intent_authorization_id": "intent-1",
            "task_id": "task-1",
            "user_id": "user-1",
            "requirement_digest": _canonical_blob({"digest": SHA_A}),
            "project_id": "project-1",
            "repository_id": "repository-1",
            "repository_binding_digest": _canonical_blob({"digest": SHA_A}),
            "baseline_digest": _canonical_blob({"digest": SHA_A}),
            "target_stage": "DESIGN_APPROVED",
            "stage_capability_map_version": "stage-capability-map.v1",
            "allowed_capability_set_digest": _canonical_blob({"digest": SHA_A}),
            "target_binding_digest": _canonical_blob({"digest": SHA_A}),
            "risk_ceiling": "medium",
            "estimated_cost_alert_digest": _canonical_blob({"digest": SHA_A}),
            "autonomous_execution_budget_ms": 1,
            "repair_loop_limit": 0,
            "auto_replan_limit": 0,
            "attempt_limit": 1,
            "issued_at": "2026-08-12T00:00:00Z",
            "expires_at": "2026-08-12T01:00:00Z",
            "revoked_at": None,
            "revoke_reason": None,
            "contract_schema_id": "intent-authorization.v1",
            "contract_schema_version": 1,
            "canonical_contract": auth_blob,
            "canonical_contract_digest": _canonical_digest_selector(auth_blob),
        },
    )
    run_spec_blob = _canonical_blob(
        {
            "schemaVersion": 1,
            "taskId": "task-1",
            "specRevision": 1,
            "parentRevisionId": None,
            "planRevisionDigest": SHA_B,
            "createdAt": "2026-08-12T00:00:00Z",
        }
    )
    revision_blob = _canonical_blob(
        {
            "schemaVersion": 1,
            "planRevisionId": "plan-1",
            "taskId": "task-1",
            "specRevision": 1,
            "parentRevisionId": None,
            "planRevisionDigest": SHA_B,
            "createdAt": "2026-08-12T00:00:00Z",
        }
    )
    _insert(
        connection,
        "plan_revisions",
        {
            "plan_revision_id": "plan-1",
            "task_id": "task-1",
            "spec_revision": 1,
            "parent_revision_id": None,
            "intent_authorization_id": "intent-1",
            "semantic_plan_hash": SHA_A,
            "plan_revision_digest": SHA_B,
            "dag_version": 1,
            "node_capability_map_version": "node-capability-map.v1",
            "stage_capability_map_version": "stage-capability-map.v1",
            "created_at": "2026-08-12T00:00:00Z",
            "run_spec_schema_id": "run-spec.v1",
            "run_spec_schema_version": 1,
            "canonical_run_spec": run_spec_blob,
            "plan_revision_schema_id": "plan-revision.v1",
            "plan_revision_schema_version": 1,
            "canonical_plan_revision": revision_blob,
        },
    )
    _insert(
        connection,
        "runs",
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "desired_state": "RUNNING",
            "observed_state": "QUEUED",
            "phase": "CREATED",
            "active_barrier_id": None,
            "dag_version": 1,
            "run_cursor": None,
            "durable_cursor": None,
            "control_command_seq": 0,
            "repair_loop_used": 0,
            "auto_replan_used": 0,
            "requires_user_action": 0,
            "block_reason_code": None,
            "budget_accumulated_ms": 0,
            "budget_clock_state": None,
            "budget_clock_boot_id": None,
            "budget_clock_monotonic_ns": None,
            "budget_clock_wall_time": None,
            "budget_suspension_reason": None,
            "executor_id": None,
            "host_id": None,
            "runtime": None,
            "protocol_version": "control-plane.v1",
            "recovery_target_phase": None,
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "phase_barriers",
        {
            "barrier_id": "bar-runtime-1",
            "run_id": "run-1",
            "plan_revision_id": "plan-1",
            "business_phase": "PLANNING",
            "barrier_ordinal": 0,
            "required_node_set_digest": SHA_A,
            "settle_timeout_ms": 30_000,
            "settle_deadline_at": None,
            "pass_predicate_id": "planning-barrier-v1",
            "settled": 0,
            "passed": 0,
            "gate_digest": None,
            "state_version": 0,
        },
    )
    connection.execute("UPDATE runs SET active_barrier_id = ? WHERE run_id = ?", ("bar-runtime-1", "run-1"))
    connection.execute(
        "UPDATE tasks SET active_plan_revision_id = ?, active_run_id = ? WHERE task_id = ?",
        ("plan-1", "run-1", "task-1"),
    )
    _insert(
        connection,
        "steps",
        {
            "step_id": "step-1",
            "run_id": "run-1",
            "plan_revision_id": "plan-1",
            "barrier_id": "bar-runtime-1",
            "logical_node_id": "plan",
            "business_phase": "PLANNING",
            "node_type": "PLAN",
            "required": 1,
            "side_effect_class": "none",
            "phase": "PENDING",
            "outcome": "NONE",
            "dependency_hash": SHA_A,
            "required_artifacts_digest": SHA_A,
            "success_predicate_id": "planning-complete-v1",
            "timeout_ms": 30_000,
            "retry_policy_id": "no-retry-v1",
            "idempotency_key": "step-1-v1",
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "attempts",
        {
            "attempt_id": "attempt-1",
            "step_id": "step-1",
            "supersedes_attempt_id": None,
            "phase": "CREATED",
            "outcome": "NONE",
            "executor_id": None,
            "process_session_id": None,
            "pid": None,
            "process_start_time": None,
            "job_object_id": None,
            "wsl_distro": None,
            "container_id": None,
            "image_digest": None,
            "exit_code": None,
            "termination_reason": None,
            "fencing_token": 0,
            "control_epoch": 0,
            "accepted_control_command_seq": 0,
            "interrupt_command_id": None,
            "drain_state": "NONE",
            "started_at": None,
            "ended_at": None,
            "state_version": 0,
        },
    )


def test_p01_four_migrations_have_exact_table_and_column_specs(tmp_path: Path) -> None:
    """table_list/xinfo 必须同时冻结对象集、strict/wr 与所有列属性。"""
    with _migrated_connection(tmp_path) as connection:
        rows = {
            str(row[1]): (str(row[2]), int(row[3]), bool(row[4]), bool(row[5]))
            for row in connection.execute("PRAGMA table_list")
            if str(row[0]) == "main" and not str(row[1]).startswith("sqlite_")
        }
        assert set(rows) == set(TABLE_SPECS)
        assert all(object_type == "table" for object_type, _ncol, _wr, _strict in rows.values())
        for table, spec in TABLE_SPECS.items():
            assert rows[table] == ("table", len(spec.columns), spec.without_rowid, spec.strict)
            assert _actual_column_specs(connection, table) == spec.columns
        views = connection.execute("SELECT name, sql FROM sqlite_schema WHERE type='view' ORDER BY name").fetchall()
        assert views == []


def test_p03_required_pragmas_are_actual_readbacks_not_setter_assumptions(tmp_path: Path) -> None:
    """WAL/FULL/foreign_keys 必须由同一真实连接读回精确值。"""
    with _migrated_connection(tmp_path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_p01_all_text_primary_keys_are_explicitly_not_null(tmp_path: Path) -> None:
    """SQLite rowid 表的 TEXT PK 不隐含 NOT NULL，DDL 必须显式声明。"""
    with _migrated_connection(tmp_path) as connection:
        for table in EXPECTED_COLUMNS:
            for row in connection.execute(f'PRAGMA table_info("{table}")'):
                _cid, column, declared_type, not_null, _default, pk_ordinal = row
                if pk_ordinal and str(declared_type).casefold() == "text":
                    assert not_null == 1, f"{table}.{column} TEXT PK 未显式 NOT NULL"


def test_n02_foreign_keys_are_exact_and_each_child_selector_is_indexed(tmp_path: Path) -> None:
    """FK target/selector/actions 必须 exact，且 child selector 有完整左前缀索引。"""
    with _migrated_connection(tmp_path) as connection:
        for table, spec in TABLE_SPECS.items():
            assert _actual_foreign_keys(connection, table) == tuple(
                sorted(spec.foreign_keys, key=lambda item: (item.columns, item.target_table))
            )
            for foreign_key in spec.foreign_keys:
                _assert_index_prefix(connection, table, foreign_key.columns)

        intake_blob = _canonical_blob({"requirementText": "orphan", "targetStage": "DESIGN_APPROVED"})
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            _insert(
                connection,
                "tasks",
                {
                    "task_id": "orphan-task",
                    "project_id": "missing-project",
                    "lifecycle": "ACTIVE",
                    "target_stage": "DESIGN_APPROVED",
                    "achieved_stage": "NONE",
                    "state_version": 0,
                    "outcome_version": 0,
                    "intake_schema_id": "task-intake.v1",
                    "intake_schema_version": 1,
                    "canonical_intake": intake_blob,
                    "intake_digest": _digest_blob(intake_blob),
                },
            )


def test_n03_unique_and_explicit_index_collections_are_exact(tmp_path: Path) -> None:
    """UNIQUE 禁止 prefix 假阳性；显式/自动索引不得遗漏、冗余或换列顺序。"""
    with _migrated_connection(tmp_path) as connection:
        for table, spec in TABLE_SPECS.items():
            explicit, automatic = _actual_indexes(connection, table)
            assert explicit == tuple(sorted(spec.explicit_indexes, key=lambda item: item.name))
            assert automatic == _expected_automatic_indexes(table)


def test_action_completed_partial_unique_has_behavioral_teeth(tmp_path: Path) -> None:
    """同一 Action 可有多条非完成 receipt，但最多一条 COMPLETED。"""
    with _migrated_connection(tmp_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        prototype = _constraint_bypassed_row("action_receipt_events")
        for receipt_seq, phase in ((1, "STARTED"), (2, "STARTED"), (3, "COMPLETED")):
            row = dict(prototype)
            row.update(
                receipt_event_id=f"receipt-{receipt_seq}",
                action_id="action-one",
                receipt_seq=receipt_seq,
                phase=phase,
            )
            _insert(connection, "action_receipt_events", row)
        duplicate = dict(prototype)
        duplicate.update(
            receipt_event_id="receipt-4",
            action_id="action-one",
            receipt_seq=4,
            phase="COMPLETED",
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            _insert(connection, "action_receipt_events", duplicate)


def test_recovery_indexes_are_exact_without_redundant_cas_index(tmp_path: Path) -> None:
    """恢复 selector 必须由 exact 显式索引覆盖，PK CAS 不另建 (id,state_version)。"""
    with _migrated_connection(tmp_path) as connection:
        for table, index_sets in RECOVERY_INDEXES.items():
            for columns in index_sets:
                _assert_index_prefix(connection, table, columns)
        for table, primary_key in EXPECTED_PRIMARY_KEYS.items():
            all_indexes = [columns for _unique, columns in _index_columns(connection, table)]
            assert (*primary_key, "state_version") not in all_indexes


def test_named_checks_and_triggers_are_exact_collections(tmp_path: Path) -> None:
    """所有业务 CHECK/trigger 均以 exact-set 审计，拒绝额外错误约束或缺失保护。"""
    with _migrated_connection(tmp_path) as connection:
        for table, spec in TABLE_SPECS.items():
            _assert_exact_checks(connection, table, spec.checks)
            _assert_exact_triggers(connection, table, spec.triggers)


@pytest.mark.parametrize(("table", "column"), sorted(ENUM_CHECKS))
def test_each_enum_check_rejects_an_unknown_value(tmp_path: Path, table: str, column: str) -> None:
    """逐个真实写入非法 enum，避免 exact SQL oracle 自身成为无牙齿字符串比较。"""
    with _migrated_connection(tmp_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        row = _constraint_bypassed_row(table)
        row[column] = "__UNKNOWN__"
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert(connection, table, row)


@pytest.mark.parametrize(("_case", "table", "mutation"), CHECK_MUTATIONS, ids=[case[0] for case in CHECK_MUTATIONS])
def test_each_cross_column_and_bound_check_rejects_drift(
    tmp_path: Path,
    _case: str,
    table: str,
    mutation: Mapping[str, object],
) -> None:
    """逐条破坏 pair/version/bound 联动，确认对应 CHECK 在 SQLite 真实执行。"""
    with _migrated_connection(tmp_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        row = _constraint_bypassed_row(table)
        row.update(mutation)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert(connection, table, row)


def test_projects_are_exact_identity_only_until_project_contract_is_frozen(tmp_path: Path) -> None:
    """未冻结 Project schema 时只保存 project_id，不发明 opaque contract 或业务列。"""
    with _migrated_connection(tmp_path) as connection:
        assert _table_columns(connection, "projects") == ("project_id",)
        _insert(connection, "projects", {"project_id": "project-roundtrip"})
        assert connection.execute(
            "SELECT project_id FROM projects WHERE project_id = ?",
            ("project-roundtrip",),
        ).fetchone() == ("project-roundtrip",)


def test_deployment_tables_are_not_guessed_in_task1(tmp_path: Path) -> None:
    """server/deployment/acceptance/rollback 属后续 Deployment，不得提前建表。"""
    with _migrated_connection(tmp_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables.isdisjoint({"server_profiles", "deployments", "acceptance_checks", "rollback_receipts"})


@pytest.mark.parametrize("table", sorted(APPEND_ONLY_TABLES))
def test_n05_each_append_only_table_rejects_update_and_delete(tmp_path: Path, table: str) -> None:
    """每个追加表均以真实 UPDATE/DELETE 证明 trigger 有牙齿。"""
    with _migrated_connection(tmp_path) as connection:
        with _insert_for_trigger_probe(connection, table) as (primary_key, identity):
            with pytest.raises(sqlite3.IntegrityError, match="append_only"):
                connection.execute(
                    f'UPDATE "{table}" SET "{primary_key}"="{primary_key}" WHERE "{primary_key}"=?',  # noqa: S608
                    (identity,),
                )
            with pytest.raises(sqlite3.IntegrityError, match="append_only"):
                connection.execute(
                    f'DELETE FROM "{table}" WHERE "{primary_key}"=?',  # noqa: S608
                    (identity,),
                )


@pytest.mark.parametrize(
    "table",
    sorted(set(EXPECTED_COLUMNS) - APPEND_ONLY_TABLES - {"control_plane_epochs"}),
)
def test_mutable_tables_do_not_inherit_append_only_triggers(tmp_path: Path, table: str) -> None:
    """非追加表的同值 UPDATE 与 DELETE 均成功，拒绝错误扩大 immutable 边界。"""
    with _migrated_connection(tmp_path) as connection:
        with _insert_for_trigger_probe(connection, table) as (primary_key, identity):
            updated = connection.execute(
                f'UPDATE "{table}" SET "{primary_key}"="{primary_key}" WHERE "{primary_key}"=?',  # noqa: S608
                (identity,),
            )
            assert updated.rowcount == 1
            deleted = connection.execute(
                f'DELETE FROM "{table}" WHERE "{primary_key}"=?',  # noqa: S608
                (identity,),
            )
            assert deleted.rowcount == 1


def test_n06_achieved_stage_trigger_rejects_regression_but_not_target_ceiling(tmp_path: Path) -> None:
    """里程碑只按自身 rank 单调，不能错误增加 achieved<=target。"""
    with _migrated_connection(tmp_path) as connection:
        _seed_workflow_graph(connection)
        connection.execute(
            "UPDATE tasks SET achieved_stage = ? WHERE task_id = ?",
            ("STAGING_ACCEPTED", "task-1"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE tasks SET achieved_stage = ? WHERE task_id = ?",
                ("CODEX_APPROVED", "task-1"),
            )
        assert connection.execute(
            "SELECT target_stage, achieved_stage FROM tasks WHERE task_id = ?",
            ("task-1",),
        ).fetchone() == ("DESIGN_APPROVED", "STAGING_ACCEPTED")


def test_n07_attempt_primary_key_can_never_be_reused(tmp_path: Path) -> None:
    """同一 Attempt ID 的第二次插入失败，且 Attempt 具备五路 CAS 所需版本。"""
    with _migrated_connection(tmp_path) as connection:
        _seed_workflow_graph(connection)
        assert _table_columns(connection, "attempts")[-1] == "state_version"
        assert connection.execute(
            "SELECT state_version FROM attempts WHERE attempt_id = ?",
            ("attempt-1",),
        ).fetchone() == (0,)
        values = connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?",
            ("attempt-1",),
        ).fetchone()
        placeholders = ",".join("?" for _ in values)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            # 占位符数量来自刚读出的固定 attempts 表形，且所有值继续参数绑定。
            connection.execute(f"INSERT INTO attempts VALUES ({placeholders})", values)  # noqa: S608


@pytest.mark.parametrize(
    ("table", "identity_column", "identity"),
    [
        ("tasks", "task_id", "task-1"),
        ("runs", "run_id", "run-1"),
        ("phase_barriers", "barrier_id", "bar-runtime-1"),
        ("steps", "step_id", "step-1"),
        ("attempts", "attempt_id", "attempt-1"),
    ],
)
def test_core_five_projection_rows_are_mutable_through_state_version(
    tmp_path: Path,
    table: str,
    identity_column: str,
    identity: str,
) -> None:
    """Task/Run/Barrier/Step/Attempt 是 CAS 投影，append-only trigger 不得误伤合法版本推进。"""
    with _migrated_connection(tmp_path) as connection:
        _seed_workflow_graph(connection)
        cursor = connection.execute(
            f'UPDATE "{table}" SET state_version=state_version+1 '  # noqa: S608
            f'WHERE "{identity_column}"=? AND state_version=?',
            (identity, 0),
        )
        assert cursor.rowcount == 1
        assert connection.execute(
            f'SELECT state_version FROM "{table}" WHERE "{identity_column}"=?',  # noqa: S608
            (identity,),
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    ("table", "column", "unknown"),
    [
        ("tasks", "lifecycle", "UNKNOWN"),
        ("tasks", "target_stage", "UNKNOWN"),
        ("tasks", "achieved_stage", "UNKNOWN"),
        ("runs", "desired_state", "UNKNOWN"),
        ("runs", "observed_state", "UNKNOWN"),
        ("runs", "phase", "UNKNOWN"),
        ("phase_barriers", "business_phase", "CREATED"),
        ("steps", "phase", "UNKNOWN"),
        ("steps", "outcome", "UNKNOWN"),
        ("attempts", "phase", "UNKNOWN"),
        ("attempts", "outcome", "UNKNOWN"),
        ("attempts", "drain_state", "UNKNOWN"),
    ],
)
def test_n01_core_unknown_enums_are_rejected(
    tmp_path: Path,
    table: str,
    column: str,
    unknown: str,
) -> None:
    """核心工作流所有闭集枚举都必须有 SQLite CHECK，而非只靠 Python。"""
    with _migrated_connection(tmp_path) as connection:
        _seed_workflow_graph(connection)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            # table/column 只来自上方封闭参数表，不接受运行时外部输入。
            connection.execute(f'UPDATE "{table}" SET "{column}" = ?', (unknown,))  # noqa: S608


@pytest.mark.parametrize(
    "bad_digest",
    [
        "SHA256:" + "a" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha256:" + "g" * 64,
    ],
)
def test_n04_digest_shape_rejects_uppercase_length_and_non_hex(
    tmp_path: Path,
    bad_digest: str,
) -> None:
    """sha256 wire 必须小写精确 71 字符；全 0 digest 另有正例。"""
    with _migrated_connection(tmp_path) as connection:
        _seed_workflow_graph(connection)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "UPDATE attempts SET image_digest = ? WHERE attempt_id = ?",
                (bad_digest, "attempt-1"),
            )


def test_n04_zero_digest_allowed_but_zero_git_sha_rejected(tmp_path: Path) -> None:
    """摘要允许全 0 sentinel；语义 Git SHA 必须拒绝全 0。"""
    with _migrated_connection(tmp_path) as connection:
        _seed_workflow_graph(connection)
        _insert(
            connection,
            "workspaces",
            {
                "workspace_id": "workspace-1",
                "task_id": "task-1",
                "project_id": "project-1",
                "distro_identity": "distro-1",
                "linux_worktree_path": "/worktrees/task-1",
                "linux_git_common_dir": "/mirrors/repo.git",
                "windows_source_repo": "D:/codex项目/source",
                "factory_bare_repo": "D:/codex项目/factory.git",
                "base_sha": GIT_A,
                "candidate_sha": None,
                "candidate_ref": "refs/factory/tasks/task-1/candidate",
                "checkpoint_namespace": "checkpoint-task-1",
                "writer_lease_key": "workspace-task-1",
                "import_bundle_digest": SHA_ZERO,
                "export_bundle_digest": None,
                "cleanup_state": "ACTIVE",
                "state_version": 0,
            },
        )
        assert connection.execute(
            "SELECT import_bundle_digest FROM workspaces WHERE workspace_id = ?",
            ("workspace-1",),
        ).fetchone() == (SHA_ZERO,)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "UPDATE workspaces SET base_sha = ? WHERE workspace_id = ?",
                ("0" * 40, "workspace-1"),
            )


def test_n10_persisted_control_uses_soft_pause_not_legacy_pause(tmp_path: Path) -> None:
    """持久层接受 SOFT_PAUSE，拒绝旧 wire 适配值 PAUSE。"""
    with _migrated_connection(tmp_path) as connection:
        _seed_workflow_graph(connection)
        base = {
            "command_id": "command-1",
            "run_id": "run-1",
            "command_seq": 1,
            "request_id": "request-1",
            "command_type": "SOFT_PAUSE",
            "actor_id": "user-1",
            "expected_state_version": 0,
            "accepted_state_version": 1,
            "issued_at": "2026-08-12T00:00:00Z",
            "acknowledged_attempt_id": "attempt-1",
            "reason_digest": SHA_A,
        }
        _insert(connection, "control_commands", base)
        legacy = dict(base)
        legacy.update(
            {
                "command_id": "command-2",
                "command_seq": 2,
                "request_id": "request-2",
                "command_type": "PAUSE",
            }
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert(connection, "control_commands", legacy)


def test_control_plane_epoch_singleton_is_seeded_cas_mutable_and_undeletable(tmp_path: Path) -> None:
    """迁移预置唯一 epoch 行；Task 1 只冻结 CAS/存续约束，不实现 Task 8 推进策略。"""
    with _migrated_connection(tmp_path) as connection:
        rows = connection.execute(
            "SELECT singleton_id, writer_epoch, control_epoch, state_version FROM control_plane_epochs"
        ).fetchall()
        assert rows == [(1, 0, 0, 0)]

        updated = connection.execute(
            "UPDATE control_plane_epochs "
            "SET writer_epoch=?, control_epoch=?, state_version=state_version+1 "
            "WHERE singleton_id=? AND state_version=?",
            (7, 11, 1, 0),
        )
        assert updated.rowcount == 1

        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert(
                connection,
                "control_plane_epochs",
                {"singleton_id": 2, "writer_epoch": 0, "control_epoch": 0, "state_version": 0},
            )
        with pytest.raises(sqlite3.IntegrityError, match="control_plane_epoch_singleton"):
            connection.execute("DELETE FROM control_plane_epochs WHERE singleton_id=1")

        assert connection.execute(
            "SELECT singleton_id, writer_epoch, control_epoch, state_version FROM control_plane_epochs"
        ).fetchall() == [(1, 7, 11, 1)]
