-- TEST DATABASE ONLY. Never run against corporate_intelligence.
-- Milestone 22.4A: claim-correction lineage for integration tests.
--
-- Generated from sql/migrations/m22_4a_claim_correction_lineage.sql;
-- the ALTER TABLE must stay identical (checked by
-- tests/test_claim_lineage.py). Only names (qualified with
-- corporate_intelligence_test) and the claim_corrections DELETE
-- trigger differ: here DELETE is allowed only when the session
-- explicitly sets @crie_claim_maintenance = 1, so integration tests
-- can remove the synthetic rows they created. UPDATE is always
-- rejected.
--
-- Requires an administrative account (ALTER, REFERENCES, INDEX,
-- TRIGGER; binary logging is enabled). Additive only. Not
-- idempotent: a re-run fails at the ALTER without changing anything.
--
-- Run with the mysql client (uses DELIMITER), without --force.

ALTER TABLE corporate_intelligence_test.claim_corrections
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
        REFERENCES corporate_intelligence_test.disclosure_claims (claim_id),
    ADD CONSTRAINT fk_correction_evidence_source
        FOREIGN KEY (evidence_source_id)
        REFERENCES corporate_intelligence_test.case_sources (source_id),

    ADD CONSTRAINT chk_correction_forward
        CHECK (replacement_claim_id > original_claim_id),
    ADD CONSTRAINT chk_restoration_target
        CHECK ((correction_kind = 'RESTORATION') = (restored_claim_id IS NOT NULL)),
    ADD CONSTRAINT chk_restoration_backward
        CHECK (restored_claim_id IS NULL OR restored_claim_id < original_claim_id),
    ADD CONSTRAINT chk_evidence_complete
        CHECK ((evidence_source_id IS NULL) = (evidence_location IS NULL));

DELIMITER $$

CREATE TRIGGER corporate_intelligence_test.trg_claim_corrections_no_update
BEFORE UPDATE ON corporate_intelligence_test.claim_corrections FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'claim_corrections is append-only'$$

CREATE TRIGGER corporate_intelligence_test.trg_claim_corrections_no_delete
BEFORE DELETE ON corporate_intelligence_test.claim_corrections FOR EACH ROW
BEGIN
    IF COALESCE(@crie_claim_maintenance, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'claim_corrections is append-only';
    END IF;
END$$

CREATE TRIGGER corporate_intelligence_test.trg_disclosure_claims_no_update
BEFORE UPDATE ON corporate_intelligence_test.disclosure_claims FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'disclosure_claims are superseded, never updated'$$

DELIMITER ;
