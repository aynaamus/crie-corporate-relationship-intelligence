-- E2E DATABASE ONLY (corporate_intelligence_e2e). Never run against corporate_intelligence
-- or corporate_intelligence_test. Milestone 23A, step 2 of 5.
--
-- Production's exact table and view definitions, taken verbatim from the
-- schema-only dump of the accepted backup pre-22.3B-20261025T025600Z (no
-- data). Every name in it is unqualified and the first statement selects
-- the E2E database, so nothing can resolve to another database.
-- Requires an administrative account (the view keeps DEFINER root@localhost).


USE `corporate_intelligence_e2e`;

--
-- ------------------------------------------------------

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `case_notes`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `case_notes` (
  `note_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `case_id` bigint unsigned NOT NULL,
  `note_text` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `source_url` varchar(2048) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`note_id`),
  KEY `idx_case_notes` (`case_id`),
  CONSTRAINT `fk_case_note` FOREIGN KEY (`case_id`) REFERENCES `investigation_cases` (`case_id`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `case_sources`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `case_sources` (
  `source_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `case_id` bigint unsigned NOT NULL,
  `document_title` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL,
  `publisher` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `source_url` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `publication_date` date DEFAULT NULL,
  `evidence_assessment` enum('NOT_REVIEWED','SUPPORTS','CONTRADICTS','INCONCLUSIVE') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'NOT_REVIEWED',
  `analyst_notes` text COLLATE utf8mb4_unicode_ci,
  `added_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`source_id`),
  KEY `idx_case_sources` (`case_id`),
  CONSTRAINT `fk_case_source` FOREIGN KEY (`case_id`) REFERENCES `investigation_cases` (`case_id`)
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `claim_corrections`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `claim_corrections` (
  `correction_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `original_claim_id` bigint unsigned NOT NULL,
  `replacement_claim_id` bigint unsigned NOT NULL,
  `correction_reason` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `corrected_at_utc` timestamp(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`correction_id`),
  UNIQUE KEY `uq_original_claim` (`original_claim_id`),
  UNIQUE KEY `uq_replacement_claim` (`replacement_claim_id`),
  CONSTRAINT `fk_correction_original` FOREIGN KEY (`original_claim_id`) REFERENCES `disclosure_claims` (`claim_id`),
  CONSTRAINT `fk_correction_replacement` FOREIGN KEY (`replacement_claim_id`) REFERENCES `disclosure_claims` (`claim_id`),
  CONSTRAINT `chk_distinct_claims` CHECK ((`original_claim_id` <> `replacement_claim_id`))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `corporate_relationships`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `corporate_relationships` (
  `child_lei` char(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `parent_lei` char(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `relationship_type` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `relationship_status` varchar(30) COLLATE utf8mb4_unicode_ci NOT NULL,
  `registration_status` varchar(30) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source_dataset` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT 'GLEIF RR-CDF',
  `imported_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`child_lei`,`parent_lei`,`relationship_type`),
  KEY `idx_parent_lei` (`parent_lei`),
  CONSTRAINT `corporate_relationships_ibfk_1` FOREIGN KEY (`child_lei`) REFERENCES `legal_entities` (`lei`),
  CONSTRAINT `corporate_relationships_ibfk_2` FOREIGN KEY (`parent_lei`) REFERENCES `legal_entities` (`lei`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `disclosure_claims`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `disclosure_claims` (
  `claim_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `source_id` bigint unsigned NOT NULL,
  `entity_name` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `metric` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `report_year` smallint unsigned NOT NULL,
  `period_end` date NOT NULL,
  `value_text` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL,
  `normalized_value` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source_location` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL,
  `claim_fingerprint` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `created_at` timestamp(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`claim_id`),
  UNIQUE KEY `uq_claim_fingerprint` (`claim_fingerprint`),
  KEY `idx_claim_comparison` (`entity_name`,`metric`,`report_year`,`period_end`),
  KEY `fk_claim_source` (`source_id`),
  CONSTRAINT `fk_claim_source` FOREIGN KEY (`source_id`) REFERENCES `case_sources` (`source_id`)
) ENGINE=InnoDB AUTO_INCREMENT=17 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `investigation_cases`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `investigation_cases` (
  `case_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `child_lei` char(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `parent_lei` char(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `relationship_type` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `snapshot_at_utc` datetime NOT NULL,
  `review_status` enum('OPEN','IN_PROGRESS','NEEDS_MORE_EVIDENCE','CLOSED') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'OPEN',
  `assessment` enum('NOT_ASSESSED','CORROBORATED','INCONCLUSIVE','CONTRADICTED') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'NOT_ASSESSED',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`case_id`),
  UNIQUE KEY `uq_investigation` (`child_lei`,`parent_lei`,`relationship_type`,`snapshot_at_utc`),
  CONSTRAINT `fk_case_evidence` FOREIGN KEY (`child_lei`, `parent_lei`, `relationship_type`, `snapshot_at_utc`) REFERENCES `relationship_evidence` (`child_lei`, `parent_lei`, `relationship_type`, `snapshot_at_utc`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `legal_entities`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `legal_entities` (
  `lei` char(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `legal_name` varchar(1000) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `jurisdiction` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `country` varchar(10) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `registration_status` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`lei`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `relationship_evidence`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `relationship_evidence` (
  `child_lei` char(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `parent_lei` char(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `relationship_type` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `snapshot_at_utc` datetime NOT NULL,
  `initial_registration_at_utc` datetime DEFAULT NULL,
  `last_update_at_utc` datetime DEFAULT NULL,
  `next_renewal_at_utc` datetime DEFAULT NULL,
  `managing_lou` char(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `validation_sources` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL,
  `validation_documents` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `validation_reference` text COLLATE utf8mb4_unicode_ci,
  `periods_json` json NOT NULL,
  `source_file` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `imported_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`child_lei`,`parent_lei`,`relationship_type`,`snapshot_at_utc`),
  KEY `idx_validation_sources` (`validation_sources`),
  CONSTRAINT `fk_evidence_relationship` FOREIGN KEY (`child_lei`, `parent_lei`, `relationship_type`) REFERENCES `corporate_relationships` (`child_lei`, `parent_lei`, `relationship_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `relationship_periods`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `relationship_periods` (
  `child_lei` char(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `parent_lei` char(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `relationship_type` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `snapshot_at_utc` datetime NOT NULL,
  `period_index` int NOT NULL,
  `period_type` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL,
  `period_start_utc` datetime DEFAULT NULL,
  `period_end_utc` datetime DEFAULT NULL,
  PRIMARY KEY (`child_lei`,`parent_lei`,`relationship_type`,`snapshot_at_utc`,`period_index`),
  KEY `idx_period_type` (`period_type`),
  CONSTRAINT `fk_period_evidence` FOREIGN KEY (`child_lei`, `parent_lei`, `relationship_type`, `snapshot_at_utc`) REFERENCES `relationship_evidence` (`child_lei`, `parent_lei`, `relationship_type`, `snapshot_at_utc`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `source_assessment_history`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `source_assessment_history` (
  `history_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `source_id` bigint unsigned NOT NULL,
  `event_type` enum('BASELINE','CREATED','REVIEW') COLLATE utf8mb4_unicode_ci NOT NULL,
  `previous_assessment` enum('NOT_REVIEWED','SUPPORTS','CONTRADICTS','INCONCLUSIVE') COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `new_assessment` enum('NOT_REVIEWED','SUPPORTS','CONTRADICTS','INCONCLUSIVE') COLLATE utf8mb4_unicode_ci NOT NULL,
  `review_reason` text COLLATE utf8mb4_unicode_ci,
  `recorded_at_utc` timestamp(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`history_id`),
  KEY `idx_source_history` (`source_id`,`history_id`),
  CONSTRAINT `fk_history_source` FOREIGN KEY (`source_id`) REFERENCES `case_sources` (`source_id`)
) ENGINE=InnoDB AUTO_INCREMENT=18 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `source_history_annotations`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `source_history_annotations` (
  `annotation_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `history_id` bigint unsigned NOT NULL,
  `annotation_type` enum('ACCIDENTAL_DUPLICATE') COLLATE utf8mb4_unicode_ci NOT NULL,
  `annotation_note` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at_utc` timestamp(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`annotation_id`),
  UNIQUE KEY `uq_history_annotation` (`history_id`,`annotation_type`),
  CONSTRAINT `fk_annotation_history` FOREIGN KEY (`history_id`) REFERENCES `source_assessment_history` (`history_id`)
) ENGINE=InnoDB AUTO_INCREMENT=16 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `source_metadata_history`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `source_metadata_history` (
  `change_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `source_id` bigint unsigned NOT NULL,
  `previous_metadata` json NOT NULL,
  `updated_metadata` json NOT NULL,
  `change_reason` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `changed_at_utc` timestamp(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`change_id`),
  KEY `fk_metadata_source` (`source_id`),
  CONSTRAINT `fk_metadata_source` FOREIGN KEY (`source_id`) REFERENCES `case_sources` (`source_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Temporary view structure for view `v_current_disclosure_claims`
--

SET @saved_cs_client     = @@character_set_client;
/*!50503 SET character_set_client = utf8mb4 */;
/*!50001 CREATE VIEW `v_current_disclosure_claims` AS SELECT 
 1 AS `claim_id`,
 1 AS `source_id`,
 1 AS `entity_name`,
 1 AS `metric`,
 1 AS `report_year`,
 1 AS `period_end`,
 1 AS `value_text`,
 1 AS `normalized_value`,
 1 AS `source_location`,
 1 AS `claim_fingerprint`,
 1 AS `created_at`*/;
SET character_set_client = @saved_cs_client;

--
-- Dumping events for database 'corporate_intelligence'
--

--
-- Dumping routines for database 'corporate_intelligence'
--

--
-- Final view structure for view `v_current_disclosure_claims`
--

/*!50001 DROP VIEW IF EXISTS `v_current_disclosure_claims`*/;
/*!50001 SET @saved_cs_client          = @@character_set_client */;
/*!50001 SET @saved_cs_results         = @@character_set_results */;
/*!50001 SET @saved_col_connection     = @@collation_connection */;
/*!50001 SET character_set_client      = utf8mb4 */;
/*!50001 SET character_set_results     = utf8mb4 */;
/*!50001 SET collation_connection      = utf8mb4_0900_ai_ci */;
/*!50001 CREATE ALGORITHM=UNDEFINED */
/*!50013 DEFINER=`root`@`localhost` SQL SECURITY DEFINER */
/*!50001 VIEW `v_current_disclosure_claims` AS select `c`.`claim_id` AS `claim_id`,`c`.`source_id` AS `source_id`,`c`.`entity_name` AS `entity_name`,`c`.`metric` AS `metric`,`c`.`report_year` AS `report_year`,`c`.`period_end` AS `period_end`,`c`.`value_text` AS `value_text`,`c`.`normalized_value` AS `normalized_value`,`c`.`source_location` AS `source_location`,`c`.`claim_fingerprint` AS `claim_fingerprint`,`c`.`created_at` AS `created_at` from `disclosure_claims` `c` where (not exists(select 1 from `claim_corrections` `cc` where (`cc`.`original_claim_id` = `c`.`claim_id`))) */;
/*!50001 SET character_set_client      = @saved_cs_client */;
/*!50001 SET character_set_results     = @saved_cs_results */;
/*!50001 SET collation_connection      = @saved_col_connection */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;
