
USE corporate_intelligence;

CREATE TABLE IF NOT EXISTS source_metadata_history (
    change_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_id BIGINT UNSIGNED NOT NULL,

    previous_metadata JSON NOT NULL,
    updated_metadata JSON NOT NULL,

    change_reason TEXT NOT NULL,

    changed_at_utc TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    CONSTRAINT fk_metadata_source
        FOREIGN KEY (source_id)
        REFERENCES case_sources(source_id)
) ENGINE=InnoDB;
