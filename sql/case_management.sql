
USE corporate_intelligence;

-- One investigation per relationship and source snapshot
CREATE TABLE IF NOT EXISTS investigation_cases (
    case_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

    child_lei CHAR(20) NOT NULL,
    parent_lei CHAR(20) NOT NULL,
    relationship_type VARCHAR(64) NOT NULL,
    snapshot_at_utc DATETIME NOT NULL,

    review_status ENUM(
        'OPEN',
        'IN_PROGRESS',
        'NEEDS_MORE_EVIDENCE',
        'CLOSED'
    ) NOT NULL DEFAULT 'OPEN',

    assessment ENUM(
        'NOT_ASSESSED',
        'CORROBORATED',
        'INCONCLUSIVE',
        'CONTRADICTED'
    ) NOT NULL DEFAULT 'NOT_ASSESSED',

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_investigation (
        child_lei,
        parent_lei,
        relationship_type,
        snapshot_at_utc
    ),

    CONSTRAINT fk_case_evidence
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
    )
) ENGINE=InnoDB;

-- Append-only investigative observations
CREATE TABLE IF NOT EXISTS case_notes (
    note_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    case_id BIGINT UNSIGNED NOT NULL,

    note_text TEXT NOT NULL,
    source_url VARCHAR(2048),

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_case_note
    FOREIGN KEY (case_id)
    REFERENCES investigation_cases(case_id),

    INDEX idx_case_notes (case_id)
) ENGINE=InnoDB;
