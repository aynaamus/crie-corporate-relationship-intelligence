
USE corporate_intelligence;

CREATE TABLE IF NOT EXISTS disclosure_claims (
    claim_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

    source_id BIGINT UNSIGNED NOT NULL,
    entity_name VARCHAR(255) NOT NULL,
    metric VARCHAR(128) NOT NULL,

    report_year SMALLINT UNSIGNED NOT NULL,
    period_end DATE NOT NULL,

    value_text VARCHAR(500) NOT NULL,
    normalized_value VARCHAR(500) NOT NULL,
    source_location VARCHAR(500) NOT NULL,

    claim_fingerprint CHAR(64)
        CHARACTER SET ascii
        COLLATE ascii_bin NOT NULL,

    created_at TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    UNIQUE KEY uq_claim_fingerprint (claim_fingerprint),

    INDEX idx_claim_comparison (
        entity_name,
        metric,
        report_year,
        period_end
    ),

    CONSTRAINT fk_claim_source
        FOREIGN KEY (source_id)
        REFERENCES case_sources(source_id)

) ENGINE=InnoDB;
