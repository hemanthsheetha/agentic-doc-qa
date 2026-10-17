# Vendor Management Policy

## 1. Critical vendors

Our critical vendors are PagerDuty (paging), Okta (identity), Navan (travel booking), AWS (hosting) and Twilio (SMS and voice for the Notification Service).

## 2. Vendor criticality

A vendor is critical if any Tier 1 service depends on it. Critical vendors are reviewed every year by the owning team's lead.

## 3. Vendor outages

An outage of a critical vendor that affects one of our services is handled as an incident at the severity of its impact on that service. The owning team opens a support ticket with the vendor within 1 hour of the incident being declared.

## 4. Data processing

Vendors that process personal data must sign a data processing agreement and delete that data within our own retention periods (see the Data Retention Policy §2).

## 5. Vendor access

Vendor staff never get standing production access. Temporary access is granted through Okta groups as described in the Security Access Policy §2, but for vendors it expires after 7 days instead of the usual period.
