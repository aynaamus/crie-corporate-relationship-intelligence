-- E2E DATABASE ONLY (corporate_intelligence_e2e). Never run against corporate_intelligence
-- or corporate_intelligence_test. Milestone 23A, step 4 of 5.
--
-- The claim-correction lineage (Milestone 22.4A), identical to the test-database twin
-- sql/test_schema_upgrade_m22_4a.sql with only the database name changed. As there,
-- deletes are allowed only under the session's maintenance flag, so the
-- end-to-end test can remove the rows it created; UPDATE is always rejected.
-- Requires an administrative account. Run with the mysql client.


ALTER TABLE corporate_intelligence_e2e.claim_corrections
    ADD COLUMN correction_kind ENUM('CORRECTION', 'RESTORATION')
        NOT NULL DEFAULT 'CORRECTION' AFTER replacement_claim_id,
    ADD COLUMN restored_claim_id BIGINT UNSIGNED NULL AFTER correction_kind,
    ADD COLUMN evidence_source_id BIGINT UNSIGNED NULL AFTER correction_reason,
    ADD COLUMN evidence_location VARCHAR(500) NULL AFTER evidence_source_id,
    ADD COLUMN submission_key CHAR(64)
        CHARACTER SET ascii COLLATE ascii_bin NULL AFTER evidence_location,

    ADD UNIQUE KEY uq_correction_submission (submission_key),

    ADD CONSTRAINT fk_correction_restored
        FOREIGN KEY (restored_claim_id)
        REFERENCES corporate_intelligence_e2e.disclosure_claims (claim_id),
    ADD CONSTRAINT fk_correction_evidence_source
        FOREIGN KEY (evidence_source_id)
        REFERENCES corporate_intelligence_e2e.case_sources (source_id),

    ADD CONSTRAINT chk_correction_forward
        CHECK (replacement_claim_id > original_claim_id),
    ADD CONSTRAINT chk_restoration_target
        CHECK ((correction_kind = 'RESTORATION') = (restored_claim_id IS NOT NULL)),
    ADD CONSTRAINT chk_restoration_backward
        CHECK (restored_claim_id IS NULL OR restored_claim_id < original_claim_id),
    ADD CONSTRAINT chk_evidence_complete
        CHECK ((evidence_source_id IS NULL) = (evidence_location IS NULL));

DELIMITER $$

CREATE TRIGGER corporate_intelligence_e2e.trg_claim_corrections_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.claim_corrections FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'claim_corrections is append-only'$$

CREATE TRIGGER corporate_intelligence_e2e.trg_claim_corrections_no_delete
BEFORE DELETE ON corporate_intelligence_e2e.claim_corrections FOR EACH ROW
BEGIN
    IF COALESCE(@crie_claim_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'claim_corrections is append-only';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_e2e.trg_disclosure_claims_no_update
BEFORE UPDATE ON corporate_intelligence_e2e.disclosure_claims FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'disclosure_claims are superseded, never updated'$$

DELIMITER ;
