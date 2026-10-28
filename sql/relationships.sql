
USE corporate_intelligence;

CREATE TABLE IF NOT EXISTS corporate_relationships (
    child_lei CHAR(20) NOT NULL,
    parent_lei CHAR(20) NOT NULL,

    relationship_type VARCHAR(64) NOT NULL,
    relationship_status VARCHAR(30) NOT NULL,
    registration_status VARCHAR(30) NOT NULL,

    source_dataset VARCHAR(100)
        DEFAULT 'GLEIF RR-CDF',

    imported_at TIMESTAMP
        DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (
        child_lei,
        parent_lei,
        relationship_type
    ),

    INDEX idx_parent_lei (parent_lei),

    FOREIGN KEY (child_lei)
        REFERENCES legal_entities(lei),

    FOREIGN KEY (parent_lei)
        REFERENCES legal_entities(lei)
) ENGINE=InnoDB;
