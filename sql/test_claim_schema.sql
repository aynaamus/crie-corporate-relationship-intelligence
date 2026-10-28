
CREATE DATABASE IF NOT EXISTS corporate_intelligence_test;

USE corporate_intelligence_test;

CREATE TABLE IF NOT EXISTS investigation_cases (
    case_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    case_name VARCHAR(255) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS case_sources (
    source_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    case_id BIGINT UNSIGNED NOT NULL,
    document_title VARCHAR(500) NOT NULL,
    publisher VARCHAR(255) NULL,
    -- Nullable here (NOT NULL in production) for minimal fixtures.
    source_url TEXT NULL,
    publication_date DATE NULL,
    evidence_assessment ENUM(
        'NOT_REVIEWED', 'SUPPORTS', 'CONTRADICTS', 'INCONCLUSIVE'
    ) NOT NULL DEFAULT 'NOT_REVIEWED',
    analyst_notes TEXT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (case_id)
        REFERENCES investigation_cases(case_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS disclosure_claims (
    claim_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_id BIGINT UNSIGNED NOT NULL,
    entity_name VARCHAR(255) NOT NULL,
    metric VARCHAR(128) NOT NULL,
    report_year SMALLINT UNSIGNED NOT NULL,
    period_end DATE NOT NULL,
    value_text VARCHAR(500) NOT NULL,
    normalized_value VARCHAR(500) NOT NULL,
    source_location VARCHAR(500) NOT NULL,
    claim_fingerprint CHAR(64)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    created_at TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    UNIQUE KEY uq_claim_fingerprint (claim_fingerprint),

    FOREIGN KEY (source_id)
        REFERENCES case_sources(source_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS claim_corrections (
    correction_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    original_claim_id BIGINT UNSIGNED NOT NULL,
    replacement_claim_id BIGINT UNSIGNED NOT NULL,
    correction_reason TEXT NOT NULL,
    corrected_at_utc TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    UNIQUE KEY uq_original_claim (original_claim_id),
    UNIQUE KEY uq_replacement_claim (replacement_claim_id),

    FOREIGN KEY (original_claim_id)
        REFERENCES disclosure_claims(claim_id),

    FOREIGN KEY (replacement_claim_id)
        REFERENCES disclosure_claims(claim_id),

    CHECK (original_claim_id <> replacement_claim_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS source_assessment_history (
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

    FOREIGN KEY (source_id) REFERENCES case_sources(source_id),
    INDEX idx_source_history (source_id, history_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS source_history_annotations (
    annotation_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    history_id BIGINT UNSIGNED NOT NULL,
    annotation_type ENUM('ACCIDENTAL_DUPLICATE') NOT NULL,
    annotation_note TEXT NOT NULL,
    created_at_utc TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    UNIQUE KEY uq_history_annotation (history_id, annotation_type),
    FOREIGN KEY (history_id)
        REFERENCES source_assessment_history(history_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS source_metadata_history (
    change_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_id BIGINT UNSIGNED NOT NULL,
    previous_metadata JSON NOT NULL,
    updated_metadata JSON NOT NULL,
    change_reason TEXT NOT NULL,
    changed_at_utc TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    FOREIGN KEY (source_id) REFERENCES case_sources(source_id)
) ENGINE=InnoDB;

CREATE OR REPLACE VIEW v_current_disclosure_claims AS
SELECT c.*
FROM disclosure_claims c
WHERE NOT EXISTS (
    SELECT 1
    FROM claim_corrections cc
    WHERE cc.original_claim_id = c.claim_id
);
