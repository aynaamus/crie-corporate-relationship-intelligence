
USE corporate_intelligence;

CREATE OR REPLACE VIEW v_current_disclosure_claims AS
SELECT c.*
FROM disclosure_claims AS c
WHERE NOT EXISTS (
    SELECT 1
    FROM claim_corrections AS cc
    WHERE cc.original_claim_id = c.claim_id
);