-- 本 migration 由冻结测试 oracle 机械生成；运行时按原始 bytes 校验 SHA-256。

CREATE TABLE control_commands (
    command_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    command_seq INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    command_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    expected_state_version INTEGER NOT NULL,
    accepted_state_version INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    acknowledged_attempt_id TEXT,
    reason_digest TEXT,
    PRIMARY KEY (command_id),
    UNIQUE (run_id, command_seq),
    UNIQUE (request_id),
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (acknowledged_attempt_id) REFERENCES attempts (attempt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_control_commands__command_id__nonempty CHECK (length(command_id)>0),
    CONSTRAINT ck_control_commands__command_type__enum CHECK (command_type IN ('SOFT_PAUSE','IMMEDIATE_STOP','RESUME','CANCEL')),
    CONSTRAINT ck_control_commands__reason_digest__sha256 CHECK (reason_digest IS NULL OR (length(reason_digest)=71 AND substr(reason_digest,1,7)='sha256:' AND substr(reason_digest,8) NOT GLOB '*[^0-9a-f]*'))
) STRICT;

CREATE INDEX idx_control_commands__acknowledged_attempt_id ON control_commands (acknowledged_attempt_id);

CREATE TRIGGER trg_control_commands__reject_delete BEFORE DELETE ON control_commands FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER trg_control_commands__reject_update BEFORE UPDATE ON control_commands FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE control_command_receipt_events (
    command_receipt_event_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    receipt_seq INTEGER NOT NULL,
    phase TEXT NOT NULL,
    attempt_id TEXT,
    state_event_id TEXT,
    evidence_digest TEXT,
    previous_receipt_digest TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (command_receipt_event_id),
    UNIQUE (command_id, receipt_seq),
    FOREIGN KEY (command_id) REFERENCES control_commands (command_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (attempt_id) REFERENCES attempts (attempt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (state_event_id) REFERENCES authoritative_state_events (state_event_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_control_command_receipt_events__command_receipt_event_id__nonempty CHECK (length(command_receipt_event_id)>0),
    CONSTRAINT ck_control_command_receipt_events__evidence_digest__sha256 CHECK (evidence_digest IS NULL OR (length(evidence_digest)=71 AND substr(evidence_digest,1,7)='sha256:' AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_control_command_receipt_events__phase__enum CHECK (phase IN ('ACKNOWLEDGED','COMPLETED','FAILED')),
    CONSTRAINT ck_control_command_receipt_events__previous_receipt_digest__sha256 CHECK (previous_receipt_digest IS NULL OR (length(previous_receipt_digest)=71 AND substr(previous_receipt_digest,1,7)='sha256:' AND substr(previous_receipt_digest,8) NOT GLOB '*[^0-9a-f]*'))
) STRICT;

CREATE INDEX idx_control_command_receipt_events__attempt_id ON control_command_receipt_events (attempt_id);

CREATE INDEX idx_control_command_receipt_events__state_event_id ON control_command_receipt_events (state_event_id);

CREATE TRIGGER trg_control_command_receipt_events__reject_delete BEFORE DELETE ON control_command_receipt_events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER trg_control_command_receipt_events__reject_update BEFORE UPDATE ON control_command_receipt_events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE budget_clock_events (
    run_id TEXT NOT NULL,
    clock_seq INTEGER NOT NULL,
    transition TEXT NOT NULL,
    suspension_reason TEXT,
    boot_id TEXT NOT NULL,
    monotonic_ns INTEGER NOT NULL,
    wall_time TEXT NOT NULL,
    state_event_id TEXT NOT NULL,
    previous_clock_digest TEXT,
    clock_digest TEXT NOT NULL,
    PRIMARY KEY (run_id, clock_seq),
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (state_event_id) REFERENCES authoritative_state_events (state_event_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_budget_clock_events__clock_digest__sha256 CHECK (length(clock_digest)=71 AND substr(clock_digest,1,7)='sha256:' AND substr(clock_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_budget_clock_events__previous_clock_digest__sha256 CHECK (previous_clock_digest IS NULL OR (length(previous_clock_digest)=71 AND substr(previous_clock_digest,1,7)='sha256:' AND substr(previous_clock_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_budget_clock_events__run_id__nonempty CHECK (length(run_id)>0),
    CONSTRAINT ck_budget_clock_events__suspension_reason__enum CHECK (suspension_reason IN ('USER_PAUSED','REQUIRES_USER_ACTION')),
    CONSTRAINT ck_budget_clock_events__transition__enum CHECK (transition IN ('RUNNING_STARTED','SUSPENSION_STARTED')),
    CONSTRAINT ck_budget_clock_events__transition_reason CHECK ((transition='RUNNING_STARTED' AND suspension_reason IS NULL) OR (transition='SUSPENSION_STARTED' AND suspension_reason IN ('USER_PAUSED','REQUIRES_USER_ACTION')))
) STRICT;

CREATE INDEX idx_budget_clock_events__state_event_id ON budget_clock_events (state_event_id);

CREATE TRIGGER trg_budget_clock_events__reject_delete BEFORE DELETE ON budget_clock_events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER trg_budget_clock_events__reject_update BEFORE UPDATE ON budget_clock_events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE control_plane_epochs (
    singleton_id INTEGER NOT NULL,
    writer_epoch INTEGER NOT NULL,
    control_epoch INTEGER NOT NULL,
    state_version INTEGER NOT NULL,
    PRIMARY KEY (singleton_id),
    CONSTRAINT ck_control_plane_epochs__singleton CHECK (singleton_id=1),
    CONSTRAINT ck_control_plane_epochs__state_version__nonnegative CHECK (state_version>=0)
) STRICT;

CREATE TRIGGER trg_control_plane_epochs__reject_delete BEFORE DELETE ON control_plane_epochs FOR EACH ROW BEGIN SELECT RAISE(ABORT,'control_plane_epoch_singleton'); END;

INSERT INTO control_plane_epochs (singleton_id, writer_epoch, control_epoch, state_version) VALUES (1, 0, 0, 0);
