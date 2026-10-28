-- READ-ONLY. Milestone 23B.2B-3a-2 installation check (SELECT, SET, PREPARE only).
--
-- Lists every object m23b2b3 adds or removes, in installation order, with
-- the state expected after a complete installation and the state found.
-- A failed run of the (non-idempotent) migration stops at the first
-- error; this shows exactly how far it got. Objects marked OK before the
-- first MISMATCH were applied and must not be applied again. Then the
-- batch and observation triggers are listed with their execution order,
-- and the anchor and legacy checks of m23b2b are repeated.
--
-- Set the schema first, e.g.
--   SET @crie_schema = 'corporate_intelligence_test';
-- and run with an account that can see triggers (TRIGGER privilege).

SELECT expected.ordinal, expected.kind, expected.name, expected.expected_state,
       IF(CASE expected.kind
            WHEN 'TABLE' THEN EXISTS (SELECT 1 FROM information_schema.TABLES t
                WHERE t.TABLE_SCHEMA = @crie_schema AND t.TABLE_NAME = expected.name)
            WHEN 'COLUMN' THEN EXISTS (SELECT 1 FROM information_schema.COLUMNS c
                WHERE c.TABLE_SCHEMA = @crie_schema
                  AND CONCAT(c.TABLE_NAME, '.', c.COLUMN_NAME) = expected.name)
            WHEN 'CHECK' THEN EXISTS (SELECT 1 FROM information_schema.TABLE_CONSTRAINTS k
                WHERE k.CONSTRAINT_SCHEMA = @crie_schema AND k.CONSTRAINT_TYPE = 'CHECK'
                  AND CONCAT(k.TABLE_NAME, '.', k.CONSTRAINT_NAME) = expected.name)
            ELSE EXISTS (SELECT 1 FROM information_schema.TRIGGERS g
                WHERE g.TRIGGER_SCHEMA = @crie_schema AND g.TRIGGER_NAME = expected.name)
          END, 'PRESENT', 'ABSENT') AS found_state,
       IF((CASE expected.kind
            WHEN 'TABLE' THEN EXISTS (SELECT 1 FROM information_schema.TABLES t
                WHERE t.TABLE_SCHEMA = @crie_schema AND t.TABLE_NAME = expected.name)
            WHEN 'COLUMN' THEN EXISTS (SELECT 1 FROM information_schema.COLUMNS c
                WHERE c.TABLE_SCHEMA = @crie_schema
                  AND CONCAT(c.TABLE_NAME, '.', c.COLUMN_NAME) = expected.name)
            WHEN 'CHECK' THEN EXISTS (SELECT 1 FROM information_schema.TABLE_CONSTRAINTS k
                WHERE k.CONSTRAINT_SCHEMA = @crie_schema AND k.CONSTRAINT_TYPE = 'CHECK'
                  AND CONCAT(k.TABLE_NAME, '.', k.CONSTRAINT_NAME) = expected.name)
            ELSE EXISTS (SELECT 1 FROM information_schema.TRIGGERS g
                WHERE g.TRIGGER_SCHEMA = @crie_schema AND g.TRIGGER_NAME = expected.name)
          END) = (expected.expected_state = 'PRESENT'), 'OK', 'MISMATCH') AS verdict
FROM (VALUES
    ROW(1, 'COLUMN', 'gleif_source_files.originator', 'PRESENT'),
    ROW(2, 'COLUMN', 'gleif_source_files.header_extension', 'PRESENT'),
    ROW(3, 'COLUMN', 'gleif_source_files.header_source_counts_consistent', 'PRESENT'),
    ROW(4, 'CHECK', 'gleif_source_files.chk_gleif_source_originator', 'PRESENT'),
    ROW(5, 'CHECK', 'gleif_source_files.chk_gleif_source_header_extension', 'PRESENT'),
    ROW(6, 'COLUMN', 'legal_entity_observations.canonical_record', 'PRESENT'),
    ROW(7, 'COLUMN', 'legal_entity_observations.entity_category', 'PRESENT'),
    ROW(8, 'COLUMN', 'legal_entity_observations.entity_sub_category', 'PRESENT'),
    ROW(9, 'COLUMN', 'legal_entity_observations.entity_created_at_utc', 'PRESENT'),
    ROW(10, 'COLUMN', 'legal_entity_observations.validation_sources', 'PRESENT'),
    ROW(11, 'COLUMN', 'legal_entity_observations.other_registration_authority_id', 'PRESENT'),
    ROW(12, 'COLUMN', 'legal_entity_observations.identifier_check', 'PRESENT'),
    ROW(13, 'COLUMN', 'legal_entity_observations.identifier_flags', 'PRESENT'),
    ROW(14, 'CHECK', 'legal_entity_observations.chk_entity_observation_canonical', 'PRESENT'),
    ROW(15, 'COLUMN', 'relationship_observations.identifier_flags', 'PRESENT'),
    ROW(16, 'CHECK', 'relationship_observations.chk_relationship_observation_identifier_flags', 'PRESENT'),
    ROW(17, 'COLUMN', 'gleif_import_batches.quarantined_inserted', 'PRESENT'),
    ROW(18, 'COLUMN', 'gleif_import_batches.quarantined_already_recorded', 'PRESENT'),
    ROW(19, 'CHECK', 'gleif_import_batches.chk_gleif_batch_counts_v2', 'PRESENT'),
    ROW(20, 'TABLE', 'gleif_quarantined_records', 'PRESENT'),
    ROW(21, 'TABLE', 'gleif_batch_quarantined_records', 'PRESENT'),
    ROW(22, 'TABLE', 'gleif_batch_duplicate_occurrences', 'PRESENT'),
    ROW(23, 'TABLE', 'gleif_source_positions', 'PRESENT'),
    ROW(24, 'TABLE', 'gleif_batch_positions', 'PRESENT'),
    ROW(25, 'TRIGGER', 'trg_quarantined_records_batch', 'PRESENT'),
    ROW(26, 'TRIGGER', 'trg_quarantined_records_no_update', 'PRESENT'),
    ROW(27, 'TRIGGER', 'trg_quarantined_records_no_delete', 'PRESENT'),
    ROW(28, 'TRIGGER', 'trg_batch_quarantined_records_batch', 'PRESENT'),
    ROW(29, 'TRIGGER', 'trg_batch_quarantined_records_no_update', 'PRESENT'),
    ROW(30, 'TRIGGER', 'trg_batch_quarantined_records_no_delete', 'PRESENT'),
    ROW(31, 'TRIGGER', 'trg_duplicate_occurrences_batch', 'PRESENT'),
    ROW(32, 'TRIGGER', 'trg_duplicate_occurrences_no_update', 'PRESENT'),
    ROW(33, 'TRIGGER', 'trg_duplicate_occurrences_no_delete', 'PRESENT'),
    ROW(34, 'TRIGGER', 'trg_relationship_observations_not_quarantined', 'PRESENT'),
    ROW(35, 'TRIGGER', 'trg_entity_observations_not_quarantined', 'PRESENT'),
    ROW(36, 'TRIGGER', 'trg_relationship_observations_position', 'PRESENT'),
    ROW(37, 'TRIGGER', 'trg_entity_observations_position', 'PRESENT'),
    ROW(38, 'TRIGGER', 'trg_quarantined_records_position', 'PRESENT'),
    ROW(39, 'TRIGGER', 'trg_relationship_batch_observations_position', 'PRESENT'),
    ROW(40, 'TRIGGER', 'trg_entity_batch_observations_position', 'PRESENT'),
    ROW(41, 'TRIGGER', 'trg_batch_quarantined_records_position', 'PRESENT'),
    ROW(42, 'TRIGGER', 'trg_duplicate_occurrences_position', 'PRESENT'),
    ROW(43, 'TRIGGER', 'trg_source_positions_no_update', 'PRESENT'),
    ROW(44, 'TRIGGER', 'trg_source_positions_no_delete', 'PRESENT'),
    ROW(45, 'TRIGGER', 'trg_batch_positions_no_update', 'PRESENT'),
    ROW(46, 'TRIGGER', 'trg_batch_positions_no_delete', 'PRESENT'),
    ROW(47, 'TRIGGER', 'trg_gleif_batches_start_v2', 'PRESENT'),
    ROW(48, 'TRIGGER', 'trg_gleif_batches_lifecycle_v2', 'PRESENT'),
    ROW(49, 'TRIGGER', 'trg_gleif_batches_start', 'ABSENT'),
    ROW(50, 'TRIGGER', 'trg_gleif_batches_lifecycle', 'ABSENT'),
    ROW(51, 'CHECK', 'gleif_import_batches.chk_gleif_batch_counts', 'ABSENT')
) AS expected (ordinal, kind, name, expected_state)
ORDER BY expected.ordinal;

-- Trigger execution order per table and event (both originals and
-- replacements appear here only during an interrupted installation).
SELECT EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_ORDER, TRIGGER_NAME
FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA = @crie_schema
  AND EVENT_OBJECT_TABLE IN ('gleif_import_batches', 'relationship_observations',
                             'legal_entity_observations')
ORDER BY EVENT_OBJECT_TABLE, EVENT_MANIPULATION, ACTION_TIMING, ACTION_ORDER;

-- Row counts that must be zero before installation and are unchanged by it.
SET @crie_check = CONCAT(
    'SELECT (SELECT COUNT(*) FROM `', REPLACE(@crie_schema, '`', ''), '`.gleif_import_batches) AS batches, ',
    '(SELECT COUNT(*) FROM `', REPLACE(@crie_schema, '`', ''), '`.legal_entity_observations) AS entity_observations, ',
    '(SELECT COUNT(*) FROM `', REPLACE(@crie_schema, '`', ''), '`.relationship_observations) AS relationship_observations');
PREPARE crie_check FROM @crie_check;
EXECUTE crie_check;
DEALLOCATE PREPARE crie_check;
