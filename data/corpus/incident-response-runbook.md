# Incident Response Runbook

Owner: Site Reliability Engineering. Applies to every production service listed in the Service Catalog §1.

## 1. Severity levels

SEV1: full outage of a Tier 1 service or confirmed customer data loss.
SEV2: major degradation of a Tier 1 service, or a full outage of a Tier 2 service.
SEV3: partial degradation with a known workaround.
SEV4: cosmetic issues or problems with internal tooling.

## 2. Declaring an incident

Anyone can declare an incident in the #incidents Slack channel with the /incident command. Whoever declares it is the interim Incident Commander (IC) until the on-call engineer accepts the page. SEV1 and SEV2 incidents page the primary on-call immediately. SEV3 and SEV4 incidents create a ticket for the next business day.

## 3. Escalation

### 3.1 SEV1 escalation

If the primary on-call has not acknowledged a SEV1 page within 5 minutes, the page escalates to the secondary on-call, and after 15 minutes to the Engineering Manager on duty. Escalation contacts for each rotation are listed in the On-call Handbook §2. The IC posts the first customer update within 30 minutes of declaring the incident, following the Customer Communication Guide §3.

### 3.2 SEV2 escalation

For a SEV2, the page escalates to the secondary on-call after 15 minutes without acknowledgement. Customer updates follow the cadence in the Customer Communication Guide §3.

## 4. Client-site response

When an incident needs hands-on work at a customer warehouse (for example, a failed scanner gateway), the IC may dispatch a field engineer. Travel booked for a client-site incident is exempt from the 14-day advance booking rule; see the Travel and Expense Policy §2.

## 5. Production access during incidents

Engineers who need production database access during an incident use break-glass credentials. Every break-glass session must be recorded in the incident timeline with its start time, end time and reason, and is reviewed as described in the Security Access Policy §4.

## 6. Postmortems

A blameless postmortem is required for every SEV1 and SEV2 within 5 business days of resolution. Action items are tracked in Jira with the label postmortem, and each one needs an owner and a due date.
