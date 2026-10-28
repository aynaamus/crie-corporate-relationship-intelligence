
USE corporate_intelligence;

CREATE TABLE IF NOT EXISTS relationship_evidence (
    child_lei CHAR(20) NOT NULL,
    parent_lei CHAR(20) NOT NULL,
    relationship_type VARCHAR(64) NOT NULL,

    snapshot_at_utc DATETIME NOT NULL,
    initial_registration_at_utc DATETIME,
    last_update_at_utc DATETIME,
    next_renewal_at_utc DATETIME,

    managing_lou CHAR(20),
    validation_sources VARCHAR(40) NOT NULL,
    validation_documents VARCHAR(50) NOT NULL,
    validation_reference TEXT,

    periods_json JSON NOT NULL,
    source_file VARCHAR(255) NOT NULL,
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (
        child_lei,
        parent_lei,
        relationship_type,
        snapshot_at_utc
    ),

    CONSTRAINT fk_evidence_relationship
    FOREIGN KEY (
        child_lei,
        parent_lei,
        relationship_type
    )
    REFERENCES corporate_relationships (
        child_lei,
        parent_lei,
        relationship_type
    ),

    INDEX idx_validation_sources (validation_sources)
) ENGINE=InnoDB;
