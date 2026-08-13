-- 本 migration 由冻结测试 oracle 机械生成；运行时按原始 bytes 校验 SHA-256。

CREATE TABLE event_chain_heads (
    task_id TEXT NOT NULL,
    committed_task_seq INTEGER,
    committed_event_digest TEXT,
    pending_batch_id TEXT,
    pending_writer_epoch INTEGER,
    pending_claimed_at TEXT,
    state_version INTEGER NOT NULL,
    PRIMARY KEY (task_id),
    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (pending_batch_id) REFERENCES ingest_batches (prepared_batch_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_event_chain_heads__committed_event_digest__sha256 CHECK (committed_event_digest IS NULL OR (length(committed_event_digest)=71 AND substr(committed_event_digest,1,7)='sha256:' AND substr(committed_event_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_event_chain_heads__committed_pair CHECK ((committed_task_seq IS NULL AND committed_event_digest IS NULL) OR (committed_task_seq IS NOT NULL AND committed_task_seq>=0 AND committed_event_digest IS NOT NULL)),
    CONSTRAINT ck_event_chain_heads__state_version__nonnegative CHECK (state_version>=0),
    CONSTRAINT ck_event_chain_heads__task_id__nonempty CHECK (length(task_id)>0)
) STRICT;

CREATE INDEX idx_event_chain_heads__pending_batch_id ON event_chain_heads (pending_batch_id);

CREATE TABLE ingest_batches (
    prepared_batch_id TEXT NOT NULL,
    writer_epoch INTEGER NOT NULL,
    state TEXT NOT NULL,
    manifest_storage_path TEXT,
    manifest_digest TEXT,
    ordered_ingest_ids_digest TEXT,
    per_task_expected_heads_digest TEXT,
    first_batch_ordinal INTEGER NOT NULL,
    last_batch_ordinal INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    payload_bytes INTEGER NOT NULL,
    oldest_ingested_at TEXT NOT NULL,
    claimed_at TEXT,
    prepared_at TEXT,
    committed_at TEXT,
    recovery_state TEXT NOT NULL,
    PRIMARY KEY (prepared_batch_id),
    CONSTRAINT ck_ingest_batches__manifest_digest__sha256 CHECK (manifest_digest IS NULL OR (length(manifest_digest)=71 AND substr(manifest_digest,1,7)='sha256:' AND substr(manifest_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_ingest_batches__ordered_ingest_ids_digest__sha256 CHECK (ordered_ingest_ids_digest IS NULL OR (length(ordered_ingest_ids_digest)=71 AND substr(ordered_ingest_ids_digest,1,7)='sha256:' AND substr(ordered_ingest_ids_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_ingest_batches__per_task_expected_heads_digest__sha256 CHECK (per_task_expected_heads_digest IS NULL OR (length(per_task_expected_heads_digest)=71 AND substr(per_task_expected_heads_digest,1,7)='sha256:' AND substr(per_task_expected_heads_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_ingest_batches__prepared_batch_id__nonempty CHECK (length(prepared_batch_id)>0),
    CONSTRAINT ck_ingest_batches__state__enum CHECK (state IN ('CLAIMED','PREPARING','PREPARED','COMMITTED','ABANDONED'))
) STRICT;

CREATE INDEX idx_ingest_batches__state__writer_epoch ON ingest_batches (state, writer_epoch);

CREATE TABLE ingest_batch_task_heads (
    prepared_batch_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    expected_task_seq INTEGER,
    expected_event_digest TEXT,
    first_batch_ordinal INTEGER NOT NULL,
    last_batch_ordinal INTEGER NOT NULL,
    PRIMARY KEY (prepared_batch_id, task_id),
    FOREIGN KEY (prepared_batch_id) REFERENCES ingest_batches (prepared_batch_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_ingest_batch_task_heads__expected_event_digest__sha256 CHECK (expected_event_digest IS NULL OR (length(expected_event_digest)=71 AND substr(expected_event_digest,1,7)='sha256:' AND substr(expected_event_digest,8) NOT GLOB '*[^0-9a-f]*')),
    CONSTRAINT ck_ingest_batch_task_heads__expected_pair CHECK ((expected_task_seq IS NULL AND expected_event_digest IS NULL) OR (expected_task_seq IS NOT NULL AND expected_task_seq>=0 AND expected_event_digest IS NOT NULL)),
    CONSTRAINT ck_ingest_batch_task_heads__prepared_batch_id__nonempty CHECK (length(prepared_batch_id)>0),
    CONSTRAINT ck_ingest_batch_task_heads__task_id__nonempty CHECK (length(task_id)>0)
) STRICT;

CREATE INDEX idx_ingest_batch_task_heads__task_id ON ingest_batch_task_heads (task_id);

CREATE TABLE stream_segments (
    segment_id TEXT NOT NULL,
    prepared_batch_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    stream_id TEXT,
    storage_path TEXT NOT NULL,
    source_span_summary BLOB NOT NULL,
    sanitized_start INTEGER NOT NULL,
    sanitized_end INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    redaction_manifest_digest TEXT NOT NULL,
    predecessor_digest TEXT NOT NULL,
    complete_footer_digest TEXT NOT NULL,
    commit_state TEXT NOT NULL,
    PRIMARY KEY (segment_id),
    FOREIGN KEY (prepared_batch_id) REFERENCES ingest_batches (prepared_batch_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (attempt_id) REFERENCES attempts (attempt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_stream_segments__commit_state__enum CHECK (commit_state IN ('COMMITTED','QUARANTINED')),
    CONSTRAINT ck_stream_segments__complete_footer_digest__sha256 CHECK (length(complete_footer_digest)=71 AND substr(complete_footer_digest,1,7)='sha256:' AND substr(complete_footer_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_stream_segments__content_digest__sha256 CHECK (length(content_digest)=71 AND substr(content_digest,1,7)='sha256:' AND substr(content_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_stream_segments__predecessor_digest__sha256 CHECK (length(predecessor_digest)=71 AND substr(predecessor_digest,1,7)='sha256:' AND substr(predecessor_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_stream_segments__redaction_manifest_digest__sha256 CHECK (length(redaction_manifest_digest)=71 AND substr(redaction_manifest_digest,1,7)='sha256:' AND substr(redaction_manifest_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_stream_segments__segment_id__nonempty CHECK (length(segment_id)>0)
) STRICT;

CREATE INDEX idx_stream_segments__attempt_id ON stream_segments (attempt_id);

CREATE INDEX idx_stream_segments__prepared_batch_id ON stream_segments (prepared_batch_id);

CREATE INDEX idx_stream_segments__run_id ON stream_segments (run_id);

CREATE INDEX idx_stream_segments__task_id ON stream_segments (task_id);

CREATE TABLE ingest_queue_metrics (
    metric_id TEXT NOT NULL,
    writer_epoch INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    metrics_schema_id TEXT NOT NULL,
    metrics_schema_version INTEGER NOT NULL,
    canonical_metrics BLOB NOT NULL,
    metrics_digest TEXT NOT NULL,
    PRIMARY KEY (metric_id),
    CONSTRAINT ck_ingest_queue_metrics__metric_id__nonempty CHECK (length(metric_id)>0),
    CONSTRAINT ck_ingest_queue_metrics__metrics_digest__sha256 CHECK (length(metrics_digest)=71 AND substr(metrics_digest,1,7)='sha256:' AND substr(metrics_digest,8) NOT GLOB '*[^0-9a-f]*')
) STRICT;

CREATE TABLE events (
    event_id TEXT NOT NULL,
    ingest_event_id TEXT NOT NULL,
    prepared_batch_id TEXT NOT NULL,
    batch_ordinal INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_seq INTEGER NOT NULL,
    run_seq INTEGER NOT NULL,
    source_seq INTEGER NOT NULL,
    source_mapping_precision TEXT NOT NULL,
    sanitized_segment_id TEXT NOT NULL,
    sanitized_start INTEGER NOT NULL,
    sanitized_end INTEGER NOT NULL,
    durability_class TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    previous_event_digest TEXT NOT NULL,
    event_digest TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    canonical_event BLOB NOT NULL,
    PRIMARY KEY (event_id),
    UNIQUE (ingest_event_id),
    UNIQUE (prepared_batch_id, batch_ordinal),
    UNIQUE (task_id, task_seq),
    UNIQUE (event_digest),
    FOREIGN KEY (prepared_batch_id) REFERENCES ingest_batches (prepared_batch_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (sanitized_segment_id) REFERENCES stream_segments (segment_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_events__durability_class__enum CHECK (durability_class IN ('authoritative_state','side_effect_receipt','provider_source','derived')),
    CONSTRAINT ck_events__event_digest__sha256 CHECK (length(event_digest)=71 AND substr(event_digest,1,7)='sha256:' AND substr(event_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_events__event_id__nonempty CHECK (length(event_id)>0),
    CONSTRAINT ck_events__payload_digest__sha256 CHECK (length(payload_digest)=71 AND substr(payload_digest,1,7)='sha256:' AND substr(payload_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_events__previous_event_digest__sha256 CHECK (length(previous_event_digest)=71 AND substr(previous_event_digest,1,7)='sha256:' AND substr(previous_event_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_events__schema_version CHECK (schema_version=2),
    CONSTRAINT ck_events__source_mapping_precision__enum CHECK (source_mapping_precision IN ('byte','field','frame','none'))
) STRICT;

CREATE INDEX idx_events__run_id ON events (run_id);

CREATE INDEX idx_events__run_id__run_seq ON events (run_id, run_seq);

CREATE INDEX idx_events__sanitized_segment_id ON events (sanitized_segment_id);

CREATE TRIGGER trg_events__reject_delete BEFORE DELETE ON events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER trg_events__reject_update BEFORE UPDATE ON events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE authoritative_state_events (
    state_event_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    run_id TEXT,
    step_id TEXT,
    attempt_id TEXT,
    previous_state_version INTEGER NOT NULL,
    state_version INTEGER NOT NULL,
    payload_digest TEXT NOT NULL,
    canonical_state_event BLOB NOT NULL,
    PRIMARY KEY (state_event_id),
    UNIQUE (aggregate_type, aggregate_id, state_version),
    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (step_id) REFERENCES steps (step_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (attempt_id) REFERENCES attempts (attempt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_authoritative_state_events__aggregate_type__enum CHECK (aggregate_type IN ('TASK','RUN','PHASE_BARRIER','STEP','ATTEMPT')),
    CONSTRAINT ck_authoritative_state_events__payload_digest__sha256 CHECK (length(payload_digest)=71 AND substr(payload_digest,1,7)='sha256:' AND substr(payload_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_authoritative_state_events__schema_version CHECK (schema_version=1),
    CONSTRAINT ck_authoritative_state_events__scope__enum CHECK (scope IN ('TASK','RUN','STEP','ATTEMPT')),
    CONSTRAINT ck_authoritative_state_events__state_event_id__nonempty CHECK (length(state_event_id)>0),
    CONSTRAINT ck_authoritative_state_events__state_version__nonnegative CHECK (state_version>=0),
    CONSTRAINT ck_authoritative_state_events__version_increment CHECK (previous_state_version>=0 AND state_version=previous_state_version+1)
) STRICT;

CREATE INDEX idx_authoritative_state_events__attempt_id ON authoritative_state_events (attempt_id);

CREATE INDEX idx_authoritative_state_events__run_id ON authoritative_state_events (run_id);

CREATE INDEX idx_authoritative_state_events__step_id ON authoritative_state_events (step_id);

CREATE INDEX idx_authoritative_state_events__task_id ON authoritative_state_events (task_id);

CREATE TRIGGER trg_authoritative_state_events__reject_delete BEFORE DELETE ON authoritative_state_events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER trg_authoritative_state_events__reject_update BEFORE UPDATE ON authoritative_state_events FOR EACH ROW BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE artifacts (
    artifact_id TEXT NOT NULL,
    producer_attempt_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    confidentiality TEXT NOT NULL,
    commit_state TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (artifact_id),
    FOREIGN KEY (producer_attempt_id) REFERENCES attempts (attempt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_artifacts__artifact_id__nonempty CHECK (length(artifact_id)>0),
    CONSTRAINT ck_artifacts__commit_state__enum CHECK (commit_state IN ('STAGING','COMMITTED')),
    CONSTRAINT ck_artifacts__digest__sha256 CHECK (length(digest)=71 AND substr(digest,1,7)='sha256:' AND substr(digest,8) NOT GLOB '*[^0-9a-f]*')
) STRICT;

CREATE INDEX idx_artifacts__producer_attempt_id ON artifacts (producer_attempt_id);

CREATE TABLE review_findings (
    finding_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    plan_revision_id TEXT NOT NULL,
    candidate_sha TEXT,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    finding_schema_id TEXT NOT NULL,
    finding_schema_version INTEGER NOT NULL,
    canonical_finding BLOB NOT NULL,
    finding_digest TEXT NOT NULL,
    PRIMARY KEY (finding_id),
    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (plan_revision_id) REFERENCES plan_revisions (plan_revision_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_review_findings__finding_digest__sha256 CHECK (length(finding_digest)=71 AND substr(finding_digest,1,7)='sha256:' AND substr(finding_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_review_findings__finding_id__nonempty CHECK (length(finding_id)>0)
) STRICT;

CREATE INDEX idx_review_findings__plan_revision_id ON review_findings (plan_revision_id);

CREATE INDEX idx_review_findings__task_id ON review_findings (task_id);
