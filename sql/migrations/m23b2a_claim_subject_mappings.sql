-- PRODUCTION MIGRATION. Not executed; requires separate authorization
-- (backup first, administrator, mysql client without --force).
--
-- Milestone 23B.2A: case-scoped claim-subject identity mappings.
--
-- An append-only log in which an investigator records what legal
-- entity the exact subject text of a case's disclosure claims refers
-- to: MAPPED to one LEI, AMBIGUOUS between named candidates, NO_LEI
-- (the entity has none; never invented), or UNRESOLVED. Nothing is
-- assigned automatically and the claims themselves are not changed.
--
-- Depends on m23b1_source_relationship_assessments.sql (the unique key
-- case_sources (source_id, case_id) used by the evidence foreign key).
--
-- Integrity:
--   * subject_text is stored in utf8mb4_0900_bin, so every comparison
--     of it (unique keys, trigger, queries) is exact: case, trailing
--     spaces and Unicode normalization all distinguish subjects.
--   * One chain per case and subject: at most one root
--     (uq_subject_mapping_root) and one successor per mapping
--     (uq_subject_mapping_successor); the trigger allows superseding
--     only a mapping of the same case and subject.
--   * MAPPED needs an LEI that exists in legal_entities, a name type,
--     traceable evidence, a locator and stated limitations. AMBIGUOUS
--     needs at least two candidate LEIs and no chosen LEI. NO_LEI
--     needs the evidence of the search. The evidence source belongs to
--     the same case (composite foreign key).
--   * Append-only: UPDATE and DELETE are always rejected.
--
-- Requires an administrative account (CREATE, REFERENCES, INDEX,
-- TRIGGER; binary logging is enabled). Additive only.

CREATE TABLE corporate_intelligence.claim_subject_mappings (
    mapping_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    case_id BIGINT UNSIGNED NOT NULL,
    subject_text VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,

    status ENUM('MAPPED', 'AMBIGUOUS', 'NO_LEI', 'UNRESOLVED') NOT NULL,
    lei CHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL,
    name_type ENUM('LEGAL_NAME', 'TRADING_NAME', 'PREVIOUS_NAME', 'ALIAS',
                   'ABBREVIATION', 'OTHER') NULL,
    candidate_leis JSON NULL,

    basis ENUM('DOCUMENTED', 'INVESTIGATOR_JUDGEMENT') NOT NULL,
    evidence_source_id BIGINT UNSIGNED NULL,
    evidence_reference VARCHAR(2048) NULL,
    evidence_location VARCHAR(500) NULL,
    rationale TEXT NOT NULL,
    limitations TEXT NULL,

    recorded_by VARCHAR(255) NOT NULL,
    recorded_at_utc TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    supersedes_mapping_id BIGINT UNSIGNED NULL,
    submission_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,

    -- The subject on a chain's first row only (NULL elsewhere).
    root_subject_text VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin
        GENERATED ALWAYS AS (IF(supersedes_mapping_id IS NULL, subject_text, NULL)) STORED,

    UNIQUE KEY uq_subject_mapping_submission (submission_key),
    UNIQUE KEY uq_subject_mapping_successor (supersedes_mapping_id),
    UNIQUE KEY uq_subject_mapping_root (case_id, root_subject_text),
    INDEX idx_subject_mapping_subject (case_id, subject_text),
    INDEX idx_subject_mapping_lei (lei),

    CONSTRAINT fk_subject_mapping_case
        FOREIGN KEY (case_id)
        REFERENCES corporate_intelligence.investigation_cases (case_id),
    CONSTRAINT fk_subject_mapping_lei
        FOREIGN KEY (lei)
        REFERENCES corporate_intelligence.legal_entities (lei),
    CONSTRAINT fk_subject_mapping_evidence_source
        FOREIGN KEY (evidence_source_id, case_id)
        REFERENCES corporate_intelligence.case_sources (source_id, case_id),
    CONSTRAINT fk_subject_mapping_supersedes
        FOREIGN KEY (supersedes_mapping_id)
        REFERENCES corporate_intelligence.claim_subject_mappings (mapping_id),

    CONSTRAINT chk_subject_mapping_subject
        CHECK (CHAR_LENGTH(TRIM(subject_text)) > 0),
    CONSTRAINT chk_subject_mapping_rationale
        CHECK (CHAR_LENGTH(TRIM(rationale)) > 0),
    CONSTRAINT chk_subject_mapping_recorded_by
        CHECK (CHAR_LENGTH(TRIM(recorded_by)) > 0),
    CONSTRAINT chk_subject_mapping_status_shape
        CHECK ((status = 'MAPPED' AND lei IS NOT NULL AND name_type IS NOT NULL
                AND candidate_leis IS NULL)
            OR (status = 'AMBIGUOUS' AND lei IS NULL AND candidate_leis IS NOT NULL
                AND JSON_LENGTH(candidate_leis) >= 2)
            OR (status IN ('NO_LEI', 'UNRESOLVED') AND lei IS NULL
                AND candidate_leis IS NULL)),
    CONSTRAINT chk_subject_mapping_evidence
        CHECK (status NOT IN ('MAPPED', 'NO_LEI') OR (
            (evidence_source_id IS NOT NULL
             OR CHAR_LENGTH(TRIM(COALESCE(evidence_reference, ''))) > 0)
            AND CHAR_LENGTH(TRIM(COALESCE(evidence_location, ''))) > 0)),
    CONSTRAINT chk_subject_mapping_limitations
        CHECK (status <> 'MAPPED'
               OR CHAR_LENGTH(TRIM(COALESCE(limitations, ''))) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

DELIMITER $$

CREATE TRIGGER corporate_intelligence.trg_subject_mappings_supersession
BEFORE INSERT ON corporate_intelligence.claim_subject_mappings FOR EACH ROW
BEGIN
    IF NEW.supersedes_mapping_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM corporate_intelligence.claim_subject_mappings p
        WHERE p.mapping_id = NEW.supersedes_mapping_id
          AND p.case_id = NEW.case_id
          AND (p.subject_text COLLATE utf8mb4_0900_bin) = (NEW.subject_text COLLATE utf8mb4_0900_bin)
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a mapping may only supersede one of the same case and subject';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence.trg_subject_mappings_no_update
BEFORE UPDATE ON corporate_intelligence.claim_subject_mappings FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'claim_subject_mappings is append-only'$$

CREATE TRIGGER corporate_intelligence.trg_subject_mappings_no_delete
BEFORE DELETE ON corporate_intelligence.claim_subject_mappings FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'claim_subject_mappings is append-only'$$

DELIMITER ;
