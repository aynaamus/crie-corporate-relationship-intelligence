-- PRODUCTION MIGRATION. Not executed; requires separate authorization
-- (docs/REGISTRY_ACTIVATION.md process: backup first, administrator,
-- mysql client without --force).
--
-- Milestone 22.4A: auditable claim-correction lineage.
--
-- Adds to claim_corrections:
--   correction_kind     CORRECTION, or RESTORATION of an earlier value
--   restored_claim_id   the earlier claim in the same lineage whose
--                       value and location a RESTORATION reinstates
--   evidence_source_id  the case source that supports the transition
--   evidence_location   where in that source the evidence is found
--   submission_key      SHA-256 of the submitted correction; a repeated
--                       identical submission is recognised, not duplicated
-- and constraints that make lineage acyclic by construction: a
-- replacement is always a newer claim than the claim it supersedes,
-- and a restored claim is always older.
--
-- Existing rows keep their meaning: correction_kind defaults to
-- CORRECTION and the new columns are NULL (no recorded evidence
-- attribution). No row is updated or deleted.
--
-- Append-only enforcement: claim_corrections rejects UPDATE and DELETE,
-- and disclosure_claims rejects UPDATE (claims are superseded, never
-- rewritten). Creating triggers requires an administrative account
-- because binary logging is enabled.
--
-- Not idempotent: a re-run fails at the first ALTER ("Duplicate column
-- name") before changing anything else. If it stops at a later
-- statement, see docs/REGISTRY_ACTIVATION.md §5 for partial recovery.

ALTER TABLE corporate_intelligence.claim_corrections
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
        REFERENCES corporate_intelligence.disclosure_claims (claim_id),
    ADD CONSTRAINT fk_correction_evidence_source
        FOREIGN KEY (evidence_source_id)
        REFERENCES corporate_intelligence.case_sources (source_id),

    ADD CONSTRAINT chk_correction_forward
        CHECK (replacement_claim_id > original_claim_id),
    ADD CONSTRAINT chk_restoration_target
        CHECK ((correction_kind = 'RESTORATION') = (restored_claim_id IS NOT NULL)),
    ADD CONSTRAINT chk_restoration_backward
        CHECK (restored_claim_id IS NULL OR restored_claim_id < original_claim_id),
    ADD CONSTRAINT chk_evidence_complete
        CHECK ((evidence_source_id IS NULL) = (evidence_location IS NULL));

DELIMITER $$

CREATE TRIGGER corporate_intelligence.trg_claim_corrections_no_update
BEFORE UPDATE ON corporate_intelligence.claim_corrections FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'claim_corrections is append-only'$$

CREATE TRIGGER corporate_intelligence.trg_claim_corrections_no_delete
BEFORE DELETE ON corporate_intelligence.claim_corrections FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'claim_corrections is append-only'$$

CREATE TRIGGER corporate_intelligence.trg_disclosure_claims_no_update
BEFORE UPDATE ON corporate_intelligence.disclosure_claims FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'disclosure_claims are superseded, never updated'$$

DELIMITER ;
