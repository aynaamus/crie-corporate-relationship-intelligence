
-- TEST DATABASE ONLY. Never run against corporate_intelligence.
--
-- One-time upgrade of an existing corporate_intelligence_test
-- schema (created by test_claim_schema.sql before M22.3A) so
-- evidence snapshots can be integration-tested. Adds the
-- production columns and history tables the snapshot reads.
--
-- Every object name is qualified with corporate_intelligence_test,
-- so no statement can resolve to another database.
--
-- Not idempotent: MySQL has no ADD COLUMN IF NOT EXISTS. On a
-- re-run the first ALTER fails with "Duplicate column name" and
-- the mysql client stops before any other statement runs.
--
-- Deviation from production: case_sources.source_url is
-- nullable so older synthetic tests that omit it still run.

USE corporate_intelligence_test;

ALTER TABLE corporate_intelligence_test.case_sources
    ADD COLUMN publisher VARCHAR(255) NULL,
    ADD COLUMN source_url TEXT NULL,
    ADD COLUMN publication_date DATE NULL,
    ADD COLUMN evidence_assessment ENUM(
        'NOT_REVIEWED',
        'SUPPORTS',
        'CONTRADICTS',
        'INCONCLUSIVE'
    ) NOT NULL DEFAULT 'NOT_REVIEWED',
    ADD COLUMN analyst_notes TEXT NULL,
    ADD COLUMN added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE corporate_intelligence_test.disclosure_claims
    ADD COLUMN created_at TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6);

ALTER TABLE corporate_intelligence_test.claim_corrections
    ADD COLUMN corrected_at_utc TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6);

CREATE TABLE IF NOT EXISTS
    corporate_intelligence_test.source_assessment_history (
    history_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_id BIGINT UNSIGNED NOT NULL,
    event_type ENUM('BASELINE', 'CREATED', 'REVIEW') NOT NULL,
    previous_assessment ENUM(
        'NOT_REVIEWED', 'SUPPORTS', 'CONTRADICTS', 'INCONCLUSIVE'
    ) NULL,
    new_assessment ENUM(
        'NOT_REVIEWED', 'SUPPORTS', 'CONTRADICTS', 'INCONCLUSIVE'
    ) NOT NULL,
    review_reason TEXT,
    recorded_at_utc TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    FOREIGN KEY (source_id)
        REFERENCES corporate_intelligence_test.case_sources(source_id),
    INDEX idx_source_history (source_id, history_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS
    corporate_intelligence_test.source_history_annotations (
    annotation_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    history_id BIGINT UNSIGNED NOT NULL,
    annotation_type ENUM('ACCIDENTAL_DUPLICATE') NOT NULL,
    annotation_note TEXT NOT NULL,
    created_at_utc TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    UNIQUE KEY uq_history_annotation (history_id, annotation_type),
    FOREIGN KEY (history_id)
        REFERENCES corporate_intelligence_test.source_assessment_history(history_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS
    corporate_intelligence_test.source_metadata_history (
    change_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_id BIGINT UNSIGNED NOT NULL,
    previous_metadata JSON NOT NULL,
    updated_metadata JSON NOT NULL,
    change_reason TEXT NOT NULL,
    changed_at_utc TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    FOREIGN KEY (source_id)
        REFERENCES corporate_intelligence_test.case_sources(source_id)
) ENGINE=InnoDB;

-- Views store their column list at creation; recreate so
-- the new disclosure_claims.created_at column is included.
CREATE OR REPLACE VIEW
    corporate_intelligence_test.v_current_disclosure_claims AS
SELECT c.*
FROM corporate_intelligence_test.disclosure_claims c
WHERE NOT EXISTS (
    SELECT 1
    FROM corporate_intelligence_test.claim_corrections cc
    WHERE cc.original_claim_id = c.claim_id
);
