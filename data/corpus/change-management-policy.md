# Change Management Policy

## 1. Scope

Applies to every production change to a service listed in the Service Catalog §2, including configuration and database schema changes.

## 2. Change types

Standard changes are pre-approved, low-risk and repeatable, such as scaling a service within its configured limits. Normal changes need approval before they are deployed. Emergency changes fix an active incident.

## 3. Approvals by tier

A Normal change to a Tier 1 service needs approval from the owning team's lead and from the Site Reliability Engineering team. A Normal change to a Tier 2 service needs approval from the owning team's lead only. Tier 3 services need no approval.

## 4. Change freezes

No Normal changes may be made to Tier 1 or Tier 2 services from December 15 to January 5. The Billing Service is also frozen during the last 2 business days of every month, while the monthly invoice run is in progress.

## 5. Emergency changes

During an active SEV1 or SEV2, the Incident Commander may approve an emergency change, even during a freeze. The change must be documented in the change log within 1 business day and reviewed in the incident's postmortem (see the Incident Response Runbook §6).

## 6. Rollback

Every Normal change to a Tier 1 service needs a tested rollback plan that completes within the service's recovery time objective, as set in the Backup and Recovery Standard §2.
