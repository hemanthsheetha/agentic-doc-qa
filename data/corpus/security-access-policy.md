# Security Access Policy

## 1. Authentication

All employees sign in with SSO and a hardware security key. SMS codes are not allowed as a second factor.

## 2. Least privilege

Production access is granted per team through Okta groups and expires after 90 days unless the team's manager renews it.

## 3. Access reviews

Managers review their team's access every quarter. Access unused for more than 60 days is revoked automatically.

## 4. Break-glass access

Break-glass credentials grant time-limited production database access, for 4 hours at most. Each use must be justified in the incident timeline (see the Incident Response Runbook §5), and the Security team reviews it within 2 business days. Unjustified use is reported to the employee's director.
