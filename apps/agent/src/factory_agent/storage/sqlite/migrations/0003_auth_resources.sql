-- 本 migration 由冻结测试 oracle 机械生成；运行时按原始 bytes 校验 SHA-256。

CREATE TABLE intent_authorizations (
    intent_authorization_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    requirement_digest BLOB NOT NULL,
    project_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    repository_binding_digest BLOB NOT NULL,
    baseline_digest BLOB NOT NULL,
    target_stage TEXT NOT NULL,
    stage_capability_map_version TEXT NOT NULL,
    allowed_capability_set_digest BLOB NOT NULL,
    target_binding_digest BLOB NOT NULL,
    risk_ceiling TEXT NOT NULL,
    estimated_cost_alert_digest BLOB NOT NULL,
    autonomous_execution_budget_ms INTEGER NOT NULL,
    repair_loop_limit INTEGER NOT NULL,
    auto_replan_limit INTEGER NOT NULL,
    attempt_limit INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    contract_schema_id TEXT NOT NULL,
    contract_schema_version INTEGER NOT NULL,
    canonical_contract BLOB NOT NULL,
    canonical_contract_digest BLOB NOT NULL,
    PRIMARY KEY (intent_authorization_id),
    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (project_id) REFERENCES projects (project_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_intent_authorizations__attempt_bounds CHECK (attempt_limit BETWEEN 1 AND 9007199254740991),
    CONSTRAINT ck_intent_authorizations__auto_replan_bounds CHECK (auto_replan_limit BETWEEN 0 AND 9007199254740991),
    CONSTRAINT ck_intent_authorizations__autonomous_budget_bounds CHECK (autonomous_execution_budget_ms BETWEEN 1 AND 9007199254740991),
    CONSTRAINT ck_intent_authorizations__contract_schema_version CHECK (contract_schema_version=1),
    CONSTRAINT ck_intent_authorizations__intent_authorization_id__nonempty CHECK (length(intent_authorization_id)>0),
    CONSTRAINT ck_intent_authorizations__repair_loop_bounds CHECK (repair_loop_limit BETWEEN 0 AND 9007199254740991),
    CONSTRAINT ck_intent_authorizations__revocation_pair CHECK ((revoked_at IS NULL AND revoke_reason IS NULL) OR (revoked_at IS NOT NULL AND revoke_reason IS NOT NULL AND length(revoke_reason)>0)),
    CONSTRAINT ck_intent_authorizations__risk_ceiling__enum CHECK (risk_ceiling IN ('low','medium','high','critical')),
    CONSTRAINT ck_intent_authorizations__target_stage__enum CHECK (target_stage IN ('DESIGN_APPROVED','CODEX_APPROVED','PR_READY','MERGED','STAGING_ACCEPTED','PRODUCTION_ACCEPTED'))
) STRICT;

CREATE INDEX idx_intent_authorizations__expires_at ON intent_authorizations (expires_at);

CREATE INDEX idx_intent_authorizations__project_id ON intent_authorizations (project_id);

CREATE INDEX idx_intent_authorizations__task_id ON intent_authorizations (task_id);

CREATE TABLE execution_authorizations (
    execution_authorization_id TEXT NOT NULL,
    intent_authorization_id TEXT NOT NULL,
    plan_revision_id TEXT NOT NULL,
    semantic_plan_hash BLOB NOT NULL,
    plan_revision_digest BLOB NOT NULL,
    stage_capability_map_version TEXT NOT NULL,
    stage_capability_map_digest BLOB NOT NULL,
    node_capability_map_version TEXT NOT NULL,
    node_capability_map_digest BLOB NOT NULL,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    executor_id TEXT NOT NULL,
    resource_fingerprint BLOB NOT NULL,
    capability_scope_digest BLOB NOT NULL,
    idempotency_key BLOB NOT NULL,
    input_bindings BLOB NOT NULL,
    action_capability TEXT NOT NULL,
    action_policy_snapshot_digest BLOB NOT NULL,
    fencing_token INTEGER NOT NULL,
    control_epoch INTEGER NOT NULL,
    accepted_control_command_seq INTEGER NOT NULL,
    max_uses INTEGER NOT NULL,
    consumption_state TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    contract_schema_id TEXT NOT NULL,
    contract_schema_version INTEGER NOT NULL,
    canonical_contract BLOB NOT NULL,
    canonical_contract_digest BLOB NOT NULL,
    PRIMARY KEY (execution_authorization_id),
    FOREIGN KEY (intent_authorization_id) REFERENCES intent_authorizations (intent_authorization_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (plan_revision_id) REFERENCES plan_revisions (plan_revision_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (step_id) REFERENCES steps (step_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (attempt_id) REFERENCES attempts (attempt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_execution_authorizations__action_capability__enum CHECK (action_capability IN ('acceptance.fixture.write','build.exec.isolated','check.publish','container.inspect.scoped','db.backup','db.check','db.migrate','db.read','db.restore','forge.observe.scoped','git.local_commit','git.push','http.check.scoped','log.read.scoped','network.egress.scoped','nginx.switch','pr.create','pr.update','registry.observe.scoped','registry.push','remote.observe.scoped','remote.write.scoped','repo.bootstrap','repo.merge','repo.read','restore.validation.instance','rollback','service.restart.scoped','ssh.exec.scoped','target.guard.clear','test.exec.isolated','traffic.switch.scoped','worktree.write')),
    CONSTRAINT ck_execution_authorizations__command_seq_bounds CHECK (accepted_control_command_seq BETWEEN 0 AND 9007199254740991),
    CONSTRAINT ck_execution_authorizations__consumption_state__enum CHECK (consumption_state IN ('AVAILABLE','CONSUMED')),
    CONSTRAINT ck_execution_authorizations__consumption_uses CHECK ((consumption_state='AVAILABLE' AND max_uses>=1) OR (consumption_state='CONSUMED' AND max_uses=0)),
    CONSTRAINT ck_execution_authorizations__contract_schema_version CHECK (contract_schema_version=1),
    CONSTRAINT ck_execution_authorizations__control_epoch_bounds CHECK (control_epoch BETWEEN 0 AND 9007199254740991),
    CONSTRAINT ck_execution_authorizations__execution_authorization_id__nonempty CHECK (length(execution_authorization_id)>0),
    CONSTRAINT ck_execution_authorizations__fencing_token_bounds CHECK (fencing_token BETWEEN 1 AND 9007199254740991),
    CONSTRAINT ck_execution_authorizations__max_uses_bounds CHECK (max_uses BETWEEN 0 AND 9007199254740991),
    CONSTRAINT ck_execution_authorizations__node_type__enum CHECK (node_type IN ('PLAN','DESIGN_REVIEW','BOOTSTRAP_REPOSITORY','IMPLEMENT','VERIFY','CODE_REVIEW','ATTEST_REVIEW','PUBLISH_PR','MERGE','BUILD_ARTIFACT','DEPLOY_STAGING','ACCEPT_STAGING','DEPLOY_PRODUCTION','ACCEPT_PRODUCTION','ROLLBACK','RECONCILE_TARGET','RESTORE_DRILL')),
    CONSTRAINT ck_execution_authorizations__revocation_pair CHECK ((revoked_at IS NULL AND revoke_reason IS NULL) OR (revoked_at IS NOT NULL AND revoke_reason IS NOT NULL AND length(revoke_reason)>0))
) STRICT;

CREATE INDEX idx_execution_authorizations__attempt_id ON execution_authorizations (attempt_id);

CREATE INDEX idx_execution_authorizations__consumption_state ON execution_authorizations (consumption_state);

CREATE INDEX idx_execution_authorizations__expires_at ON execution_authorizations (expires_at);

CREATE INDEX idx_execution_authorizations__intent_authorization_id ON execution_authorizations (intent_authorization_id);

CREATE INDEX idx_execution_authorizations__plan_revision_id ON execution_authorizations (plan_revision_id);

CREATE INDEX idx_execution_authorizations__run_id ON execution_authorizations (run_id);

CREATE INDEX idx_execution_authorizations__step_id ON execution_authorizations (step_id);

CREATE TABLE resource_leases (
    resource_key TEXT NOT NULL,
    owner_executor_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    control_epoch INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    PRIMARY KEY (resource_key),
    CONSTRAINT ck_resource_leases__resource_key__nonempty CHECK (length(resource_key)>0),
    CONSTRAINT ck_resource_leases__state_version__nonnegative CHECK (state_version>=0)
) STRICT;

CREATE INDEX idx_resource_leases__expires_at ON resource_leases (expires_at);

CREATE TABLE actions (
    action_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    resource_fingerprint TEXT NOT NULL,
    current_phase TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    PRIMARY KEY (action_id),
    UNIQUE (action_type, idempotency_key),
    CONSTRAINT ck_actions__action_id__nonempty CHECK (length(action_id)>0),
    CONSTRAINT ck_actions__current_phase__enum CHECK (current_phase IN ('STARTED','COMPLETED','ABSENT_CONFIRMED','FAILED_RECONCILED')),
    CONSTRAINT ck_actions__request_digest__sha256 CHECK (length(request_digest)=71 AND substr(request_digest,1,7)='sha256:' AND substr(request_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_actions__state_version__nonnegative CHECK (state_version>=0)
) STRICT;

CREATE INDEX idx_actions__resource_fingerprint__current_phase ON actions (resource_fingerprint, current_phase);

CREATE TABLE action_receipt_events (
    receipt_event_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    receipt_seq INTEGER NOT NULL,
    phase TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    execution_authorization_id TEXT NOT NULL,
    executor_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    control_epoch INTEGER NOT NULL,
    control_command_seq INTEGER NOT NULL,
    remote_identity BLOB NOT NULL,
    result_digest TEXT,
    evidence_digest TEXT,
    previous_receipt_digest TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (receipt_event_id),
    UNIQUE (action_id, receipt_seq),
    FOREIGN KEY (action_id) REFERENCES actions (action_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (attempt_id) REFERENCES attempts (attempt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (execution_authorization_id) REFERENCES execution_authorizations (execution_authorization_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_action_receipt_events__evidence_digest__sha256 CHECK (evidence_digest IS NULL OR (length(evidence_digest)=71 AND substr(evidence_digest,1,7)='sha256:' AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_action_receipt_events__phase__enum CHECK (phase IN ('STARTED','COMPLETED','ABSENT_CONFIRMED','FAILED_RECONCILED')),
    CONSTRAINT ck_action_receipt_events__previous_receipt_digest__sha256 CHECK (previous_receipt_digest IS NULL OR (length(previous_receipt_digest)=71 AND substr(previous_receipt_digest,1,7)='sha256:' AND substr(previous_receipt_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_action_receipt_events__receipt_event_id__nonempty CHECK (length(receipt_event_id)>0),
    CONSTRAINT ck_action_receipt_events__result_digest__sha256 CHECK (result_digest IS NULL OR (length(result_digest)=71 AND substr(result_digest,1,7)='sha256:' AND substr(result_digest,8) NOT GLOB '*[^0-9a-f]*'))
) STRICT;

CREATE INDEX idx_action_receipt_events__attempt_id ON action_receipt_events (attempt_id);

CREATE INDEX idx_action_receipt_events__execution_authorization_id ON action_receipt_events (execution_authorization_id);

CREATE UNIQUE INDEX ux_action_receipt_events__action_id__completed ON action_receipt_events (action_id) WHERE phase='COMPLETED';

CREATE TRIGGER trg_action_receipt_events__reject_delete BEFORE DELETE ON action_receipt_events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER trg_action_receipt_events__reject_update BEFORE UPDATE ON action_receipt_events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE target_guards (
    resource_fingerprint TEXT NOT NULL,
    guard_state TEXT NOT NULL,
    reason_code TEXT,
    evidence_digest TEXT,
    created_by_release_id TEXT,
    created_at TEXT NOT NULL,
    cleared_by TEXT,
    cleared_at TEXT,
    clear_receipt_digest TEXT,
    state_version INTEGER NOT NULL,
    PRIMARY KEY (resource_fingerprint),
    CONSTRAINT ck_target_guards__clear_receipt_digest__sha256 CHECK (clear_receipt_digest IS NULL OR (length(clear_receipt_digest)=71 AND substr(clear_receipt_digest,1,7)='sha256:' AND substr(clear_receipt_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_target_guards__evidence_digest__sha256 CHECK (evidence_digest IS NULL OR (length(evidence_digest)=71 AND substr(evidence_digest,1,7)='sha256:' AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_target_guards__guard_state__enum CHECK (guard_state IN ('OPEN','INTERVENTION_REQUIRED')),
    CONSTRAINT ck_target_guards__resource_fingerprint__nonempty CHECK (length(resource_fingerprint)>0),
    CONSTRAINT ck_target_guards__state_version__nonnegative CHECK (state_version>=0)
) STRICT;

CREATE INDEX idx_target_guards__guard_state ON target_guards (guard_state);

CREATE TABLE notification_actions (
    notification_action_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    outcome_version INTEGER NOT NULL,
    notification_kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    provider_tag TEXT,
    payload_digest TEXT NOT NULL,
    current_phase TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    PRIMARY KEY (notification_action_id),
    UNIQUE (task_id, outcome_version, notification_kind),
    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_notification_actions__current_phase__enum CHECK (current_phase IN ('PREPARED','DISPATCH_STARTED','ACCEPTED_BY_ADAPTER','FAILED_KNOWN','DISPATCH_UNKNOWN')),
    CONSTRAINT ck_notification_actions__notification_action_id__nonempty CHECK (length(notification_action_id)>0),
    CONSTRAINT ck_notification_actions__payload_digest__sha256 CHECK (length(payload_digest)=71 AND substr(payload_digest,1,7)='sha256:' AND substr(payload_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_notification_actions__state_version__nonnegative CHECK (state_version>=0)
) STRICT;

CREATE TABLE notification_receipt_events (
    notification_receipt_event_id TEXT NOT NULL,
    notification_action_id TEXT NOT NULL,
    receipt_seq INTEGER NOT NULL,
    phase TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    provider_identity TEXT,
    result_digest TEXT,
    previous_receipt_digest TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (notification_receipt_event_id),
    UNIQUE (notification_action_id, receipt_seq),
    FOREIGN KEY (notification_action_id) REFERENCES notification_actions (notification_action_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_notification_receipt_events__notification_receipt_event_id__nonempty CHECK (length(notification_receipt_event_id)>0),
    CONSTRAINT ck_notification_receipt_events__phase__enum CHECK (phase IN ('PREPARED','DISPATCH_STARTED','ACCEPTED_BY_ADAPTER','FAILED_KNOWN','DISPATCH_UNKNOWN')),
    CONSTRAINT ck_notification_receipt_events__previous_receipt_digest__sha256 CHECK (previous_receipt_digest IS NULL OR (length(previous_receipt_digest)=71 AND substr(previous_receipt_digest,1,7)='sha256:' AND substr(previous_receipt_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_notification_receipt_events__result_digest__sha256 CHECK (result_digest IS NULL OR (length(result_digest)=71 AND substr(result_digest,1,7)='sha256:' AND substr(result_digest,8) NOT GLOB '*[^0-9a-f]*'))
) STRICT;

CREATE TRIGGER trg_notification_receipt_events__reject_delete BEFORE DELETE ON notification_receipt_events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER trg_notification_receipt_events__reject_update BEFORE UPDATE ON notification_receipt_events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE credential_refs (
    credential_id TEXT NOT NULL,
    credential_type TEXT NOT NULL,
    profile_scope TEXT NOT NULL,
    resource_fingerprint TEXT,
    expires_at TEXT,
    contract_schema_id TEXT NOT NULL,
    contract_schema_version INTEGER NOT NULL,
    canonical_contract BLOB NOT NULL,
    canonical_contract_digest BLOB NOT NULL,
    PRIMARY KEY (credential_id),
    CONSTRAINT ck_credential_refs__contract_schema_version CHECK (contract_schema_version=1),
    CONSTRAINT ck_credential_refs__credential_id__nonempty CHECK (length(credential_id)>0),
    CONSTRAINT ck_credential_refs__credential_type__enum CHECK (credential_type IN ('SSH_KEY','REGISTRY_TOKEN','API_KEY','GITHUB_APP','FACTORY_PROFILE_TOKEN'))
) STRICT;

CREATE TABLE audit_records (
    audit_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    operation TEXT NOT NULL,
    outcome TEXT NOT NULL,
    record_schema_id TEXT NOT NULL,
    record_schema_version INTEGER NOT NULL,
    canonical_redacted_record BLOB NOT NULL,
    record_digest TEXT NOT NULL,
    PRIMARY KEY (audit_id),
    CONSTRAINT ck_audit_records__audit_id__nonempty CHECK (length(audit_id)>0),
    CONSTRAINT ck_audit_records__record_digest__sha256 CHECK (length(record_digest)=71 AND substr(record_digest,1,7)='sha256:' AND substr(record_digest,8) NOT GLOB '*[^0-9a-f]*')
) STRICT;

CREATE INDEX idx_audit_records__occurred_at ON audit_records (occurred_at);
