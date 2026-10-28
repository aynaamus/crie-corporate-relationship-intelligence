
-- PRODUCTION MIGRATION — PREPARED FOR REVIEW, NOT YET AUTHORIZED.
-- Milestone 22.3B: append-only registry of versioned report runs.
--
-- Additive only: creates three new tables and their triggers.
-- No existing table, row, constraint, or grant is changed.
-- Every name is qualified with corporate_intelligence.
--
-- The registry indexes report runs. The original report.md and
-- manifest.json files remain the record and must be retained.
--
-- Immutability: UPDATE and DELETE are rejected by triggers.
-- Removing a registration requires dropping a trigger, a
-- deliberate privileged DDL action outside application workflows.
--
-- Requires an account with CREATE, REFERENCES, INDEX and TRIGGER
-- privileges (binary logging is enabled, so trigger creation
-- also needs SUPER or log_bin_trust_function_creators).
-- Not idempotent for triggers: re-running fails at the first
-- CREATE TRIGGER without changing anything further.
--
-- Run with the mysql client (uses DELIMITER), without --force.

CREATE TABLE IF NOT EXISTS corporate_intelligence.report_runs (
    run_id VARCHAR(128)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    case_id BIGINT UNSIGNED NOT NULL,
    previous_year SMALLINT UNSIGNED NOT NULL,
    current_year SMALLINT UNSIGNED NOT NULL,

    manifest_schema_version VARCHAR(16)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    snapshot_schema_version VARCHAR(16)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    generated_at_utc DATETIME(6) NOT NULL,
    entry_point VARCHAR(128) NOT NULL,
    git_commit CHAR(40)
        CHARACTER SET ascii COLLATE ascii_bin NULL,
    working_tree_dirty TINYINT(1) NULL,

    evidence_snapshot_sha256 CHAR(64)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    analysis_sha256 CHAR(64)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    report_sha256 CHAR(64)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    manifest_sha256 CHAR(64)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    report_bytes INT UNSIGNED NOT NULL,

    -- Relative to the configured runs root; never absolute.
    run_path VARCHAR(160)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,

    claim_count INT UNSIGNED NOT NULL,
    source_count INT UNSIGNED NOT NULL,

    registered_at_utc TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    PRIMARY KEY (run_id),
    UNIQUE KEY uq_report_run_path (run_path),
    INDEX idx_report_runs_case (case_id, generated_at_utc),
    INDEX idx_report_runs_evidence (evidence_snapshot_sha256),

    CONSTRAINT fk_report_run_case
        FOREIGN KEY (case_id)
        REFERENCES corporate_intelligence.investigation_cases(case_id),

    CONSTRAINT chk_report_run_years
        CHECK (previous_year < current_year),
    CONSTRAINT chk_report_run_path
        CHECK (run_path = CONCAT('case_', case_id, '/', run_id)),
    CONSTRAINT chk_report_run_hashes CHECK (
        CHAR_LENGTH(evidence_snapshot_sha256) = 64
        AND CHAR_LENGTH(analysis_sha256) = 64
        AND CHAR_LENGTH(report_sha256) = 64
        AND CHAR_LENGTH(manifest_sha256) = 64
    )
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS corporate_intelligence.report_run_claims (
    run_id VARCHAR(128)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    claim_id BIGINT UNSIGNED NOT NULL,
    claim_fingerprint CHAR(64)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_id BIGINT UNSIGNED NOT NULL,

    PRIMARY KEY (run_id, claim_id),
    INDEX idx_report_run_claims_claim (claim_id),
    INDEX idx_report_run_claims_source (source_id),

    CONSTRAINT fk_report_run_claims_run
        FOREIGN KEY (run_id)
        REFERENCES corporate_intelligence.report_runs(run_id),
    CONSTRAINT fk_report_run_claims_claim
        FOREIGN KEY (claim_id)
        REFERENCES corporate_intelligence.disclosure_claims(claim_id),
    CONSTRAINT fk_report_run_claims_source
        FOREIGN KEY (source_id)
        REFERENCES corporate_intelligence.case_sources(source_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS corporate_intelligence.report_run_sources (
    run_id VARCHAR(128)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_id BIGINT UNSIGNED NOT NULL,
    evidence_assessment ENUM(
        'NOT_REVIEWED',
        'SUPPORTS',
        'CONTRADICTS',
        'INCONCLUSIVE'
    ) NOT NULL,
    assessment_history_id BIGINT UNSIGNED NULL,

    PRIMARY KEY (run_id, source_id),
    INDEX idx_report_run_sources_source (source_id),

    CONSTRAINT fk_report_run_sources_run
        FOREIGN KEY (run_id)
        REFERENCES corporate_intelligence.report_runs(run_id),
    CONSTRAINT fk_report_run_sources_source
        FOREIGN KEY (source_id)
        REFERENCES corporate_intelligence.case_sources(source_id),
    CONSTRAINT fk_report_run_sources_history
        FOREIGN KEY (assessment_history_id)
        REFERENCES corporate_intelligence.source_assessment_history(history_id)
) ENGINE=InnoDB;

DELIMITER $$

CREATE TRIGGER corporate_intelligence.trg_report_runs_no_update
BEFORE UPDATE ON corporate_intelligence.report_runs FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'report_runs is append-only'$$

CREATE TRIGGER corporate_intelligence.trg_report_runs_no_delete
BEFORE DELETE ON corporate_intelligence.report_runs FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'report_runs is append-only'$$

CREATE TRIGGER corporate_intelligence.trg_report_run_claims_no_update
BEFORE UPDATE ON corporate_intelligence.report_run_claims FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'report_run_claims is append-only'$$

CREATE TRIGGER corporate_intelligence.trg_report_run_claims_no_delete
BEFORE DELETE ON corporate_intelligence.report_run_claims FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'report_run_claims is append-only'$$

CREATE TRIGGER corporate_intelligence.trg_report_run_sources_no_update
BEFORE UPDATE ON corporate_intelligence.report_run_sources FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'report_run_sources is append-only'$$

CREATE TRIGGER corporate_intelligence.trg_report_run_sources_no_delete
BEFORE DELETE ON corporate_intelligence.report_run_sources FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'report_run_sources is append-only'$$

DELIMITER ;
