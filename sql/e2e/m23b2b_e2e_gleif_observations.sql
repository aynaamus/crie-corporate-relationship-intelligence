-- E2E DATABASE ONLY (corporate_intelligence_e2e). Never run against corporate_intelligence.
-- Milestone 23B.2B-1: historical GLEIF observations.
--
-- Generated from sql/migrations/m23b2b_gleif_observations.sql; the
-- statements must stay identical (checked by
-- tests/test_gleif_observation_migrations.py). Only names (qualified with
-- corporate_intelligence_e2e) and the seven DELETE triggers differ: here DELETE is
-- allowed only when the session explicitly sets
-- @crie_gleif_maintenance = 1, so tests can remove the synthetic rows
-- they created. UPDATE rules are unchanged.
--
-- Requires an administrative account. Run with the mysql client, without --force.

CREATE TABLE corporate_intelligence_e2e.gleif_source_files (
    source_file_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    dataset ENUM('RR', 'LEI2') NOT NULL,
    file_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    file_size_bytes BIGINT UNSIGNED NOT NULL,
    file_name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,

    -- The header as published: ContentDate verbatim, and in UTC.
    content_date_text VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    content_date_utc DATETIME(3) NOT NULL,
    file_content_text VARCHAR(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    -- What the file itself is: the whole published dataset, the changes
    -- since delta_basis, or not stated by the header.
    publication_scope ENUM('FULL', 'DELTA', 'UNKNOWN') NOT NULL,
    delta_basis VARCHAR(255) NULL,
    header_record_count BIGINT UNSIGNED NULL,
    record_count BIGINT UNSIGNED NOT NULL,

    source_reference VARCHAR(2048) NULL,
    registered_by VARCHAR(255) NOT NULL,
    registered_at_utc TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    UNIQUE KEY uq_gleif_source_file (dataset, file_sha256),
    INDEX idx_gleif_source_content_date (dataset, content_date_utc),

    CONSTRAINT chk_gleif_source_sha256
        CHECK (REGEXP_LIKE(file_sha256, '^[0-9a-f]{64}$', 'c')),
    CONSTRAINT chk_gleif_source_delta
        CHECK ((publication_scope = 'DELTA') = (CHAR_LENGTH(TRIM(COALESCE(delta_basis, ''))) > 0)),
    CONSTRAINT chk_gleif_source_count
        CHECK (header_record_count IS NULL OR header_record_count = record_count),
    CONSTRAINT chk_gleif_source_name
        CHECK (CHAR_LENGTH(TRIM(file_name)) > 0),
    CONSTRAINT chk_gleif_source_registered_by
        CHECK (CHAR_LENGTH(TRIM(registered_by)) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE corporate_intelligence_e2e.gleif_import_batches (
    batch_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_file_id BIGINT UNSIGNED NOT NULL,

    -- Which records of the file this batch imports. ALL_RECORDS is the
    -- whole file; LEI_SET and FIRST_N are samples of it.
    selection_mode ENUM('ALL_RECORDS', 'LEI_SET', 'FIRST_N') NOT NULL,
    selection_limit BIGINT UNSIGNED NULL,
    selection_json JSON NOT NULL,
    selection_fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    selection_description TEXT NOT NULL,
    parser_version VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    record_hash_version SMALLINT UNSIGNED NOT NULL,

    status ENUM('STARTED', 'FAILED', 'COMPLETED') NOT NULL DEFAULT 'STARTED',
    lease_token CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    lease_expires_at_utc DATETIME(6) NOT NULL,
    attempt INT UNSIGNED NOT NULL DEFAULT 1,

    -- Durable progress: records 1..records_scanned of the file are committed.
    records_scanned BIGINT UNSIGNED NOT NULL DEFAULT 0,
    records_selected BIGINT UNSIGNED NOT NULL DEFAULT 0,
    observations_inserted BIGINT UNSIGNED NOT NULL DEFAULT 0,
    observations_already_recorded BIGINT UNSIGNED NOT NULL DEFAULT 0,
    identical_duplicates BIGINT UNSIGNED NOT NULL DEFAULT 0,

    started_by VARCHAR(255) NOT NULL,
    started_at_utc TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    progress_at_utc TIMESTAMP(6) NULL,
    failed_at_utc TIMESTAMP(6) NULL,
    failure_reason TEXT NULL,
    completed_at_utc TIMESTAMP(6) NULL,

    UNIQUE KEY uq_gleif_batch_selection (source_file_id, selection_fingerprint, record_hash_version),
    UNIQUE KEY uq_gleif_batch_file (batch_id, source_file_id),

    CONSTRAINT fk_gleif_batch_source_file
        FOREIGN KEY (source_file_id)
        REFERENCES corporate_intelligence_e2e.gleif_source_files (source_file_id),

    CONSTRAINT chk_gleif_batch_fingerprint
        CHECK (REGEXP_LIKE(selection_fingerprint, '^[0-9a-f]{64}$', 'c')),
    CONSTRAINT chk_gleif_batch_lease
        CHECK (REGEXP_LIKE(lease_token, '^[0-9a-f]{32}$', 'c')),
    CONSTRAINT chk_gleif_batch_description
        CHECK (CHAR_LENGTH(TRIM(selection_description)) > 0),
    CONSTRAINT chk_gleif_batch_started_by
        CHECK (CHAR_LENGTH(TRIM(started_by)) > 0),
    CONSTRAINT chk_gleif_batch_selection
        CHECK (JSON_UNQUOTE(JSON_EXTRACT(selection_json, '$.mode')) = (selection_mode COLLATE utf8mb4_bin)
               AND (selection_mode = 'FIRST_N') = (selection_limit IS NOT NULL)
               AND (selection_limit IS NULL OR (selection_limit >= 1
                    AND selection_limit = CAST(JSON_EXTRACT(selection_json, '$.limit') AS UNSIGNED)))),
    CONSTRAINT chk_gleif_batch_counts
        CHECK (records_selected <= records_scanned
               AND observations_inserted + observations_already_recorded
                   + identical_duplicates = records_selected
               AND (selection_mode = 'LEI_SET' OR records_selected = records_scanned)
               AND (selection_limit IS NULL OR records_scanned <= selection_limit)),
    CONSTRAINT chk_gleif_batch_status_shape
        CHECK ((status = 'STARTED' AND completed_at_utc IS NULL)
            OR (status = 'FAILED' AND completed_at_utc IS NULL AND failed_at_utc IS NOT NULL
                AND CHAR_LENGTH(TRIM(COALESCE(failure_reason, ''))) > 0)
            OR (status = 'COMPLETED' AND completed_at_utc IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE corporate_intelligence_e2e.relationship_observations (
    observation_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_file_id BIGINT UNSIGNED NOT NULL,
    batch_id BIGINT UNSIGNED NOT NULL,
    source_record_ordinal BIGINT UNSIGNED NOT NULL,
    record_hash_version SMALLINT UNSIGNED NOT NULL,
    record_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,

    child_lei CHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
    parent_lei CHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
    relationship_type VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,

    -- As the record states them (NULL: not stated).
    relationship_status VARCHAR(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    registration_status VARCHAR(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    validation_sources VARCHAR(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    validation_documents VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    validation_reference TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    start_node_id_type VARCHAR(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    end_node_id_type VARCHAR(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    qualifiers JSON NULL,
    quantifiers JSON NULL,
    -- GLEIF registration times of the record, not dates of corporate change.
    initial_registration_at_utc DATETIME(3) NULL,
    last_update_at_utc DATETIME(3) NULL,
    next_renewal_at_utc DATETIME(3) NULL,
    managing_lou CHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,

    recorded_at_utc TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    -- One row per distinct record of a file: an identical repeat is the
    -- same row; a conflicting record for the same relationship is a
    -- separate row, never discarded.
    UNIQUE KEY uq_relationship_observation_record
        (source_file_id, record_hash_version, child_lei, parent_lei, relationship_type, record_sha256),
    UNIQUE KEY uq_relationship_observation_ordinal
        (source_file_id, record_hash_version, source_record_ordinal),
    INDEX idx_relationship_observation_identity (child_lei, parent_lei, relationship_type),
    INDEX idx_relationship_observation_batch (batch_id),
    UNIQUE KEY uq_relationship_observation_file (observation_id, source_file_id),

    CONSTRAINT fk_relationship_observation_batch
        FOREIGN KEY (batch_id, source_file_id)
        REFERENCES corporate_intelligence_e2e.gleif_import_batches (batch_id, source_file_id),
    CONSTRAINT fk_relationship_observation_relationship
        FOREIGN KEY (child_lei, parent_lei, relationship_type)
        REFERENCES corporate_intelligence_e2e.corporate_relationships (child_lei, parent_lei, relationship_type),

    CONSTRAINT chk_relationship_observation_sha256
        CHECK (REGEXP_LIKE(record_sha256, '^[0-9a-f]{64}$', 'c')),
    CONSTRAINT chk_relationship_observation_ordinal
        CHECK (source_record_ordinal >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE corporate_intelligence_e2e.relationship_observation_periods (
    observation_id BIGINT UNSIGNED NOT NULL,
    period_index SMALLINT UNSIGNED NOT NULL,
    -- As stated: ACCOUNTING_PERIOD, RELATIONSHIP_PERIOD, DOCUMENT_FILING_PERIOD...
    period_type VARCHAR(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    start_text VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NULL,
    start_at_utc DATETIME(3) NULL,
    end_text VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NULL,
    end_at_utc DATETIME(3) NULL,

    PRIMARY KEY (observation_id, period_index),

    CONSTRAINT fk_relationship_observation_period
        FOREIGN KEY (observation_id)
        REFERENCES corporate_intelligence_e2e.relationship_observations (observation_id),

    CONSTRAINT chk_relationship_observation_period_type
        CHECK (CHAR_LENGTH(TRIM(period_type)) > 0),
    CONSTRAINT chk_relationship_observation_period_dates
        CHECK ((start_text IS NULL) = (start_at_utc IS NULL)
               AND (end_text IS NULL) = (end_at_utc IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE corporate_intelligence_e2e.legal_entity_observations (
    observation_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_file_id BIGINT UNSIGNED NOT NULL,
    batch_id BIGINT UNSIGNED NOT NULL,
    source_record_ordinal BIGINT UNSIGNED NOT NULL,
    record_hash_version SMALLINT UNSIGNED NOT NULL,
    record_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,

    lei CHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,

    -- As the record states them (NULL: not stated). Names are exact text.
    legal_name VARCHAR(1000) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    legal_name_language VARCHAR(20) CHARACTER SET ascii COLLATE ascii_bin NULL,
    other_names JSON NULL,
    entity_status VARCHAR(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    legal_form_code VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    legal_form_other VARCHAR(1000) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    legal_jurisdiction VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    legal_address_country VARCHAR(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    registration_authority_id VARCHAR(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    registration_authority_entity_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    successor_lei CHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    registration_status VARCHAR(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    initial_registration_at_utc DATETIME(3) NULL,
    last_update_at_utc DATETIME(3) NULL,
    next_renewal_at_utc DATETIME(3) NULL,
    managing_lou CHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,

    recorded_at_utc TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    UNIQUE KEY uq_entity_observation_record
        (source_file_id, record_hash_version, lei, record_sha256),
    UNIQUE KEY uq_entity_observation_ordinal
        (source_file_id, record_hash_version, source_record_ordinal),
    INDEX idx_entity_observation_lei (lei),
    INDEX idx_entity_observation_batch (batch_id),
    UNIQUE KEY uq_entity_observation_file (observation_id, source_file_id),

    CONSTRAINT fk_entity_observation_batch
        FOREIGN KEY (batch_id, source_file_id)
        REFERENCES corporate_intelligence_e2e.gleif_import_batches (batch_id, source_file_id),
    CONSTRAINT fk_entity_observation_lei
        FOREIGN KEY (lei)
        REFERENCES corporate_intelligence_e2e.legal_entities (lei),

    CONSTRAINT chk_entity_observation_sha256
        CHECK (REGEXP_LIKE(record_sha256, '^[0-9a-f]{64}$', 'c')),
    CONSTRAINT chk_entity_observation_ordinal
        CHECK (source_record_ordinal >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE corporate_intelligence_e2e.relationship_batch_observations (
    batch_id BIGINT UNSIGNED NOT NULL,
    observation_id BIGINT UNSIGNED NOT NULL,
    source_file_id BIGINT UNSIGNED NOT NULL,
    -- The first record of the file at which this batch's selection met it.
    source_record_ordinal BIGINT UNSIGNED NOT NULL,
    linked_at_utc TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    PRIMARY KEY (batch_id, observation_id),
    UNIQUE KEY uq_relationship_batch_ordinal (batch_id, source_record_ordinal),
    INDEX idx_relationship_batch_observation (observation_id),

    CONSTRAINT fk_relationship_batch_batch
        FOREIGN KEY (batch_id, source_file_id)
        REFERENCES corporate_intelligence_e2e.gleif_import_batches (batch_id, source_file_id),
    CONSTRAINT fk_relationship_batch_observation
        FOREIGN KEY (observation_id, source_file_id)
        REFERENCES corporate_intelligence_e2e.relationship_observations (observation_id, source_file_id),

    CONSTRAINT chk_relationship_batch_ordinal
        CHECK (source_record_ordinal >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE corporate_intelligence_e2e.entity_batch_observations (
    batch_id BIGINT UNSIGNED NOT NULL,
    observation_id BIGINT UNSIGNED NOT NULL,
    source_file_id BIGINT UNSIGNED NOT NULL,
    -- The first record of the file at which this batch's selection met it.
    source_record_ordinal BIGINT UNSIGNED NOT NULL,
    linked_at_utc TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    PRIMARY KEY (batch_id, observation_id),
    UNIQUE KEY uq_entity_batch_ordinal (batch_id, source_record_ordinal),
    INDEX idx_entity_batch_observation (observation_id),

    CONSTRAINT fk_entity_batch_batch
        FOREIGN KEY (batch_id, source_file_id)
        REFERENCES corporate_intelligence_e2e.gleif_import_batches (batch_id, source_file_id),
    CONSTRAINT fk_entity_batch_observation
        FOREIGN KEY (observation_id, source_file_id)
        REFERENCES corporate_intelligence_e2e.legal_entity_observations (observation_id, source_file_id),

    CONSTRAINT chk_entity_batch_ordinal
        CHECK (source_record_ordinal >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE corporate_intelligence_e2e.relationship_evidence
    ADD COLUMN first_batch_id BIGINT UNSIGNED NULL DEFAULT NULL,
    ALGORITHM=INSTANT;

ALTER TABLE corporate_intelligence_e2e.relationship_evidence
    ADD CONSTRAINT fk_evidence_first_batch
        FOREIGN KEY (first_batch_id)
        REFERENCES corporate_intelligence_e2e.gleif_import_batches (batch_id),
    ALGORITHM=COPY;

DELIMITER $$

CREATE TRIGGER corporate_intelligence_e2e.trg_gleif_source_files_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.gleif_source_files FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'gleif_source_files is append-only'$$

CREATE TRIGGER corporate_intelligence_e2e.trg_gleif_source_files_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.gleif_source_files FOR EACH ROW
BEGIN
    IF COALESCE(@crie_gleif_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'gleif_source_files is append-only';
    END IF;
END$$

-- A batch starts empty, under a live lease of the session that starts it.
CREATE TRIGGER corporate_intelligence_e2e.trg_gleif_batches_start
BEFORE INSERT ON corporate_intelligence_e2e.gleif_import_batches FOR EACH ROW
BEGIN
    IF NEW.status <> 'STARTED' OR NEW.attempt <> 1
       OR NEW.records_scanned <> 0 OR NEW.records_selected <> 0
       OR NEW.observations_inserted <> 0 OR NEW.observations_already_recorded <> 0
       OR NEW.identical_duplicates <> 0
       OR NEW.progress_at_utc IS NOT NULL OR NEW.failed_at_utc IS NOT NULL
       OR NEW.failure_reason IS NOT NULL OR NEW.completed_at_utc IS NOT NULL THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a batch must start as an empty STARTED batch';
    END IF;
    IF NOT (NEW.lease_token <=> CAST(@crie_gleif_lease AS BINARY))
       OR NEW.lease_expires_at_utc <= UTC_TIMESTAMP(6)
       OR NEW.lease_expires_at_utc > UTC_TIMESTAMP(6) + INTERVAL 1 HOUR THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a batch must be started under its own live lease';
    END IF;
END$$

-- Lifecycle (now = server UTC time; "holder" = the session presents the
-- current token and the lease has not expired):
--   STARTED -> STARTED, same token   progress and renewal, holder only.
--   STARTED -> STARTED, new token    takeover, only after expiry; attempt + 1,
--                                    progress unchanged.
--   STARTED -> FAILED                the holder, or anyone after expiry;
--                                    records only the failure.
--   FAILED  -> STARTED               resume under a new token; attempt + 1,
--                                    progress unchanged.
--   STARTED -> COMPLETED             holder only, the selection complete and
--                                    every count matched by stored rows.
--   COMPLETED                        final; nothing changes.
-- Every STARTED row's lease expires after now and at most one hour ahead.
CREATE TRIGGER corporate_intelligence_e2e.trg_gleif_batches_lifecycle
BEFORE UPDATE ON corporate_intelligence_e2e.gleif_import_batches FOR EACH ROW
BEGIN
    DECLARE now_utc DATETIME(6) DEFAULT UTC_TIMESTAMP(6);
    DECLARE holder BOOLEAN;
    DECLARE same_progress BOOLEAN;
    DECLARE file_records BIGINT UNSIGNED;
    DECLARE file_dataset VARCHAR(10);
    DECLARE created BIGINT UNSIGNED;
    DECLARE created_linked BIGINT UNSIGNED;
    DECLARE linked BIGINT UNSIGNED;

    IF OLD.status = 'COMPLETED' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a completed batch is immutable';
    END IF;
    IF NOT (NEW.batch_id <=> OLD.batch_id AND NEW.source_file_id <=> OLD.source_file_id
            AND NEW.selection_mode <=> OLD.selection_mode
            AND NEW.selection_limit <=> OLD.selection_limit
            AND NEW.selection_json <=> OLD.selection_json
            AND NEW.selection_fingerprint <=> OLD.selection_fingerprint
            AND NEW.selection_description <=> OLD.selection_description
            AND NEW.parser_version <=> OLD.parser_version
            AND NEW.record_hash_version <=> OLD.record_hash_version
            AND NEW.started_by <=> OLD.started_by
            AND NEW.started_at_utc <=> OLD.started_at_utc) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a batch identity cannot change';
    END IF;
    IF NEW.records_scanned < OLD.records_scanned OR NEW.records_selected < OLD.records_selected
       OR NEW.observations_inserted < OLD.observations_inserted
       OR NEW.observations_already_recorded < OLD.observations_already_recorded
       OR NEW.identical_duplicates < OLD.identical_duplicates THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'batch progress cannot go backwards';
    END IF;

    SET holder = (OLD.lease_token <=> CAST(@crie_gleif_lease AS BINARY))
                 AND OLD.lease_expires_at_utc > now_utc;
    SET same_progress = NEW.records_scanned <=> OLD.records_scanned
        AND NEW.records_selected <=> OLD.records_selected
        AND NEW.observations_inserted <=> OLD.observations_inserted
        AND NEW.observations_already_recorded <=> OLD.observations_already_recorded
        AND NEW.identical_duplicates <=> OLD.identical_duplicates;

    SELECT record_count, dataset INTO file_records, file_dataset
    FROM corporate_intelligence_e2e.gleif_source_files
    WHERE source_file_id = NEW.source_file_id;
    IF NEW.records_scanned > file_records THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a batch cannot scan more records than its file has';
    END IF;

    IF NEW.status = 'STARTED' AND (NEW.lease_expires_at_utc <= now_utc
            OR NEW.lease_expires_at_utc > now_utc + INTERVAL 1 HOUR) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a started batch needs a live lease of at most one hour';
    END IF;

    IF OLD.status = 'STARTED' AND NEW.status = 'STARTED'
       AND NEW.lease_token <=> OLD.lease_token THEN
        IF NOT holder OR NOT (NEW.attempt <=> OLD.attempt) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'only the lease holder may advance a batch';
        END IF;
    ELSEIF OLD.status = 'STARTED' AND NEW.status = 'STARTED' THEN
        IF OLD.lease_expires_at_utc > now_utc THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a live lease cannot be taken over';
        END IF;
        IF NOT (NEW.lease_token <=> CAST(@crie_gleif_lease AS BINARY))
           OR NEW.attempt <> OLD.attempt + 1 OR NOT same_progress THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a takeover resumes from its progress under a new lease';
        END IF;
    ELSEIF OLD.status = 'STARTED' AND NEW.status = 'FAILED' THEN
        IF NOT holder AND OLD.lease_expires_at_utc > now_utc THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'only the lease holder may fail a batch before its lease expires';
        END IF;
        IF NOT (NEW.lease_token <=> OLD.lease_token AND NEW.attempt <=> OLD.attempt
                AND same_progress) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'failing a batch records only the failure';
        END IF;
    ELSEIF OLD.status = 'FAILED' AND NEW.status = 'STARTED' THEN
        IF NEW.lease_token <=> OLD.lease_token
           OR NOT (NEW.lease_token <=> CAST(@crie_gleif_lease AS BINARY))
           OR NEW.attempt <> OLD.attempt + 1 OR NOT same_progress THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a failed batch resumes from its progress under a new lease';
        END IF;
    ELSEIF OLD.status = 'STARTED' AND NEW.status = 'COMPLETED' THEN
        IF NOT holder OR NOT (NEW.lease_token <=> OLD.lease_token AND NEW.attempt <=> OLD.attempt) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'only the lease holder may complete a batch';
        END IF;
        IF NOT (CASE NEW.selection_mode
                    WHEN 'FIRST_N' THEN NEW.records_scanned = LEAST(NEW.selection_limit, file_records)
                    ELSE NEW.records_scanned = file_records
                END) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a batch is complete only when its selection is fully scanned';
        END IF;
        IF file_dataset = 'RR' THEN
            SELECT COUNT(*), COUNT(m.observation_id) INTO created, created_linked
            FROM corporate_intelligence_e2e.relationship_observations o
            LEFT JOIN corporate_intelligence_e2e.relationship_batch_observations m
              ON m.batch_id = o.batch_id AND m.observation_id = o.observation_id
            WHERE o.batch_id = NEW.batch_id;
            SELECT COUNT(*) INTO linked FROM corporate_intelligence_e2e.relationship_batch_observations
            WHERE batch_id = NEW.batch_id;
        ELSE
            SELECT COUNT(*), COUNT(m.observation_id) INTO created, created_linked
            FROM corporate_intelligence_e2e.legal_entity_observations o
            LEFT JOIN corporate_intelligence_e2e.entity_batch_observations m
              ON m.batch_id = o.batch_id AND m.observation_id = o.observation_id
            WHERE o.batch_id = NEW.batch_id;
            SELECT COUNT(*) INTO linked FROM corporate_intelligence_e2e.entity_batch_observations
            WHERE batch_id = NEW.batch_id;
        END IF;
        IF created <> NEW.observations_inserted OR created_linked <> created
           OR linked <> NEW.observations_inserted + NEW.observations_already_recorded THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'batch counts do not match its recorded observations';
        END IF;
    ELSE
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'batch status transition not permitted';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_gleif_batches_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.gleif_import_batches FOR EACH ROW
BEGIN
    IF COALESCE(@crie_gleif_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'gleif_import_batches is append-only';
    END IF;
END$$

-- Rows are written only for the batch whose live lease the session holds,
-- for records after its committed progress and inside its selection.

CREATE TRIGGER corporate_intelligence_e2e.trg_relationship_observations_batch
BEFORE INSERT ON corporate_intelligence_e2e.relationship_observations FOR EACH ROW
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM corporate_intelligence_e2e.gleif_import_batches b
        JOIN corporate_intelligence_e2e.gleif_source_files f ON f.source_file_id = b.source_file_id
        WHERE b.batch_id = NEW.batch_id
          AND b.status = 'STARTED'
          AND b.lease_token <=> CAST(@crie_gleif_lease AS BINARY)
          AND b.lease_expires_at_utc > UTC_TIMESTAMP(6)
          AND f.dataset = 'RR'
          AND b.record_hash_version = NEW.record_hash_version
          AND NEW.source_record_ordinal > b.records_scanned
          AND NEW.source_record_ordinal <= f.record_count
          AND (b.selection_limit IS NULL OR NEW.source_record_ordinal <= b.selection_limit)
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'observation does not belong to the leased batch in progress';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_relationship_observations_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.relationship_observations FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'relationship_observations is append-only'$$

CREATE TRIGGER corporate_intelligence_e2e.trg_relationship_observations_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.relationship_observations FOR EACH ROW
BEGIN
    IF COALESCE(@crie_gleif_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'relationship_observations is append-only';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_relationship_observation_periods_batch
BEFORE INSERT ON corporate_intelligence_e2e.relationship_observation_periods FOR EACH ROW
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM corporate_intelligence_e2e.relationship_observations o
        JOIN corporate_intelligence_e2e.gleif_import_batches b ON b.batch_id = o.batch_id
        WHERE o.observation_id = NEW.observation_id
          AND b.status = 'STARTED'
          AND b.lease_token <=> CAST(@crie_gleif_lease AS BINARY)
          AND b.lease_expires_at_utc > UTC_TIMESTAMP(6)
          AND o.source_record_ordinal > b.records_scanned
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'period does not belong to the leased batch in progress';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_relationship_observation_periods_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.relationship_observation_periods FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'relationship_observation_periods is append-only'$$

CREATE TRIGGER corporate_intelligence_e2e.trg_relationship_observation_periods_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.relationship_observation_periods FOR EACH ROW
BEGIN
    IF COALESCE(@crie_gleif_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'relationship_observation_periods is append-only';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_entity_observations_batch
BEFORE INSERT ON corporate_intelligence_e2e.legal_entity_observations FOR EACH ROW
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM corporate_intelligence_e2e.gleif_import_batches b
        JOIN corporate_intelligence_e2e.gleif_source_files f ON f.source_file_id = b.source_file_id
        WHERE b.batch_id = NEW.batch_id
          AND b.status = 'STARTED'
          AND b.lease_token <=> CAST(@crie_gleif_lease AS BINARY)
          AND b.lease_expires_at_utc > UTC_TIMESTAMP(6)
          AND f.dataset = 'LEI2'
          AND b.record_hash_version = NEW.record_hash_version
          AND NEW.source_record_ordinal > b.records_scanned
          AND NEW.source_record_ordinal <= f.record_count
          AND (b.selection_limit IS NULL OR NEW.source_record_ordinal <= b.selection_limit)
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'observation does not belong to the leased batch in progress';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_entity_observations_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.legal_entity_observations FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'legal_entity_observations is append-only'$$

CREATE TRIGGER corporate_intelligence_e2e.trg_entity_observations_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.legal_entity_observations FOR EACH ROW
BEGIN
    IF COALESCE(@crie_gleif_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'legal_entity_observations is append-only';
    END IF;
END$$

-- A batch includes an observation it met in its own selection: one it
-- created, or one an overlapping batch created first at the same or an
-- earlier record of the same file and record hash version.
CREATE TRIGGER corporate_intelligence_e2e.trg_relationship_batch_observations_batch
BEFORE INSERT ON corporate_intelligence_e2e.relationship_batch_observations FOR EACH ROW
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM corporate_intelligence_e2e.gleif_import_batches b
        JOIN corporate_intelligence_e2e.gleif_source_files f ON f.source_file_id = b.source_file_id
        JOIN corporate_intelligence_e2e.relationship_observations o ON o.observation_id = NEW.observation_id
        WHERE b.batch_id = NEW.batch_id
          AND b.status = 'STARTED'
          AND b.lease_token <=> CAST(@crie_gleif_lease AS BINARY)
          AND b.lease_expires_at_utc > UTC_TIMESTAMP(6)
          AND NEW.source_record_ordinal > b.records_scanned
          AND NEW.source_record_ordinal <= f.record_count
          AND (b.selection_limit IS NULL OR NEW.source_record_ordinal <= b.selection_limit)
          AND o.record_hash_version = b.record_hash_version
          AND o.source_record_ordinal <= NEW.source_record_ordinal
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'membership does not belong to the leased batch in progress';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_relationship_batch_observations_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.relationship_batch_observations FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'relationship_batch_observations is append-only'$$

CREATE TRIGGER corporate_intelligence_e2e.trg_relationship_batch_observations_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.relationship_batch_observations FOR EACH ROW
BEGIN
    IF COALESCE(@crie_gleif_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'relationship_batch_observations is append-only';
    END IF;
END$$

-- A batch includes an observation it met in its own selection: one it
-- created, or one an overlapping batch created first at the same or an
-- earlier record of the same file and record hash version.
CREATE TRIGGER corporate_intelligence_e2e.trg_entity_batch_observations_batch
BEFORE INSERT ON corporate_intelligence_e2e.entity_batch_observations FOR EACH ROW
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM corporate_intelligence_e2e.gleif_import_batches b
        JOIN corporate_intelligence_e2e.gleif_source_files f ON f.source_file_id = b.source_file_id
        JOIN corporate_intelligence_e2e.legal_entity_observations o ON o.observation_id = NEW.observation_id
        WHERE b.batch_id = NEW.batch_id
          AND b.status = 'STARTED'
          AND b.lease_token <=> CAST(@crie_gleif_lease AS BINARY)
          AND b.lease_expires_at_utc > UTC_TIMESTAMP(6)
          AND NEW.source_record_ordinal > b.records_scanned
          AND NEW.source_record_ordinal <= f.record_count
          AND (b.selection_limit IS NULL OR NEW.source_record_ordinal <= b.selection_limit)
          AND o.record_hash_version = b.record_hash_version
          AND o.source_record_ordinal <= NEW.source_record_ordinal
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'membership does not belong to the leased batch in progress';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_entity_batch_observations_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.entity_batch_observations FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'entity_batch_observations is append-only'$$

CREATE TRIGGER corporate_intelligence_e2e.trg_entity_batch_observations_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.entity_batch_observations FOR EACH ROW
BEGIN
    IF COALESCE(@crie_gleif_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'entity_batch_observations is append-only';
    END IF;
END$$

-- A new anchor names the leased RR batch that created it, at that file's
-- publication time (rounded to the second as legacy anchors were).
CREATE TRIGGER corporate_intelligence_e2e.trg_evidence_first_batch_insert
BEFORE INSERT ON corporate_intelligence_e2e.relationship_evidence FOR EACH ROW
BEGIN
    IF NEW.first_batch_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM corporate_intelligence_e2e.gleif_import_batches b
        JOIN corporate_intelligence_e2e.gleif_source_files f ON f.source_file_id = b.source_file_id
        WHERE b.batch_id = NEW.first_batch_id
          AND b.status = 'STARTED'
          AND b.lease_token <=> CAST(@crie_gleif_lease AS BINARY)
          AND b.lease_expires_at_utc > UTC_TIMESTAMP(6)
          AND f.dataset = 'RR'
          AND CAST(f.content_date_utc AS DATETIME) = NEW.snapshot_at_utc
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'anchor does not match the leased batch in progress';
    END IF;
END$$

-- Existing anchors keep NULL; no anchor's batch can ever be changed.
CREATE TRIGGER corporate_intelligence_e2e.trg_evidence_first_batch_fixed
BEFORE UPDATE ON corporate_intelligence_e2e.relationship_evidence FOR EACH ROW
BEGIN
    IF NOT (NEW.first_batch_id <=> OLD.first_batch_id) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'relationship_evidence.first_batch_id cannot change';
    END IF;
END$$

DELIMITER ;
