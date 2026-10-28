-- E2E DATABASE ONLY (corporate_intelligence_e2e). Never run against corporate_intelligence.
-- Milestone 23B.1: source-relationship assessments.
--
-- Generated from sql/migrations/m23b1_source_relationship_assessments.sql;
-- the statements must stay identical (checked by
-- tests/test_source_assessment_migrations.py). Only names (qualified with
-- corporate_intelligence_e2e) and the DELETE trigger differ: here DELETE is
-- allowed only when the session explicitly sets
-- @crie_assessment_maintenance = 1, so tests can remove the synthetic
-- rows they created. UPDATE is always rejected.
--
-- Requires an administrative account. Run with the mysql client, without --force.

ALTER TABLE corporate_intelligence_e2e.case_sources
    ADD UNIQUE KEY uq_case_source (source_id, case_id);

CREATE TABLE corporate_intelligence_e2e.source_relationship_assessments (
    assessment_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    case_id BIGINT UNSIGNED NOT NULL,
    source_a_id BIGINT UNSIGNED NOT NULL,
    source_b_id BIGINT UNSIGNED NOT NULL,

    -- Disclosure scope: all NULL (every claim of the pair) or all set.
    entity_name VARCHAR(255) NULL,
    metric VARCHAR(128) NULL,
    report_year SMALLINT UNSIGNED NULL,
    period_end DATE NULL,

    relationship ENUM(
        'SAME_DOCUMENT', 'REPUBLICATION', 'SHARED_PUBLISHER',
        'SAME_CORPORATE_GROUP', 'QUOTES_OR_ATTRIBUTES',
        'SAME_PRIMARY_DISCLOSURE', 'INDEPENDENT_ORIGIN', 'UNKNOWN'
    ) NOT NULL,
    basis ENUM('DOCUMENTED', 'INVESTIGATOR_JUDGEMENT') NOT NULL,

    evidence_source_id BIGINT UNSIGNED NULL,
    evidence_reference VARCHAR(2048) NULL,
    evidence_location VARCHAR(500) NULL,
    rationale TEXT NOT NULL,
    limitations TEXT NULL,

    recorded_by VARCHAR(255) NOT NULL,
    recorded_at_utc TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    supersedes_assessment_id BIGINT UNSIGNED NULL,
    submission_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,

    -- Identity of the scope; equal for every row of one chain.
    scope_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (SHA2(IF(
            metric IS NULL, 'PAIR',
            CONCAT_WS(CHAR(31), 'DISCLOSURE', entity_name, metric,
                      report_year, period_end)), 256)) STORED,
    -- The scope key on a chain's first row only (NULL elsewhere).
    root_scope_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (IF(supersedes_assessment_id IS NULL,
                                scope_key, NULL)) STORED,

    UNIQUE KEY uq_assessment_submission (submission_key),
    UNIQUE KEY uq_assessment_successor (supersedes_assessment_id),
    UNIQUE KEY uq_assessment_root (case_id, source_a_id, source_b_id, root_scope_key),
    INDEX idx_assessment_scope (case_id, source_a_id, source_b_id, scope_key),

    CONSTRAINT fk_assessment_case
        FOREIGN KEY (case_id)
        REFERENCES corporate_intelligence_e2e.investigation_cases (case_id),
    CONSTRAINT fk_assessment_source_a
        FOREIGN KEY (source_a_id, case_id)
        REFERENCES corporate_intelligence_e2e.case_sources (source_id, case_id),
    CONSTRAINT fk_assessment_source_b
        FOREIGN KEY (source_b_id, case_id)
        REFERENCES corporate_intelligence_e2e.case_sources (source_id, case_id),
    CONSTRAINT fk_assessment_evidence_source
        FOREIGN KEY (evidence_source_id, case_id)
        REFERENCES corporate_intelligence_e2e.case_sources (source_id, case_id),
    CONSTRAINT fk_assessment_supersedes
        FOREIGN KEY (supersedes_assessment_id)
        REFERENCES corporate_intelligence_e2e.source_relationship_assessments (assessment_id),

    CONSTRAINT chk_assessment_pair_order
        CHECK (source_a_id < source_b_id),
    CONSTRAINT chk_assessment_scope_complete
        CHECK ((entity_name IS NULL AND metric IS NULL
                AND report_year IS NULL AND period_end IS NULL)
            OR (entity_name IS NOT NULL AND metric IS NOT NULL
                AND report_year IS NOT NULL AND period_end IS NOT NULL)),
    CONSTRAINT chk_assessment_rationale
        CHECK (CHAR_LENGTH(TRIM(rationale)) > 0),
    CONSTRAINT chk_assessment_recorded_by
        CHECK (CHAR_LENGTH(TRIM(recorded_by)) > 0),
    CONSTRAINT chk_assessment_independence_evidence
        CHECK (relationship <> 'INDEPENDENT_ORIGIN' OR (
            (evidence_source_id IS NOT NULL
             OR CHAR_LENGTH(TRIM(COALESCE(evidence_reference, ''))) > 0)
            AND CHAR_LENGTH(TRIM(COALESCE(evidence_location, ''))) > 0
            AND CHAR_LENGTH(TRIM(COALESCE(limitations, ''))) > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

DELIMITER $$

CREATE TRIGGER corporate_intelligence_e2e.trg_source_assessments_supersession
BEFORE INSERT ON corporate_intelligence_e2e.source_relationship_assessments FOR EACH ROW
BEGIN
    IF NEW.supersedes_assessment_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM corporate_intelligence_e2e.source_relationship_assessments p
        WHERE p.assessment_id = NEW.supersedes_assessment_id
          AND p.case_id = NEW.case_id
          AND p.source_a_id = NEW.source_a_id
          AND p.source_b_id = NEW.source_b_id
          AND (p.entity_name COLLATE utf8mb4_0900_bin) <=> (NEW.entity_name COLLATE utf8mb4_0900_bin)
          AND (p.metric COLLATE utf8mb4_0900_bin) <=> (NEW.metric COLLATE utf8mb4_0900_bin)
          AND p.report_year <=> NEW.report_year
          AND p.period_end <=> NEW.period_end
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'an assessment may only supersede one of the same case, pair and scope';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_source_assessments_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.source_relationship_assessments FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'source_relationship_assessments is append-only'$$

CREATE TRIGGER corporate_intelligence_e2e.trg_source_assessments_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.source_relationship_assessments FOR EACH ROW
BEGIN
    IF COALESCE(@crie_assessment_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'source_relationship_assessments is append-only';
    END IF;
END$$

DELIMITER ;
