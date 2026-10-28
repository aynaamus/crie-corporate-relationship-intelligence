-- DEMONSTRATION DATABASE ONLY (corporate_intelligence_e2e on the hosted server).
-- Never run against corporate_intelligence or a local server.
--
-- Two accounts for the hosted CRIE. The server generates each password and
-- prints it once; store them only in the build environment and in the hosting
-- service's secrets, never in a file in the repository.
--   crie_demo_builder: builds and resets the demonstration
--     (scripts/demo_synthetic_case.py build and cleanup). Remove it, or revoke
--     its grant, when the build is done.
--   crie_demo_app: the hosted application's account. SELECT, INSERT and UPDATE
--     only: users' investigations are added and updated, never deleted, so the
--     database refuses every DELETE whatever the application does. Ownership is
--     checked by the application before every write and, for publication, by
--     the database's trigger.
-- Requires an administrative account. Run with the mysql client, without --force.
-- Replace '%' with the narrowest host pattern the hosting provider allows.

CREATE USER 'crie_demo_builder'@'%' IDENTIFIED BY RANDOM PASSWORD;
GRANT SELECT, INSERT, UPDATE, DELETE ON `corporate_intelligence_e2e`.* TO 'crie_demo_builder'@'%';

CREATE USER 'crie_demo_app'@'%' IDENTIFIED BY RANDOM PASSWORD;
GRANT SELECT, INSERT, UPDATE ON `corporate_intelligence_e2e`.* TO 'crie_demo_app'@'%';
