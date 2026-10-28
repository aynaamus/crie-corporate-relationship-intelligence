-- READ-ONLY. Milestone 23B.2B-1 installation check (SELECT only).
--
-- Lists, in migration order, every object m23b2b creates and whether it
-- exists in the schema named below, so a migration that stopped part way
-- can be diagnosed instead of rerun blindly. Every DDL statement commits on
-- its own: objects listed PRESENT before the first MISSING one were created
-- and must not be created again. Then the anchor's existing keys are listed
-- so they can be compared with the expected, unchanged definitions.
--
-- Set the schema first (test, E2E or, once authorized, production), e.g.
--   SET @crie_schema = 'corporate_intelligence_test';
-- and run with an account that can see triggers (TRIGGER privilege).

SELECT expected.ordinal, expected.kind, expected.name,
       IF(CASE expected.kind
            WHEN 'TABLE' THEN EXISTS (SELECT 1 FROM information_schema.TABLES t
                WHERE t.TABLE_SCHEMA = @crie_schema AND t.TABLE_NAME = expected.name)
            WHEN 'COLUMN' THEN EXISTS (SELECT 1 FROM information_schema.COLUMNS c
                WHERE c.TABLE_SCHEMA = @crie_schema
                  AND CONCAT(c.TABLE_NAME, '.', c.COLUMN_NAME) = expected.name)
            WHEN 'FOREIGN KEY' THEN EXISTS (SELECT 1 FROM information_schema.REFERENTIAL_CONSTRAINTS r
                WHERE r.CONSTRAINT_SCHEMA = @crie_schema
                  AND CONCAT(r.TABLE_NAME, '.', r.CONSTRAINT_NAME) = expected.name)
            ELSE EXISTS (SELECT 1 FROM information_schema.TRIGGERS g
                WHERE g.TRIGGER_SCHEMA = @crie_schema AND g.TRIGGER_NAME = expected.name)
          END, 'PRESENT', 'MISSING') AS state
FROM (VALUES
    ROW(1, 'TABLE', 'gleif_source_files'),
    ROW(2, 'TABLE', 'gleif_import_batches'),
    ROW(3, 'TABLE', 'relationship_observations'),
    ROW(4, 'TABLE', 'relationship_observation_periods'),
    ROW(5, 'TABLE', 'legal_entity_observations'),
    ROW(6, 'TABLE', 'relationship_batch_observations'),
    ROW(7, 'TABLE', 'entity_batch_observations'),
    ROW(8, 'COLUMN', 'relationship_evidence.first_batch_id'),
    ROW(9, 'FOREIGN KEY', 'relationship_evidence.fk_evidence_first_batch'),
    ROW(10, 'TRIGGER', 'trg_gleif_source_files_no_update'),
    ROW(11, 'TRIGGER', 'trg_gleif_source_files_no_delete'),
    ROW(12, 'TRIGGER', 'trg_gleif_batches_start'),
    ROW(13, 'TRIGGER', 'trg_gleif_batches_lifecycle'),
    ROW(14, 'TRIGGER', 'trg_gleif_batches_no_delete'),
    ROW(15, 'TRIGGER', 'trg_relationship_observations_batch'),
    ROW(16, 'TRIGGER', 'trg_relationship_observations_no_update'),
    ROW(17, 'TRIGGER', 'trg_relationship_observations_no_delete'),
    ROW(18, 'TRIGGER', 'trg_relationship_observation_periods_batch'),
    ROW(19, 'TRIGGER', 'trg_relationship_observation_periods_no_update'),
    ROW(20, 'TRIGGER', 'trg_relationship_observation_periods_no_delete'),
    ROW(21, 'TRIGGER', 'trg_entity_observations_batch'),
    ROW(22, 'TRIGGER', 'trg_entity_observations_no_update'),
    ROW(23, 'TRIGGER', 'trg_entity_observations_no_delete'),
    ROW(24, 'TRIGGER', 'trg_relationship_batch_observations_batch'),
    ROW(25, 'TRIGGER', 'trg_relationship_batch_observations_no_update'),
    ROW(26, 'TRIGGER', 'trg_relationship_batch_observations_no_delete'),
    ROW(27, 'TRIGGER', 'trg_entity_batch_observations_batch'),
    ROW(28, 'TRIGGER', 'trg_entity_batch_observations_no_update'),
    ROW(29, 'TRIGGER', 'trg_entity_batch_observations_no_delete'),
    ROW(30, 'TRIGGER', 'trg_evidence_first_batch_insert'),
    ROW(31, 'TRIGGER', 'trg_evidence_first_batch_fixed')
) AS expected (ordinal, kind, name)
ORDER BY expected.ordinal;

-- Expected unchanged: PRIMARY (child_lei, parent_lei, relationship_type,
-- snapshot_at_utc), fk_evidence_relationship, and the two references
-- investigation_cases.fk_case_evidence and relationship_periods.fk_period_evidence
-- (production and E2E only; the test schema has neither referencing table).
SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.REFERENCED_TABLE_NAME,
       GROUP_CONCAT(k.COLUMN_NAME ORDER BY k.ORDINAL_POSITION) AS columns
FROM information_schema.KEY_COLUMN_USAGE k
WHERE k.TABLE_SCHEMA = @crie_schema
  AND (k.TABLE_NAME = 'relationship_evidence' OR k.REFERENCED_TABLE_NAME = 'relationship_evidence')
GROUP BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.REFERENCED_TABLE_NAME
ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME;

-- Legacy anchors must all still have no batch (fails, harmlessly, while
-- first_batch_id is MISSING).
SET @crie_check = CONCAT('SELECT COUNT(*) AS anchors, ',
    'COALESCE(SUM(first_batch_id IS NOT NULL), 0) AS anchors_with_batch FROM `',
    REPLACE(@crie_schema, '`', ''), '`.relationship_evidence');
PREPARE crie_check FROM @crie_check;
EXECUTE crie_check;
DEALLOCATE PREPARE crie_check;
