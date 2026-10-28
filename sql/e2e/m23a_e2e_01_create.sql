-- E2E DATABASE ONLY (corporate_intelligence_e2e). Never run against corporate_intelligence
-- or corporate_intelligence_test. Milestone 23A, step 1 of 5.
--
-- Creates the disposable end-to-end database with production's character
-- set and collation. CREATE DATABASE (without IF NOT EXISTS) fails if the
-- database already exists, so nothing existing is ever reused or replaced.
-- Requires an administrative account.


CREATE DATABASE `corporate_intelligence_e2e` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
