-- E2E DATABASE ONLY (corporate_intelligence_e2e). Never run against corporate_intelligence
-- or corporate_intelligence_test.
-- Milestone 24: investigation ownership and publication.
--
-- 1. investigation_cases gains owner_subject: the user who opened the case
--    ('oidc:<hash>' or 'local:<name>'; 'crie:...' for system-owned cases).
--    Existing rows get '' (a legacy case of the local investigator). The owner
--    can never change. One user cannot open two cases on the same evidence
--    row, but different users can, so the unique key gains the owner. The
--    widened key still begins with the evidence-row columns, so it keeps
--    serving fk_case_evidence.
-- 2. investigation_publications: append-only. Each row publishes one case
--    with one registered report run of that case, chosen by the case's
--    owner. A later publication supersedes, never rewrites, an earlier one.
--    DELETE is allowed only under @crie_publication_maintenance = 1 (this
--    end-to-end database only), so tests can remove the rows they create.
--
-- Changes no existing row other than adding the owner column. Requires the
-- report registry (m23a_e2e_03_registry.sql). Requires an administrative
-- account (ALTER, CREATE, TRIGGER). Run with the mysql client, without --force.
-- Not idempotent: if it fails part-way, inspect before re-running.

ALTER TABLE corporate_intelligence_e2e.investigation_cases
    ADD COLUMN owner_subject VARCHAR(255)
        CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '' AFTER assessment,
    ADD UNIQUE KEY uq_investigation_owner (
        child_lei, parent_lei, relationship_type, snapshot_at_utc, owner_subject),
    DROP INDEX uq_investigation;

CREATE TABLE corporate_intelligence_e2e.investigation_publications (
    publication_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    case_id BIGINT UNSIGNED NOT NULL,
    run_id VARCHAR(128)
        CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    title VARCHAR(200) NOT NULL,
    summary TEXT NOT NULL,
    author_name VARCHAR(200) NOT NULL,
    published_by VARCHAR(255)
        CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    published_at_utc TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    CONSTRAINT fk_publication_case
        FOREIGN KEY (case_id)
        REFERENCES corporate_intelligence_e2e.investigation_cases (case_id),
    CONSTRAINT fk_publication_run
        FOREIGN KEY (run_id)
        REFERENCES corporate_intelligence_e2e.report_runs (run_id),
    CONSTRAINT chk_publication_text CHECK (
        CHAR_LENGTH(TRIM(title)) > 0
        AND CHAR_LENGTH(TRIM(summary)) > 0
        AND CHAR_LENGTH(TRIM(author_name)) > 0),

    INDEX idx_publication_case (case_id, publication_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

DELIMITER $$

CREATE TRIGGER corporate_intelligence_e2e.trg_investigation_cases_owner_fixed
BEFORE UPDATE ON corporate_intelligence_e2e.investigation_cases FOR EACH ROW
BEGIN
    IF NOT (NEW.owner_subject <=> OLD.owner_subject) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'An investigation''s owner cannot be changed';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_publications_insert
BEFORE INSERT ON corporate_intelligence_e2e.investigation_publications FOR EACH ROW
BEGIN
    DECLARE run_case BIGINT UNSIGNED DEFAULT NULL;
    DECLARE case_owner VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL;

    SELECT case_id INTO run_case
    FROM corporate_intelligence_e2e.report_runs WHERE run_id = NEW.run_id;
    SELECT owner_subject INTO case_owner
    FROM corporate_intelligence_e2e.investigation_cases WHERE case_id = NEW.case_id;

    IF run_case IS NULL OR run_case <> NEW.case_id THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'The report run does not belong to this investigation';
    END IF;
    IF case_owner IS NULL OR case_owner = '' OR case_owner <> NEW.published_by THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Only the investigation''s owner can publish it';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_publications_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.investigation_publications FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'investigation_publications is append-only'$$

CREATE TRIGGER corporate_intelligence_e2e.trg_publications_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.investigation_publications FOR EACH ROW
BEGIN
    IF COALESCE(@crie_publication_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'investigation_publications is append-only';
    END IF;
END$$

DELIMITER ;

-- Check (read-only), after installation:
--   SHOW CREATE TABLE corporate_intelligence_e2e.investigation_cases;
--   SHOW CREATE TABLE corporate_intelligence_e2e.investigation_publications;
--   SHOW TRIGGERS FROM corporate_intelligence_e2e LIKE 'investigation%';
