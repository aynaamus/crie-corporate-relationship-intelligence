
USE corporate_intelligence;

CREATE TABLE IF NOT EXISTS claim_corrections (

    correction_id BIGINT UNSIGNED
        AUTO_INCREMENT PRIMARY KEY,

    original_claim_id BIGINT UNSIGNED NOT NULL,

    replacement_claim_id BIGINT UNSIGNED NOT NULL,

    correction_reason TEXT NOT NULL,

    corrected_at_utc TIMESTAMP(6)
        NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    UNIQUE KEY uq_original_claim (
        original_claim_id
    ),

    UNIQUE KEY uq_replacement_claim (
        replacement_claim_id
    ),

    CONSTRAINT fk_correction_original
        FOREIGN KEY (original_claim_id)
        REFERENCES disclosure_claims(claim_id),

    CONSTRAINT fk_correction_replacement
        FOREIGN KEY (replacement_claim_id)
        REFERENCES disclosure_claims(claim_id),

    CONSTRAINT chk_distinct_claims
        CHECK (original_claim_id <> replacement_claim_id)

) ENGINE=InnoDB;