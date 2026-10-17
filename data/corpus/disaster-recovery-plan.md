# Disaster Recovery Plan

## 1. Scope

Covers the loss of the primary cloud region, ca-central-1. The secondary region is ca-west-1.

## 2. Declaring a disaster

Only the VP Engineering can declare a disaster. If the VP Engineering cannot be reached within 30 minutes, the Director of Infrastructure may declare it instead. A declared disaster is always handled as a SEV1 under the Incident Response Runbook §1.

## 3. Failover order

Tier 1 services fail over first, in this order: Shipment Tracker, Route Optimizer, Driver App. Tier 2 services follow. Tier 3 services are restored only after every Tier 2 service is back. Each service must be restored within its recovery time objective (see the Backup and Recovery Standard §2).

## 4. Drills

The Site Reliability Engineering team runs a full failover drill twice a year, in April and October. Drill results are recorded in the reliability review.

## 5. Communication

Customers are updated at the SEV1 cadence in the Customer Communication Guide §3. Employees are updated in the #all-hands Slack channel.
