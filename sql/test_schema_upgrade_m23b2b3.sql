-- TEST DATABASE ONLY. Never run against corporate_intelligence.
-- Milestone 23B.2B-3a-2: GLEIF quarantine, header metadata, LEI2 columns.
--
-- Generated from sql/migrations/m23b2b3_gleif_quarantine.sql; the
-- statements must stay identical (checked by
-- tests/test_gleif_quarantine_migrations.py). Only names (qualified with
-- corporate_intelligence_test) and the five new DELETE triggers differ: here DELETE is
-- allowed only when the session explicitly sets
-- @crie_gleif_maintenance = 1, so tests can remove the synthetic rows
-- they created. Requires sql/test_schema_upgrade_m23b2b.sql (or its E2E
-- twin) to be installed first. No GLEIF importer may run meanwhile.
--
-- Requires an administrative account. Run with the mysql client, without --force.
-- Not idempotent: after any failure, run sql/m23b2b3_installation_check.sql
-- and do not rerun this script.

ALTER TABLE corporate_intelligence_test.gleif_source_files
    ADD COLUMN originator CHAR(20) CHARACTER SET ascii COLLATE ascii_bin NULL,
    ADD COLUMN header_extension JSON NULL,
    ADD COLUMN header_source_counts_consistent BOOLEAN NULL,
    ADD CONSTRAINT chk_gleif_source_originator
        CHECK (originator IS NULL OR REGEXP_LIKE(originator, '^[0-9A-Z]{18}[0-9]{2}$', 'c')),
    ADD CONSTRAINT chk_gleif_source_header_extension
        CHECK ((header_extension IS NULL) = (header_source_counts_consistent IS NULL)
               AND (header_extension IS NULL OR JSON_TYPE(header_extension) = 'OBJECT'));

ALTER TABLE corporate_intelligence_test.legal_entity_observations
    ADD COLUMN canonical_record JSON NULL,
    ADD COLUMN entity_category VARCHAR(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    ADD COLUMN entity_sub_category VARCHAR(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    ADD COLUMN entity_created_at_utc DATETIME(3) NULL,
    ADD COLUMN validation_sources VARCHAR(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    ADD COLUMN other_registration_authority_id VARCHAR(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    ADD COLUMN identifier_check ENUM('VALID', 'SECONDARY_IDENTIFIER_FLAGGED') NULL,
    ADD COLUMN identifier_flags JSON NULL,
    ADD CONSTRAINT chk_entity_observation_canonical
        CHECK (canonical_record IS NOT NULL AND JSON_TYPE(canonical_record) = 'OBJECT'
               AND identifier_check IS NOT NULL
               AND (identifier_check = 'VALID') = (identifier_flags IS NULL)
               AND (identifier_flags IS NULL OR (JSON_TYPE(identifier_flags) = 'ARRAY'
                                                 AND JSON_LENGTH(identifier_flags) > 0)));

ALTER TABLE corporate_intelligence_test.relationship_observations
    ADD COLUMN identifier_flags JSON NULL,
    ADD CONSTRAINT chk_relationship_observation_identifier_flags
        CHECK (identifier_flags IS NULL OR (JSON_TYPE(identifier_flags) = 'ARRAY'
                                            AND JSON_LENGTH(identifier_flags) > 0));

-- The replacement counts rule is added beside the original. Together they
-- force both quarantine counters to 0 until the original is dropped
-- (step 5), so nothing new is allowed in between.
ALTER TABLE corporate_intelligence_test.gleif_import_batches
    ADD COLUMN quarantined_inserted BIGINT UNSIGNED NOT NULL DEFAULT 0,
    ADD COLUMN quarantined_already_recorded BIGINT UNSIGNED NOT NULL DEFAULT 0,
    ADD CONSTRAINT chk_gleif_batch_counts_v2
        CHECK (records_selected <= records_scanned
               AND observations_inserted + observations_already_recorded
                   + quarantined_inserted + quarantined_already_recorded
                   + identical_duplicates = records_selected
               AND (selection_mode = 'LEI_SET' OR records_selected = records_scanned)
               AND (selection_limit IS NULL OR records_scanned <= selection_limit));

-- A validly parsed source record that cannot be trusted as identity
-- evidence, kept whole. One row per distinct record of a file (an
-- identical repeat is the same row); identifiers and flags are kept as
-- stated. No foreign key reaches legal_entities or corporate_relationships.
CREATE TABLE corporate_intelligence_test.gleif_quarantined_records (
    quarantine_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    dataset ENUM('RR', 'LEI2') NOT NULL,
    source_file_id BIGINT UNSIGNED NOT NULL,
    batch_id BIGINT UNSIGNED NOT NULL,
    source_record_ordinal BIGINT UNSIGNED NOT NULL,
    record_hash_version SMALLINT UNSIGNED NOT NULL,
    record_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    -- The record's subject as stated: RR "child|parent|type", LEI2 the LEI.
    subject_key VARCHAR(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    canonical_record JSON NOT NULL,
    -- [{role, value, node_id_type, issue}, ...] for every flagged identifier.
    identifiers JSON NOT NULL,
    reason_flags JSON NOT NULL,
    recorded_at_utc TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    UNIQUE KEY uq_quarantine_record
        (source_file_id, record_hash_version, subject_key, record_sha256),
    UNIQUE KEY uq_quarantine_ordinal (source_file_id, record_hash_version, source_record_ordinal),
    UNIQUE KEY uq_quarantine_file (quarantine_id, source_file_id),
    INDEX idx_quarantine_batch (batch_id),

    CONSTRAINT fk_quarantine_batch
        FOREIGN KEY (batch_id, source_file_id)
        REFERENCES corporate_intelligence_test.gleif_import_batches (batch_id, source_file_id),

    CONSTRAINT chk_quarantine_sha256
        CHECK (REGEXP_LIKE(record_sha256, '^[0-9a-f]{64}$', 'c')),
    CONSTRAINT chk_quarantine_ordinal
        CHECK (source_record_ordinal >= 1),
    CONSTRAINT chk_quarantine_subject
        CHECK (CHAR_LENGTH(TRIM(subject_key)) > 0),
    CONSTRAINT chk_quarantine_json
        CHECK (JSON_TYPE(canonical_record) = 'OBJECT'
               AND JSON_TYPE(identifiers) = 'ARRAY' AND JSON_LENGTH(identifiers) > 0
               AND JSON_TYPE(reason_flags) = 'ARRAY' AND JSON_LENGTH(reason_flags) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Which quarantined records each batch's selection includes, including
-- those an overlapping batch recorded first.
CREATE TABLE corporate_intelligence_test.gleif_batch_quarantined_records (
    batch_id BIGINT UNSIGNED NOT NULL,
    quarantine_id BIGINT UNSIGNED NOT NULL,
    source_file_id BIGINT UNSIGNED NOT NULL,
    source_record_ordinal BIGINT UNSIGNED NOT NULL,
    linked_at_utc TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    PRIMARY KEY (batch_id, quarantine_id),
    UNIQUE KEY uq_quarantine_batch_ordinal (batch_id, source_record_ordinal),
    INDEX idx_quarantine_batch_record (quarantine_id),

    CONSTRAINT fk_quarantine_batch_batch
        FOREIGN KEY (batch_id, source_file_id)
        REFERENCES corporate_intelligence_test.gleif_import_batches (batch_id, source_file_id),
    CONSTRAINT fk_quarantine_batch_record
        FOREIGN KEY (quarantine_id, source_file_id)
        REFERENCES corporate_intelligence_test.gleif_quarantined_records (quarantine_id, source_file_id),

    CONSTRAINT chk_quarantine_batch_ordinal
        CHECK (source_record_ordinal >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Every selected record that repeats, byte for byte in canonical form, a
-- record the batch already includes: its own position, and the position
-- of the record it repeats. identical_duplicates counts these rows.
CREATE TABLE corporate_intelligence_test.gleif_batch_duplicate_occurrences (
    batch_id BIGINT UNSIGNED NOT NULL,
    source_file_id BIGINT UNSIGNED NOT NULL,
    source_record_ordinal BIGINT UNSIGNED NOT NULL,
    repeats_ordinal BIGINT UNSIGNED NOT NULL,
    record_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    recorded_at_utc TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    PRIMARY KEY (batch_id, source_record_ordinal),

    CONSTRAINT fk_duplicate_occurrence_batch
        FOREIGN KEY (batch_id, source_file_id)
        REFERENCES corporate_intelligence_test.gleif_import_batches (batch_id, source_file_id),

    CONSTRAINT chk_duplicate_occurrence_order
        CHECK (repeats_ordinal >= 1 AND repeats_ordinal < source_record_ordinal),
    CONSTRAINT chk_duplicate_occurrence_sha256
        CHECK (REGEXP_LIKE(record_sha256, '^[0-9a-f]{64}$', 'c'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Position registries. Each row is written by an AFTER INSERT trigger of
-- the table it stands for, so no writer can skip it, and its primary key
-- makes exclusivity atomic: InnoDB enforces a unique key with row locks,
-- so concurrent writers cannot both succeed (a NOT EXISTS check in a
-- trigger could not guarantee that).
--   gleif_source_positions: a position of a file (per record hash version)
--     is either an observation or quarantined, never both.
--   gleif_batch_positions: within a batch, each selected position is
--     exactly one of trusted member, quarantined member or duplicate.
CREATE TABLE corporate_intelligence_test.gleif_source_positions (
    source_file_id BIGINT UNSIGNED NOT NULL,
    record_hash_version SMALLINT UNSIGNED NOT NULL,
    source_record_ordinal BIGINT UNSIGNED NOT NULL,
    kind ENUM('OBSERVATION', 'QUARANTINE') NOT NULL,

    PRIMARY KEY (source_file_id, record_hash_version, source_record_ordinal),

    CONSTRAINT fk_source_position_file
        FOREIGN KEY (source_file_id)
        REFERENCES corporate_intelligence_test.gleif_source_files (source_file_id),
    CONSTRAINT chk_source_position_ordinal
        CHECK (source_record_ordinal >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE corporate_intelligence_test.gleif_batch_positions (
    batch_id BIGINT UNSIGNED NOT NULL,
    source_record_ordinal BIGINT UNSIGNED NOT NULL,
    kind ENUM('TRUSTED', 'QUARANTINED', 'DUPLICATE') NOT NULL,

    PRIMARY KEY (batch_id, source_record_ordinal),

    CONSTRAINT fk_batch_position_batch
        FOREIGN KEY (batch_id)
        REFERENCES corporate_intelligence_test.gleif_import_batches (batch_id),
    CONSTRAINT chk_batch_position_ordinal
        CHECK (source_record_ordinal >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

DELIMITER $$

-- Quarantined records are written only by the lease holder of a STARTED
-- batch of the same dataset, inside its selection, at a position that is
-- not already an observation.
CREATE TRIGGER corporate_intelligence_test.trg_quarantined_records_batch
BEFORE INSERT ON corporate_intelligence_test.gleif_quarantined_records FOR EACH ROW
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM corporate_intelligence_test.gleif_import_batches b
        JOIN corporate_intelligence_test.gleif_source_files f ON f.source_file_id = b.source_file_id
        WHERE b.batch_id = NEW.batch_id
          AND b.status = 'STARTED'
          AND b.lease_token <=> CAST(@crie_gleif_lease AS BINARY)
          AND b.lease_expires_at_utc > UTC_TIMESTAMP(6)
          AND f.dataset = NEW.dataset
          AND b.record_hash_version = NEW.record_hash_version
          AND NEW.source_record_ordinal > b.records_scanned
          AND NEW.source_record_ordinal <= f.record_count
          AND (b.selection_limit IS NULL OR NEW.source_record_ordinal <= b.selection_limit)
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'quarantined record does not belong to the leased batch in progress';
    END IF;
    IF EXISTS (
        SELECT 1 FROM corporate_intelligence_test.relationship_observations o
        WHERE o.source_file_id = NEW.source_file_id AND o.record_hash_version = NEW.record_hash_version
          AND o.source_record_ordinal = NEW.source_record_ordinal
    ) OR EXISTS (
        SELECT 1 FROM corporate_intelligence_test.legal_entity_observations o
        WHERE o.source_file_id = NEW.source_file_id AND o.record_hash_version = NEW.record_hash_version
          AND o.source_record_ordinal = NEW.source_record_ordinal
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a source position cannot be both an observation and quarantined';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_test.trg_quarantined_records_no_update
BEFORE UPDATE ON corporate_intelligence_test.gleif_quarantined_records FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'gleif_quarantined_records is append-only'$$

CREATE TRIGGER corporate_intelligence_test.trg_quarantined_records_no_delete
BEFORE DELETE ON corporate_intelligence_test.gleif_quarantined_records FOR EACH ROW
BEGIN
    IF COALESCE(@crie_gleif_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'gleif_quarantined_records is append-only';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_test.trg_batch_quarantined_records_batch
BEFORE INSERT ON corporate_intelligence_test.gleif_batch_quarantined_records FOR EACH ROW
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM corporate_intelligence_test.gleif_import_batches b
        JOIN corporate_intelligence_test.gleif_source_files f ON f.source_file_id = b.source_file_id
        JOIN corporate_intelligence_test.gleif_quarantined_records q ON q.quarantine_id = NEW.quarantine_id
        WHERE b.batch_id = NEW.batch_id
          AND b.status = 'STARTED'
          AND b.lease_token <=> CAST(@crie_gleif_lease AS BINARY)
          AND b.lease_expires_at_utc > UTC_TIMESTAMP(6)
          AND NEW.source_record_ordinal > b.records_scanned
          AND NEW.source_record_ordinal <= f.record_count
          AND (b.selection_limit IS NULL OR NEW.source_record_ordinal <= b.selection_limit)
          AND q.record_hash_version = b.record_hash_version
          AND q.source_record_ordinal <= NEW.source_record_ordinal
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'quarantine membership does not belong to the leased batch in progress';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_test.trg_batch_quarantined_records_no_update
BEFORE UPDATE ON corporate_intelligence_test.gleif_batch_quarantined_records FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'gleif_batch_quarantined_records is append-only'$$

CREATE TRIGGER corporate_intelligence_test.trg_batch_quarantined_records_no_delete
BEFORE DELETE ON corporate_intelligence_test.gleif_batch_quarantined_records FOR EACH ROW
BEGIN
    IF COALESCE(@crie_gleif_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'gleif_batch_quarantined_records is append-only';
    END IF;
END$$

-- A duplicate occurrence must repeat, with the same record hash, a record
-- this batch already includes at an earlier position.
CREATE TRIGGER corporate_intelligence_test.trg_duplicate_occurrences_batch
BEFORE INSERT ON corporate_intelligence_test.gleif_batch_duplicate_occurrences FOR EACH ROW
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM corporate_intelligence_test.gleif_import_batches b
        JOIN corporate_intelligence_test.gleif_source_files f ON f.source_file_id = b.source_file_id
        WHERE b.batch_id = NEW.batch_id
          AND b.status = 'STARTED'
          AND b.lease_token <=> CAST(@crie_gleif_lease AS BINARY)
          AND b.lease_expires_at_utc > UTC_TIMESTAMP(6)
          AND NEW.source_record_ordinal > b.records_scanned
          AND NEW.source_record_ordinal <= f.record_count
          AND (b.selection_limit IS NULL OR NEW.source_record_ordinal <= b.selection_limit)
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'duplicate occurrence does not belong to the leased batch in progress';
    END IF;
    IF NOT (EXISTS (
        SELECT 1 FROM corporate_intelligence_test.relationship_batch_observations m
        JOIN corporate_intelligence_test.relationship_observations o ON o.observation_id = m.observation_id
        WHERE m.batch_id = NEW.batch_id AND m.source_record_ordinal = NEW.repeats_ordinal
          AND o.record_sha256 = NEW.record_sha256
    ) OR EXISTS (
        SELECT 1 FROM corporate_intelligence_test.entity_batch_observations m
        JOIN corporate_intelligence_test.legal_entity_observations o ON o.observation_id = m.observation_id
        WHERE m.batch_id = NEW.batch_id AND m.source_record_ordinal = NEW.repeats_ordinal
          AND o.record_sha256 = NEW.record_sha256
    ) OR EXISTS (
        SELECT 1 FROM corporate_intelligence_test.gleif_batch_quarantined_records m
        JOIN corporate_intelligence_test.gleif_quarantined_records q ON q.quarantine_id = m.quarantine_id
        WHERE m.batch_id = NEW.batch_id AND m.source_record_ordinal = NEW.repeats_ordinal
          AND q.record_sha256 = NEW.record_sha256
    )) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a duplicate occurrence must repeat a record this batch includes';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_test.trg_duplicate_occurrences_no_update
BEFORE UPDATE ON corporate_intelligence_test.gleif_batch_duplicate_occurrences FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'gleif_batch_duplicate_occurrences is append-only'$$

CREATE TRIGGER corporate_intelligence_test.trg_duplicate_occurrences_no_delete
BEFORE DELETE ON corporate_intelligence_test.gleif_batch_duplicate_occurrences FOR EACH ROW
BEGIN
    IF COALESCE(@crie_gleif_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'gleif_batch_duplicate_occurrences is append-only';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_test.trg_relationship_observations_not_quarantined
BEFORE INSERT ON corporate_intelligence_test.relationship_observations FOR EACH ROW
FOLLOWS trg_relationship_observations_batch
BEGIN
    IF EXISTS (
        SELECT 1 FROM corporate_intelligence_test.gleif_quarantined_records q
        WHERE q.source_file_id = NEW.source_file_id AND q.record_hash_version = NEW.record_hash_version
          AND q.source_record_ordinal = NEW.source_record_ordinal
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a source position cannot be both an observation and quarantined';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_test.trg_entity_observations_not_quarantined
BEFORE INSERT ON corporate_intelligence_test.legal_entity_observations FOR EACH ROW
FOLLOWS trg_entity_observations_batch
BEGIN
    IF EXISTS (
        SELECT 1 FROM corporate_intelligence_test.gleif_quarantined_records q
        WHERE q.source_file_id = NEW.source_file_id AND q.record_hash_version = NEW.record_hash_version
          AND q.source_record_ordinal = NEW.source_record_ordinal
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a source position cannot be both an observation and quarantined';
    END IF;
END$$

-- Register every position (see the position tables above).

CREATE TRIGGER corporate_intelligence_test.trg_relationship_observations_position
AFTER INSERT ON corporate_intelligence_test.relationship_observations FOR EACH ROW
INSERT INTO corporate_intelligence_test.gleif_source_positions (source_file_id, record_hash_version, source_record_ordinal, kind)
VALUES (NEW.source_file_id, NEW.record_hash_version, NEW.source_record_ordinal, 'OBSERVATION')$$

CREATE TRIGGER corporate_intelligence_test.trg_entity_observations_position
AFTER INSERT ON corporate_intelligence_test.legal_entity_observations FOR EACH ROW
INSERT INTO corporate_intelligence_test.gleif_source_positions (source_file_id, record_hash_version, source_record_ordinal, kind)
VALUES (NEW.source_file_id, NEW.record_hash_version, NEW.source_record_ordinal, 'OBSERVATION')$$

CREATE TRIGGER corporate_intelligence_test.trg_quarantined_records_position
AFTER INSERT ON corporate_intelligence_test.gleif_quarantined_records FOR EACH ROW
INSERT INTO corporate_intelligence_test.gleif_source_positions (source_file_id, record_hash_version, source_record_ordinal, kind)
VALUES (NEW.source_file_id, NEW.record_hash_version, NEW.source_record_ordinal, 'QUARANTINE')$$

CREATE TRIGGER corporate_intelligence_test.trg_relationship_batch_observations_position
AFTER INSERT ON corporate_intelligence_test.relationship_batch_observations FOR EACH ROW
INSERT INTO corporate_intelligence_test.gleif_batch_positions (batch_id, source_record_ordinal, kind)
VALUES (NEW.batch_id, NEW.source_record_ordinal, 'TRUSTED')$$

CREATE TRIGGER corporate_intelligence_test.trg_entity_batch_observations_position
AFTER INSERT ON corporate_intelligence_test.entity_batch_observations FOR EACH ROW
INSERT INTO corporate_intelligence_test.gleif_batch_positions (batch_id, source_record_ordinal, kind)
VALUES (NEW.batch_id, NEW.source_record_ordinal, 'TRUSTED')$$

CREATE TRIGGER corporate_intelligence_test.trg_batch_quarantined_records_position
AFTER INSERT ON corporate_intelligence_test.gleif_batch_quarantined_records FOR EACH ROW
INSERT INTO corporate_intelligence_test.gleif_batch_positions (batch_id, source_record_ordinal, kind)
VALUES (NEW.batch_id, NEW.source_record_ordinal, 'QUARANTINED')$$

CREATE TRIGGER corporate_intelligence_test.trg_duplicate_occurrences_position
AFTER INSERT ON corporate_intelligence_test.gleif_batch_duplicate_occurrences FOR EACH ROW
INSERT INTO corporate_intelligence_test.gleif_batch_positions (batch_id, source_record_ordinal, kind)
VALUES (NEW.batch_id, NEW.source_record_ordinal, 'DUPLICATE')$$

CREATE TRIGGER corporate_intelligence_test.trg_source_positions_no_update
BEFORE UPDATE ON corporate_intelligence_test.gleif_source_positions FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'gleif_source_positions is append-only'$$

CREATE TRIGGER corporate_intelligence_test.trg_source_positions_no_delete
BEFORE DELETE ON corporate_intelligence_test.gleif_source_positions FOR EACH ROW
BEGIN
    IF COALESCE(@crie_gleif_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'gleif_source_positions is append-only';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_test.trg_batch_positions_no_update
BEFORE UPDATE ON corporate_intelligence_test.gleif_batch_positions FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'gleif_batch_positions is append-only'$$

CREATE TRIGGER corporate_intelligence_test.trg_batch_positions_no_delete
BEFORE DELETE ON corporate_intelligence_test.gleif_batch_positions FOR EACH ROW
BEGIN
    IF COALESCE(@crie_gleif_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'gleif_batch_positions is append-only';
    END IF;
END$$

-- Replacements, each FOLLOWS its original: while both exist the combined
-- rule is the stricter one (every original clause is repeated verbatim).
CREATE TRIGGER corporate_intelligence_test.trg_gleif_batches_start_v2
BEFORE INSERT ON corporate_intelligence_test.gleif_import_batches FOR EACH ROW
FOLLOWS trg_gleif_batches_start
BEGIN
    IF NEW.status <> 'STARTED' OR NEW.attempt <> 1
       OR NEW.records_scanned <> 0 OR NEW.records_selected <> 0
       OR NEW.observations_inserted <> 0 OR NEW.observations_already_recorded <> 0
       OR NEW.identical_duplicates <> 0
       OR NEW.quarantined_inserted <> 0 OR NEW.quarantined_already_recorded <> 0
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

CREATE TRIGGER corporate_intelligence_test.trg_gleif_batches_lifecycle_v2
BEFORE UPDATE ON corporate_intelligence_test.gleif_import_batches FOR EACH ROW
FOLLOWS trg_gleif_batches_lifecycle
BEGIN
    DECLARE now_utc DATETIME(6) DEFAULT UTC_TIMESTAMP(6);
    DECLARE holder BOOLEAN;
    DECLARE same_progress BOOLEAN;
    DECLARE file_records BIGINT UNSIGNED;
    DECLARE file_dataset VARCHAR(10);
    DECLARE created BIGINT UNSIGNED;
    DECLARE created_linked BIGINT UNSIGNED;
    DECLARE linked BIGINT UNSIGNED;
    DECLARE quarantined BIGINT UNSIGNED;
    DECLARE quarantined_linked BIGINT UNSIGNED;
    DECLARE quarantined_members BIGINT UNSIGNED;
    DECLARE duplicates BIGINT UNSIGNED;
    DECLARE positions BIGINT UNSIGNED;

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
       OR NEW.identical_duplicates < OLD.identical_duplicates
       OR NEW.quarantined_inserted < OLD.quarantined_inserted
       OR NEW.quarantined_already_recorded < OLD.quarantined_already_recorded THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'batch progress cannot go backwards';
    END IF;

    SET holder = (OLD.lease_token <=> CAST(@crie_gleif_lease AS BINARY))
                 AND OLD.lease_expires_at_utc > now_utc;
    SET same_progress = NEW.records_scanned <=> OLD.records_scanned
        AND NEW.records_selected <=> OLD.records_selected
        AND NEW.observations_inserted <=> OLD.observations_inserted
        AND NEW.observations_already_recorded <=> OLD.observations_already_recorded
        AND NEW.identical_duplicates <=> OLD.identical_duplicates
        AND NEW.quarantined_inserted <=> OLD.quarantined_inserted
        AND NEW.quarantined_already_recorded <=> OLD.quarantined_already_recorded;

    SELECT record_count, dataset INTO file_records, file_dataset
    FROM corporate_intelligence_test.gleif_source_files
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
            FROM corporate_intelligence_test.relationship_observations o
            LEFT JOIN corporate_intelligence_test.relationship_batch_observations m
              ON m.batch_id = o.batch_id AND m.observation_id = o.observation_id
            WHERE o.batch_id = NEW.batch_id;
            SELECT COUNT(*) INTO linked FROM corporate_intelligence_test.relationship_batch_observations
            WHERE batch_id = NEW.batch_id;
        ELSE
            SELECT COUNT(*), COUNT(m.observation_id) INTO created, created_linked
            FROM corporate_intelligence_test.legal_entity_observations o
            LEFT JOIN corporate_intelligence_test.entity_batch_observations m
              ON m.batch_id = o.batch_id AND m.observation_id = o.observation_id
            WHERE o.batch_id = NEW.batch_id;
            SELECT COUNT(*) INTO linked FROM corporate_intelligence_test.entity_batch_observations
            WHERE batch_id = NEW.batch_id;
        END IF;
        IF created <> NEW.observations_inserted OR created_linked <> created
           OR linked <> NEW.observations_inserted + NEW.observations_already_recorded THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'batch counts do not match its recorded observations';
        END IF;
        SELECT COUNT(*), COUNT(m.quarantine_id) INTO quarantined, quarantined_linked
        FROM corporate_intelligence_test.gleif_quarantined_records q
        LEFT JOIN corporate_intelligence_test.gleif_batch_quarantined_records m
          ON m.batch_id = q.batch_id AND m.quarantine_id = q.quarantine_id
        WHERE q.batch_id = NEW.batch_id;
        SELECT COUNT(*) INTO quarantined_members
        FROM corporate_intelligence_test.gleif_batch_quarantined_records WHERE batch_id = NEW.batch_id;
        SELECT COUNT(*) INTO duplicates
        FROM corporate_intelligence_test.gleif_batch_duplicate_occurrences WHERE batch_id = NEW.batch_id;
        IF quarantined <> NEW.quarantined_inserted OR quarantined_linked <> quarantined
           OR quarantined_members <> NEW.quarantined_inserted + NEW.quarantined_already_recorded THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'batch counts do not match its quarantined records';
        END IF;
        IF duplicates <> NEW.identical_duplicates THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'batch counts do not match its duplicate occurrences';
        END IF;
        SELECT COUNT(*) INTO positions
        FROM corporate_intelligence_test.gleif_batch_positions WHERE batch_id = NEW.batch_id;
        IF positions <> NEW.records_selected THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'batch positions do not match its selected records';
        END IF;
    ELSE
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'batch status transition not permitted';
    END IF;
END$$

DELIMITER ;

-- Every rule of these originals is in their replacements above.
DROP TRIGGER corporate_intelligence_test.trg_gleif_batches_start;
DROP TRIGGER corporate_intelligence_test.trg_gleif_batches_lifecycle;

-- chk_gleif_batch_counts_v2 (step 1) holds every rule of the original.
ALTER TABLE corporate_intelligence_test.gleif_import_batches
    DROP CHECK chk_gleif_batch_counts;
