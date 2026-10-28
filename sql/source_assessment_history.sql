
USE corporate_intelligence;

CREATE TABLE IF NOT EXISTS source_assessment_history (
    history_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

    source_id BIGINT UNSIGNED NOT NULL,

    event_type ENUM(
        'BASELINE',
        'CREATED',
        'REVIEW'
    ) NOT NULL,

    previous_assessment ENUM(
        'NOT_REVIEWED',
        'SUPPORTS',
        'CONTRADICTS',
        'INCONCLUSIVE'
    ) NULL,

    new_assessment ENUM(
        'NOT_REVIEWED',
        'SUPPORTS',
        'CONTRADICTS',
        'INCONCLUSIVE'
    ) NOT NULL,

    review_reason TEXT,

    recorded_at_utc TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    CONSTRAINT fk_history_source
        FOREIGN KEY (source_id)
        REFERENCES case_sources(source_id),

    INDEX idx_source_history (
        source_id,
        history_id
    )
) ENGINE=InnoDB;

-- Create a starting audit record for existing sources.
-- This does not invent their earlier assessment history.

INSERT INTO source_assessment_history (
    source_id,
    event_type,
    previous_assessment,
    new_assessment,
    review_reason
)
SELECT
    s.source_id,
    'BASELINE',
    NULL,
    s.evidence_assessment,
    'Existing assessment captured at audit activation. Earlier changes, if any, are unknown.'
FROM case_sources AS s
WHERE NOT EXISTS (
    SELECT 1
    FROM source_assessment_history AS h
    WHERE h.source_id = s.source_id
);
