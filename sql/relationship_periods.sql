
USE corporate_intelligence;

CREATE TABLE IF NOT EXISTS relationship_periods (
    child_lei CHAR(20) NOT NULL,
    parent_lei CHAR(20) NOT NULL,
    relationship_type VARCHAR(64) NOT NULL,
    snapshot_at_utc DATETIME NOT NULL,

    period_index INT NOT NULL,
    period_type VARCHAR(40) NOT NULL,
    period_start_utc DATETIME NULL,
    period_end_utc DATETIME NULL,

    PRIMARY KEY (
        child_lei,
        parent_lei,
        relationship_type,
        snapshot_at_utc,
        period_index
    ),

    CONSTRAINT fk_period_evidence
    FOREIGN KEY (
        child_lei,
        parent_lei,
        relationship_type,
        snapshot_at_utc
    )
    REFERENCES relationship_evidence (
        child_lei,
        parent_lei,
        relationship_type,
        snapshot_at_utc
    ),

    INDEX idx_period_type (period_type)
) ENGINE=InnoDB;
