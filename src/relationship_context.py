
"""
Read-side queries for the dashboard's relationship context (Milestone
23B.2B-2). Works with and without the 23B.2B observation schema.

Without it (production today) the queries are exactly the legacy ones.
With it:
  * an anchor created by a GLEIF import batch counts only once that batch
    is COMPLETED; anchors from STARTED or FAILED batches are never shown;
  * a relationship known only from incomplete batches is not listed;
  * each anchor carries its origin (LEGACY_IMPORT or GLEIF_BATCH) and the
    batch's coverage, so a completed sample is not taken for a dataset;
  * a batch anchor's periods come from the observation that batch
    recorded, labelled as such, because such anchors have no rows in the
    legacy relationship_periods table. They are never shown as "no
    periods reported".
"""

KEY = ("child_lei", "parent_lei", "relationship_type")


def observation_schema_installed(connection):
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = "
                       "DATABASE() AND TABLE_NAME = 'relationship_evidence' "
                       "AND COLUMN_NAME = 'first_batch_id'")
        return cursor.fetchone()[0] == 1
    finally:
        cursor.close()


_SELECT = """
    SELECT
        c.lei AS child_lei,
        c.legal_name AS child_name,
        c.jurisdiction AS child_jurisdiction,
        p.lei AS parent_lei,
        p.legal_name AS parent_name,
        p.jurisdiction AS parent_jurisdiction,
        r.relationship_type,
        e.validation_sources,
        e.validation_documents,
        e.validation_reference,
        e.last_update_at_utc,
        e.snapshot_at_utc,
        e.source_file{extra_columns}
    FROM corporate_relationships r
    JOIN legal_entities c ON c.lei = r.child_lei
    JOIN legal_entities p ON p.lei = r.parent_lei
    LEFT JOIN relationship_evidence e
      ON e.child_lei = r.child_lei
     AND e.parent_lei = r.parent_lei
     AND e.relationship_type = r.relationship_type
     AND e.snapshot_at_utc = (
         SELECT MAX(e2.snapshot_at_utc)
         FROM relationship_evidence e2
         WHERE e2.child_lei = r.child_lei
           AND e2.parent_lei = r.parent_lei
           AND e2.relationship_type = r.relationship_type{eligible}
     ){joins}
    WHERE r.relationship_status = 'ACTIVE'
      AND r.registration_status = 'PUBLISHED'
      AND r.relationship_type IN (
          'IS_DIRECTLY_CONSOLIDATED_BY',
          'IS_ULTIMATELY_CONSOLIDATED_BY'
      ){visible}
    """

_ELIGIBLE = """
           AND (e2.first_batch_id IS NULL OR EXISTS (
               SELECT 1 FROM gleif_import_batches b2
               WHERE b2.batch_id = e2.first_batch_id AND b2.status = 'COMPLETED'))"""

_EXTRA = """,
        e.first_batch_id AS anchor_batch_id,
        IF(e.child_lei IS NULL, NULL,
           IF(e.first_batch_id IS NULL, 'LEGACY_IMPORT', 'GLEIF_BATCH')) AS anchor_origin,
        ab.selection_mode AS anchor_selection_mode,
        af.publication_scope AS anchor_publication_scope{quarantine}"""

_JOINS = """
    LEFT JOIN gleif_import_batches ab ON ab.batch_id = e.first_batch_id
    LEFT JOIN gleif_source_files af ON af.source_file_id = ab.source_file_id"""

# A relationship stays listed if it has a shown anchor, no observations at
# all (legacy identity), or membership in some completed batch.
_VISIBLE = """
      AND (e.child_lei IS NOT NULL
           OR NOT EXISTS (
               SELECT 1 FROM relationship_observations o
               WHERE o.child_lei = r.child_lei AND o.parent_lei = r.parent_lei
                 AND o.relationship_type = r.relationship_type)
           OR EXISTS (
               SELECT 1 FROM relationship_batch_observations m
               JOIN relationship_observations o USING (observation_id)
               JOIN gleif_import_batches b ON b.batch_id = m.batch_id
               WHERE o.child_lei = r.child_lei AND o.parent_lei = r.parent_lei
                 AND o.relationship_type = r.relationship_type
                 AND b.status = 'COMPLETED'))"""

LEGACY_PERIODS_SQL = """
    SELECT
        child_lei,
        parent_lei,
        relationship_type,
        snapshot_at_utc,
        period_type,
        period_start_utc,
        period_end_utc
    FROM relationship_periods
    """

# The periods of every observation the anchoring (completed) batch
# includes for the anchored relationship, keyed by the anchor.
OBSERVATION_PERIODS_SQL = """
    SELECT
        e.child_lei,
        e.parent_lei,
        e.relationship_type,
        e.snapshot_at_utc,
        p.period_type,
        p.start_at_utc AS period_start_utc,
        p.end_at_utc AS period_end_utc,
        m.source_record_ordinal AS source_record_ordinal
    FROM relationship_evidence e
    JOIN gleif_import_batches b ON b.batch_id = e.first_batch_id AND b.status = 'COMPLETED'
    JOIN relationship_batch_observations m ON m.batch_id = b.batch_id
    JOIN relationship_observations o ON o.observation_id = m.observation_id
     AND o.child_lei = e.child_lei AND o.parent_lei = e.parent_lei
     AND o.relationship_type = e.relationship_type
    JOIN relationship_observation_periods p ON p.observation_id = o.observation_id
    ORDER BY m.source_record_ordinal, p.period_index
    """

# How many distinct observations the anchoring batch has for each anchor
# (more than one: the file reported the relationship differently twice).
ANCHOR_OBSERVATIONS_SQL = """
    SELECT e.child_lei, e.parent_lei, e.relationship_type, e.snapshot_at_utc,
           COUNT(*) AS anchor_observations
    FROM relationship_evidence e
    JOIN gleif_import_batches b ON b.batch_id = e.first_batch_id AND b.status = 'COMPLETED'
    JOIN relationship_batch_observations m ON m.batch_id = b.batch_id
    JOIN relationship_observations o ON o.observation_id = m.observation_id
     AND o.child_lei = e.child_lei AND o.parent_lei = e.parent_lei
     AND o.relationship_type = e.relationship_type
    GROUP BY e.child_lei, e.parent_lei, e.relationship_type, e.snapshot_at_utc
    """


_QUARANTINE = """,
        ab.quarantined_inserted + ab.quarantined_already_recorded AS anchor_quarantined_records"""


def quarantine_schema_installed(connection):
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = "
                       "DATABASE() AND TABLE_NAME = 'gleif_import_batches' "
                       "AND COLUMN_NAME = 'quarantined_inserted'")
        return cursor.fetchone()[0] == 1
    finally:
        cursor.close()


def relationships_sql(installed, quarantine=False):
    if not installed:
        return _SELECT.format(extra_columns="", eligible="", joins="", visible="")
    extra = _EXTRA.format(quarantine=_QUARANTINE if quarantine else "")
    return _SELECT.format(extra_columns=extra, eligible=_ELIGIBLE, joins=_JOINS, visible=_VISIBLE)


def load_relationship_context(connection):
    """(relationships, periods, installed) as lists of dicts, read-only."""

    installed = observation_schema_installed(connection)
    quarantine = installed and quarantine_schema_installed(connection)
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(relationships_sql(installed, quarantine))
        relationships = cursor.fetchall()
        cursor.execute(LEGACY_PERIODS_SQL)
        periods = [{**row, "period_source": "LEGACY_PERIOD_TABLE"} for row in cursor.fetchall()]
        if installed:
            cursor.execute(OBSERVATION_PERIODS_SQL)
            periods += [{**row, "period_source": "GLEIF_OBSERVATION"} for row in cursor.fetchall()]
            cursor.execute(ANCHOR_OBSERVATIONS_SQL)
            counts = {tuple(r[k] for k in (*KEY, "snapshot_at_utc")): r["anchor_observations"]
                      for r in cursor.fetchall()}
            for row in relationships:
                row["anchor_observations"] = counts.get(
                    tuple(row[k] for k in (*KEY, "snapshot_at_utc")))
    finally:
        cursor.close()
        connection.rollback()
    return relationships, periods, installed
