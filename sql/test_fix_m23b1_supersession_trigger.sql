-- TEST DATABASE ONLY. Never run against corporate_intelligence.
-- Milestone 23B.1 correction: exact scope comparison in the supersession trigger.
--
-- The trigger installed by the first version of
-- sql/test_schema_upgrade_m23b1.sql compared entity_name and metric with
-- the column collation (utf8mb4_unicode_ci), which ignores case and
-- trailing spaces, while scope_key and reconciliation compare them
-- exactly. It could therefore accept superseding a "Acme" assessment
-- with an "ACME" one: a cross-scope link. This replaces only that
-- trigger with the corrected definition now in the migration files.
--
-- Changes no data. Between the DROP and the CREATE (one script run) the
-- test table briefly has no supersession check; run it while no tests run.
-- Requires an administrative account (TRIGGER). Run with the mysql
-- client, without --force.

DROP TRIGGER corporate_intelligence_test.trg_source_assessments_supersession;

DELIMITER $$

CREATE TRIGGER corporate_intelligence_test.trg_source_assessments_supersession
BEFORE INSERT ON corporate_intelligence_test.source_relationship_assessments FOR EACH ROW
BEGIN
    IF NEW.supersedes_assessment_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM corporate_intelligence_test.source_relationship_assessments p
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
