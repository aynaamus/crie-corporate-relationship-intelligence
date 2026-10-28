
USE corporate_intelligence;

CREATE TABLE IF NOT EXISTS case_sources (
    source_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    case_id BIGINT UNSIGNED NOT NULL,

    document_title VARCHAR(500) NOT NULL,
    publisher VARCHAR(255),
    source_url TEXT NOT NULL,
    publication_date DATE,

    evidence_assessment ENUM(
        'NOT_REVIEWED',
        'SUPPORTS',
        'CONTRADICTS',
        'INCONCLUSIVE'
    ) NOT NULL DEFAULT 'NOT_REVIEWED',

    analyst_notes TEXT,

    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_case_source
        FOREIGN KEY (case_id)
        REFERENCES investigation_cases(case_id),

    INDEX idx_case_sources (case_id)
) ENGINE=InnoDB;