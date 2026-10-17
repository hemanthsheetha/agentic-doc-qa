# Backup and Recovery Standard

## 1. Purpose

Defines backup frequency, retention and recovery objectives for each service tier defined in the Service Catalog §1.

## 2. Recovery objectives

Tier 1: a recovery point objective (RPO) of 15 minutes and a recovery time objective (RTO) of 1 hour. Tier 2: an RPO of 4 hours and an RTO of 8 hours. Tier 3: an RPO of 24 hours and an RTO of 3 business days.

## 3. Backup retention

Tier 1 and Tier 2 backups are kept for 35 days, and monthly snapshots for 13 months. Tier 3 backups are kept for 14 days. Longer retention for regulated data is set by the Data Retention Policy §2.

## 4. Restore testing

Each Tier 1 service must complete a restore test every quarter, and each Tier 2 service every six months. Results are recorded in the reliability review.
