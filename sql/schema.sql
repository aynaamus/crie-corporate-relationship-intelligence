
CREATE DATABASE IF NOT EXISTS corporate_intelligence
CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci;

USE corporate_intelligence;

CREATE TABLE IF NOT EXISTS legal_entities (
    lei CHAR(20) PRIMARY KEY,
    legal_name VARCHAR(1000),
    jurisdiction VARCHAR(20),
    country VARCHAR(10),
    registration_status VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
