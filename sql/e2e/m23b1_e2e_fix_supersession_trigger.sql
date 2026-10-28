-- E2E DATABASE ONLY (corporate_intelligence_e2e). Never run against corporate_intelligence
-- or corporate_intelligence_test.
-- Milestone 23B.1 correction: exact scope comparison in the supersession trigger.
--
-- Identical to sql/test_fix_m23b1_supersession_trigger.sql with only the
-- database name changed. Replaces only the supersession trigger installed
-- by the first version of sql/e2e/m23b1_e2e_source_assessments.sql, whose
-- case- and trailing-space-insensitive comparison could accept a
-- cross-scope supersession. Changes no data. Requires an administrative
-- account (TRIGGER). Run with the mysql client, without --force.

DROP TRIGGER corporate_intelligence_e2e.trg_source_assessments_supersession;

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

DELIMITER ;
