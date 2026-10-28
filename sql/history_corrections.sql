
USE corporate_intelligence;

CREATE TABLE IF NOT EXISTS source_history_annotations (
    annotation_id BIGINT UNSIGNED
        AUTO_INCREMENT PRIMARY KEY,

    history_id BIGINT UNSIGNED NOT NULL,

    annotation_type ENUM(
        'ACCIDENTAL_DUPLICATE'
    ) NOT NULL,

    annotation_note TEXT NOT NULL,

    created_at_utc TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    UNIQUE KEY uq_history_annotation (
        history_id,
        annotation_type
    ),

    CONSTRAINT fk_annotation_history
        FOREIGN KEY (history_id)
        REFERENCES source_assessment_history(history_id)
) ENGINE=InnoDB;

-- Annotate only the specified repeated reviews,
-- and only when their content matches the original.

INSERT INTO source_history_annotations (
    history_id,
    annotation_type,
    annotation_note
)
SELECT
    h.history_id,
    'ACCIDENTAL_DUPLICATE',
    'Repeated submission of an existing review; original retained.'
FROM source_assessment_history h

JOIN source_assessment_history original
    ON original.history_id =
        CASE h.source_id
            WHEN 2 THEN 5
            WHEN 4 THEN 11
        END

LEFT JOIN source_history_annotations a
    ON a.history_id = h.history_id
    AND a.annotation_type = 'ACCIDENTAL_DUPLICATE'

WHERE (
    (h.source_id = 2 AND h.history_id IN (6, 7))
    OR
    (h.source_id = 4 AND h.history_id BETWEEN 12 AND 17)
)
AND original.source_id = h.source_id
AND h.event_type = 'REVIEW'
AND original.event_type = 'REVIEW'
AND h.previous_assessment = 'SUPPORTS'
AND h.new_assessment = 'SUPPORTS'
AND original.new_assessment = 'SUPPORTS'
AND h.review_reason <=> original.review_reason
AND a.annotation_id IS NULL;
