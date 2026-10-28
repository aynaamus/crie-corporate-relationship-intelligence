-- E2E DATABASE ONLY (corporate_intelligence_e2e). Never run against corporate_intelligence
-- or corporate_intelligence_test. Milestone 23A, step 3 of 5.
--
-- The report registry (Milestone 22.3B), identical to the test-database twin
-- sql/test_report_registry_m22_3b.sql with only the database name changed. As there,
-- deletes are allowed only under the session's maintenance flag, so the
-- end-to-end test can remove the rows it created; UPDATE is always rejected.
-- Requires an administrative account. Run with the mysql client.


-- TEST DATABASE ONLY. Never run against corporate_intelligence.
-- Milestone 22.3B.1: report registry for integration tests.
--
-- Generated from sql/migrations/m22_3b_report_registry.sql; the
-- table definitions must stay identical (checked by
-- tests/test_registry_migrations.py). Only names (qualified with
-- corporate_intelligence_e2e) and the DELETE triggers differ:
-- here DELETE is allowed only when the session explicitly sets
-- @crie_registry_maintenance = 1, so integration tests can remove
-- the synthetic rows they created. UPDATE is always rejected.
--
-- Requires an administrative account (CREATE, REFERENCES, INDEX,
-- TRIGGER; binary logging is enabled). Additive only. Not
-- idempotent for triggers: a re-run fails at the first CREATE
-- TRIGGER without changing anything further.
--
-- Run with the mysql client (uses DELIMITER), without --force.

CREATE TABLE IF NOT EXISTS corporate_intelligence_e2e.report_runs (
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
        REFERENCES corporate_intelligence_e2e.investigation_cases(case_id),

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

CREATE TABLE IF NOT EXISTS corporate_intelligence_e2e.report_run_claims (
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
        REFERENCES corporate_intelligence_e2e.report_runs(run_id),
    CONSTRAINT fk_report_run_claims_claim
        FOREIGN KEY (claim_id)
        REFERENCES corporate_intelligence_e2e.disclosure_claims(claim_id),
    CONSTRAINT fk_report_run_claims_source
        FOREIGN KEY (source_id)
        REFERENCES corporate_intelligence_e2e.case_sources(source_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS corporate_intelligence_e2e.report_run_sources (
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
        REFERENCES corporate_intelligence_e2e.report_runs(run_id),
    CONSTRAINT fk_report_run_sources_source
        FOREIGN KEY (source_id)
        REFERENCES corporate_intelligence_e2e.case_sources(source_id),
    CONSTRAINT fk_report_run_sources_history
        FOREIGN KEY (assessment_history_id)
        REFERENCES corporate_intelligence_e2e.source_assessment_history(history_id)
) ENGINE=InnoDB;

DELIMITER $$

CREATE TRIGGER corporate_intelligence_e2e.trg_report_runs_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.report_runs FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'report_runs is append-only'$$

CREATE TRIGGER corporate_intelligence_e2e.trg_report_runs_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.report_runs FOR EACH ROW
BEGIN
    IF COALESCE(@crie_registry_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'report_runs is append-only';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_report_run_claims_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.report_run_claims FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'report_run_claims is append-only'$$

CREATE TRIGGER corporate_intelligence_e2e.trg_report_run_claims_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.report_run_claims FOR EACH ROW
BEGIN
    IF COALESCE(@crie_registry_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'report_run_claims is append-only';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_report_run_sources_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.report_run_sources FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'report_run_sources is append-only'$$

CREATE TRIGGER corporate_intelligence_e2e.trg_report_run_sources_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.report_run_sources FOR EACH ROW
BEGIN
    IF COALESCE(@crie_registry_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'report_run_sources is append-only';
    END IF;
END$$

DELIMITER ;
