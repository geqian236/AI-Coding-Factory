-- 本 migration 由冻结测试 oracle 机械生成；运行时按原始 bytes 校验 SHA-256。

-- 迁移账本属于数据库 bootstrap 合同；生产 runner 会在同一显式事务内登记 raw-byte checksum。
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (version)
) STRICT;

CREATE TABLE projects (
    project_id TEXT NOT NULL,
    PRIMARY KEY (project_id),
    CONSTRAINT ck_projects__project_id__nonempty CHECK (length(project_id)>0)
) STRICT;

CREATE TABLE tasks (
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    target_stage TEXT NOT NULL,
    achieved_stage TEXT NOT NULL,
    active_plan_revision_id TEXT,
    active_run_id TEXT,
    state_version INTEGER NOT NULL,
    outcome_version INTEGER NOT NULL,
    intake_schema_id TEXT NOT NULL,
    intake_schema_version INTEGER NOT NULL,
    canonical_intake BLOB NOT NULL,
    intake_digest TEXT NOT NULL,
    PRIMARY KEY (task_id),
    FOREIGN KEY (project_id) REFERENCES projects (project_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (active_plan_revision_id) REFERENCES plan_revisions (plan_revision_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (active_run_id) REFERENCES runs (run_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_tasks__achieved_stage__enum CHECK (achieved_stage IN ('NONE','DESIGN_APPROVED','CODEX_APPROVED','PR_READY','MERGED','STAGING_ACCEPTED','PRODUCTION_ACCEPTED')),
    CONSTRAINT ck_tasks__intake_digest__sha256 CHECK (length(intake_digest)=71 AND substr(intake_digest,1,7)='sha256:' AND substr(intake_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_tasks__lifecycle__enum CHECK (lifecycle IN ('ACTIVE','SUCCEEDED','FAILED','CANCELLED','ROLLED_BACK','FAILED_NEEDS_INTERVENTION')),
    CONSTRAINT ck_tasks__state_version__nonnegative CHECK (state_version>=0),
    CONSTRAINT ck_tasks__target_stage__enum CHECK (target_stage IN ('DESIGN_APPROVED','CODEX_APPROVED','PR_READY','MERGED','STAGING_ACCEPTED','PRODUCTION_ACCEPTED')),
    CONSTRAINT ck_tasks__task_id__nonempty CHECK (length(task_id)>0)
) STRICT;

CREATE INDEX idx_tasks__active_plan_revision_id ON tasks (active_plan_revision_id);

CREATE INDEX idx_tasks__active_run_id ON tasks (active_run_id);

CREATE INDEX idx_tasks__project_id ON tasks (project_id);

CREATE TRIGGER trg_tasks__achieved_stage_monotonic BEFORE UPDATE OF achieved_stage ON tasks FOR EACH ROW WHEN OLD.achieved_stage IN ('NONE','DESIGN_APPROVED','CODEX_APPROVED','PR_READY','MERGED','STAGING_ACCEPTED','PRODUCTION_ACCEPTED') AND NEW.achieved_stage IN ('NONE','DESIGN_APPROVED','CODEX_APPROVED','PR_READY','MERGED','STAGING_ACCEPTED','PRODUCTION_ACCEPTED') AND (CASE OLD.achieved_stage WHEN 'NONE' THEN 0 WHEN 'DESIGN_APPROVED' THEN 1 WHEN 'CODEX_APPROVED' THEN 2 WHEN 'PR_READY' THEN 3 WHEN 'MERGED' THEN 4 WHEN 'STAGING_ACCEPTED' THEN 5 WHEN 'PRODUCTION_ACCEPTED' THEN 6 ELSE -1 END) > (CASE NEW.achieved_stage WHEN 'NONE' THEN 0 WHEN 'DESIGN_APPROVED' THEN 1 WHEN 'CODEX_APPROVED' THEN 2 WHEN 'PR_READY' THEN 3 WHEN 'MERGED' THEN 4 WHEN 'STAGING_ACCEPTED' THEN 5 WHEN 'PRODUCTION_ACCEPTED' THEN 6 ELSE -1 END) BEGIN SELECT RAISE(ABORT,'achieved_stage_regression'); END;

CREATE TABLE plan_revisions (
    plan_revision_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    spec_revision INTEGER NOT NULL,
    parent_revision_id TEXT,
    intent_authorization_id TEXT NOT NULL,
    semantic_plan_hash TEXT NOT NULL,
    plan_revision_digest TEXT NOT NULL,
    dag_version INTEGER NOT NULL,
    node_capability_map_version TEXT NOT NULL,
    stage_capability_map_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    run_spec_schema_id TEXT NOT NULL,
    run_spec_schema_version INTEGER NOT NULL,
    canonical_run_spec BLOB NOT NULL,
    plan_revision_schema_id TEXT NOT NULL,
    plan_revision_schema_version INTEGER NOT NULL,
    canonical_plan_revision BLOB NOT NULL,
    PRIMARY KEY (plan_revision_id),
    UNIQUE (plan_revision_digest),
    UNIQUE (task_id, spec_revision),
    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (parent_revision_id) REFERENCES plan_revisions (plan_revision_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (intent_authorization_id) REFERENCES intent_authorizations (intent_authorization_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_plan_revisions__plan_revision_digest__sha256 CHECK (length(plan_revision_digest)=71 AND substr(plan_revision_digest,1,7)='sha256:' AND substr(plan_revision_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_plan_revisions__plan_revision_id__nonempty CHECK (length(plan_revision_id)>0),
    CONSTRAINT ck_plan_revisions__semantic_plan_hash__sha256 CHECK (length(semantic_plan_hash)=71 AND substr(semantic_plan_hash,1,7)='sha256:' AND substr(semantic_plan_hash,8) NOT GLOB '*[^0-9a-f]*')
) STRICT;

CREATE INDEX idx_plan_revisions__intent_authorization_id ON plan_revisions (intent_authorization_id);

CREATE INDEX idx_plan_revisions__parent_revision_id ON plan_revisions (parent_revision_id);

CREATE TRIGGER trg_plan_revisions__reject_delete BEFORE DELETE ON plan_revisions FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER trg_plan_revisions__reject_update BEFORE UPDATE ON plan_revisions FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE phase_barriers (
    barrier_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    plan_revision_id TEXT NOT NULL,
    business_phase TEXT NOT NULL,
    barrier_ordinal INTEGER NOT NULL,
    required_node_set_digest TEXT NOT NULL,
    settle_timeout_ms INTEGER NOT NULL,
    settle_deadline_at TEXT,
    pass_predicate_id TEXT NOT NULL,
    settled INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    gate_digest TEXT,
    state_version INTEGER NOT NULL,
    PRIMARY KEY (barrier_id),
    UNIQUE (run_id, plan_revision_id, business_phase, barrier_ordinal),
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (plan_revision_id) REFERENCES plan_revisions (plan_revision_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_phase_barriers__barrier_id__nonempty CHECK (length(barrier_id)>0),
    CONSTRAINT ck_phase_barriers__business_phase__enum CHECK (business_phase IN ('PLANNING','DESIGN_REVIEWING','PREPARING_WORKSPACE','IMPLEMENTING','VERIFYING','CODE_REVIEWING','PUBLISHING_PR','MERGING','BUILDING_ARTIFACT','DEPLOYING_STAGING','ACCEPTING_STAGING','DEPLOYING_PRODUCTION','ACCEPTING_PRODUCTION','ROLLING_BACK','FINALIZING')),
    CONSTRAINT ck_phase_barriers__gate_digest__sha256 CHECK (gate_digest IS NULL OR (length(gate_digest)=71 AND substr(gate_digest,1,7)='sha256:' AND substr(gate_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_phase_barriers__passed__boolean CHECK (passed IN (0,1)),
    CONSTRAINT ck_phase_barriers__required_node_set_digest__sha256 CHECK (length(required_node_set_digest)=71 AND substr(required_node_set_digest,1,7)='sha256:' AND substr(required_node_set_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_phase_barriers__settled__boolean CHECK (settled IN (0,1)),
    CONSTRAINT ck_phase_barriers__state_version__nonnegative CHECK (state_version>=0)
) STRICT;

CREATE INDEX idx_phase_barriers__plan_revision_id ON phase_barriers (plan_revision_id);

CREATE TABLE runs (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    desired_state TEXT NOT NULL,
    observed_state TEXT NOT NULL,
    phase TEXT NOT NULL,
    active_barrier_id TEXT,
    dag_version INTEGER NOT NULL,
    run_cursor INTEGER,
    durable_cursor INTEGER,
    control_command_seq INTEGER NOT NULL,
    repair_loop_used INTEGER NOT NULL,
    auto_replan_used INTEGER NOT NULL,
    requires_user_action INTEGER NOT NULL,
    block_reason_code TEXT,
    budget_accumulated_ms INTEGER NOT NULL,
    budget_clock_state TEXT,
    budget_clock_boot_id TEXT,
    budget_clock_monotonic_ns INTEGER,
    budget_clock_wall_time TEXT,
    budget_suspension_reason TEXT,
    executor_id TEXT,
    host_id TEXT,
    runtime TEXT,
    protocol_version TEXT NOT NULL,
    recovery_target_phase TEXT,
    state_version INTEGER NOT NULL,
    PRIMARY KEY (run_id),
    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (active_barrier_id) REFERENCES phase_barriers (barrier_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_runs__desired_state__enum CHECK (desired_state IN ('RUNNING','PAUSED','CANCELLED')),
    CONSTRAINT ck_runs__observed_state__enum CHECK (observed_state IN ('QUEUED','RUNNING','PAUSING','PAUSED','STOPPING','INTERRUPTED','RECONCILING','BLOCKED','TERMINATED')),
    CONSTRAINT ck_runs__phase__enum CHECK (phase IN ('CREATED','PREFLIGHT','BOOTSTRAPPING_REPOSITORY','PLANNING','DESIGN_REVIEWING','PREPARING_WORKSPACE','IMPLEMENTING','VERIFYING','CODE_REVIEWING','PUBLISHING_PR','MERGING','BUILDING_ARTIFACT','DEPLOYING_STAGING','ACCEPTING_STAGING','DEPLOYING_PRODUCTION','ACCEPTING_PRODUCTION','ROLLING_BACK','FINALIZING')),
    CONSTRAINT ck_runs__requires_user_action__boolean CHECK (requires_user_action IN (0,1)),
    CONSTRAINT ck_runs__run_id__nonempty CHECK (length(run_id)>0),
    CONSTRAINT ck_runs__runtime__enum CHECK (runtime IN ('local-windows','wsl-docker')),
    CONSTRAINT ck_runs__state_version__nonnegative CHECK (state_version>=0)
) STRICT;

CREATE INDEX idx_runs__active_barrier_id ON runs (active_barrier_id);

CREATE INDEX idx_runs__task_id ON runs (task_id);

CREATE TABLE steps (
    step_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    plan_revision_id TEXT NOT NULL,
    barrier_id TEXT NOT NULL,
    logical_node_id TEXT NOT NULL,
    business_phase TEXT NOT NULL,
    node_type TEXT NOT NULL,
    required INTEGER NOT NULL,
    side_effect_class TEXT NOT NULL,
    phase TEXT NOT NULL,
    outcome TEXT NOT NULL,
    dependency_hash TEXT NOT NULL,
    required_artifacts_digest TEXT NOT NULL,
    success_predicate_id TEXT NOT NULL,
    timeout_ms INTEGER NOT NULL,
    retry_policy_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    PRIMARY KEY (step_id),
    UNIQUE (run_id, plan_revision_id, logical_node_id),
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (plan_revision_id) REFERENCES plan_revisions (plan_revision_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (barrier_id) REFERENCES phase_barriers (barrier_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_steps__business_phase__enum CHECK (business_phase IN ('PLANNING','DESIGN_REVIEWING','PREPARING_WORKSPACE','IMPLEMENTING','VERIFYING','CODE_REVIEWING','PUBLISHING_PR','MERGING','BUILDING_ARTIFACT','DEPLOYING_STAGING','ACCEPTING_STAGING','DEPLOYING_PRODUCTION','ACCEPTING_PRODUCTION','ROLLING_BACK','FINALIZING')),
    CONSTRAINT ck_steps__dependency_hash__sha256 CHECK (length(dependency_hash)=71 AND substr(dependency_hash,1,7)='sha256:' AND substr(dependency_hash,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_steps__node_type__enum CHECK (node_type IN ('PLAN','DESIGN_REVIEW','BOOTSTRAP_REPOSITORY','IMPLEMENT','VERIFY','CODE_REVIEW','ATTEST_REVIEW','PUBLISH_PR','MERGE','BUILD_ARTIFACT','DEPLOY_STAGING','ACCEPT_STAGING','DEPLOY_PRODUCTION','ACCEPT_PRODUCTION','ROLLBACK','RECONCILE_TARGET','RESTORE_DRILL')),
    CONSTRAINT ck_steps__outcome__enum CHECK (outcome IN ('NONE','SUCCEEDED','FAILED','INTERRUPTED','SKIPPED','CANCELLED','UNKNOWN_REMOTE_STATE')),
    CONSTRAINT ck_steps__phase__enum CHECK (phase IN ('PENDING','READY','DISPATCHED','RUNNING','RECONCILING','TERMINAL')),
    CONSTRAINT ck_steps__required__boolean CHECK (required IN (0,1)),
    CONSTRAINT ck_steps__required_artifacts_digest__sha256 CHECK (length(required_artifacts_digest)=71 AND substr(required_artifacts_digest,1,7)='sha256:' AND substr(required_artifacts_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_steps__state_version__nonnegative CHECK (state_version>=0),
    CONSTRAINT ck_steps__step_id__nonempty CHECK (length(step_id)>0)
) STRICT;

CREATE INDEX idx_steps__barrier_id ON steps (barrier_id);

CREATE INDEX idx_steps__plan_revision_id ON steps (plan_revision_id);

CREATE TABLE attempts (
    attempt_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    supersedes_attempt_id TEXT,
    phase TEXT NOT NULL,
    outcome TEXT NOT NULL,
    executor_id TEXT,
    process_session_id TEXT,
    pid INTEGER,
    process_start_time TEXT,
    job_object_id TEXT,
    wsl_distro TEXT,
    container_id TEXT,
    image_digest TEXT,
    exit_code INTEGER,
    termination_reason TEXT,
    fencing_token INTEGER NOT NULL,
    control_epoch INTEGER NOT NULL,
    accepted_control_command_seq INTEGER NOT NULL,
    interrupt_command_id TEXT,
    drain_state TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    state_version INTEGER NOT NULL,
    PRIMARY KEY (attempt_id),
    FOREIGN KEY (step_id) REFERENCES steps (step_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (supersedes_attempt_id) REFERENCES attempts (attempt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (interrupt_command_id) REFERENCES control_commands (command_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_attempts__attempt_id__nonempty CHECK (length(attempt_id)>0),
    CONSTRAINT ck_attempts__drain_state__enum CHECK (drain_state IN ('NONE','DRAINING','DRAINED')),
    CONSTRAINT ck_attempts__image_digest__sha256 CHECK (image_digest IS NULL OR (length(image_digest)=71 AND substr(image_digest,1,7)='sha256:' AND substr(image_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_attempts__outcome__enum CHECK (outcome IN ('NONE','SUCCEEDED','FAILED','INTERRUPTED','KILLED','LOST','UNKNOWN_REMOTE_STATE')),
    CONSTRAINT ck_attempts__phase__enum CHECK (phase IN ('CREATED','STARTING','RUNNING','INTERRUPTING','RECONCILING','TERMINATED')),
    CONSTRAINT ck_attempts__state_version__nonnegative CHECK (state_version>=0)
) STRICT;

CREATE INDEX idx_attempts__interrupt_command_id ON attempts (interrupt_command_id);

CREATE INDEX idx_attempts__step_id ON attempts (step_id);

CREATE INDEX idx_attempts__supersedes_attempt_id ON attempts (supersedes_attempt_id);

CREATE TABLE workspaces (
    workspace_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    distro_identity TEXT NOT NULL,
    linux_worktree_path TEXT NOT NULL,
    linux_git_common_dir TEXT NOT NULL,
    windows_source_repo TEXT NOT NULL,
    factory_bare_repo TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    candidate_sha TEXT,
    candidate_ref TEXT NOT NULL,
    checkpoint_namespace TEXT NOT NULL,
    writer_lease_key TEXT NOT NULL,
    import_bundle_digest TEXT,
    export_bundle_digest TEXT,
    cleanup_state TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    PRIMARY KEY (workspace_id),
    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (project_id) REFERENCES projects (project_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_workspaces__base_sha__git_sha CHECK (length(base_sha)=40 AND base_sha NOT GLOB '*[^0-9a-f]*' AND base_sha<>printf('%040d',0)),
    CONSTRAINT ck_workspaces__candidate_sha__git_sha CHECK (candidate_sha IS NULL OR (length(candidate_sha)=40 AND candidate_sha NOT GLOB '*[^0-9a-f]*' AND candidate_sha<>printf('%040d',0))),
    CONSTRAINT ck_workspaces__export_bundle_digest__sha256 CHECK (export_bundle_digest IS NULL OR (length(export_bundle_digest)=71 AND substr(export_bundle_digest,1,7)='sha256:' AND substr(export_bundle_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_workspaces__import_bundle_digest__sha256 CHECK (import_bundle_digest IS NULL OR (length(import_bundle_digest)=71 AND substr(import_bundle_digest,1,7)='sha256:' AND substr(import_bundle_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_workspaces__state_version__nonnegative CHECK (state_version>=0),
    CONSTRAINT ck_workspaces__workspace_id__nonempty CHECK (length(workspace_id)>0)
) STRICT;

CREATE INDEX idx_workspaces__project_id ON workspaces (project_id);

CREATE INDEX idx_workspaces__task_id ON workspaces (task_id);
