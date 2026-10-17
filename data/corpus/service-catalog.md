# Service Catalog

## 1. Service tiers

Tier 1 services are customer-facing and revenue-critical. Tier 2 services are customer-facing but have a manual workaround. Tier 3 services are internal tools.

## 2. Services

### 2.1 Route Optimizer

Plans daily delivery routes for customer fleets. Tier 1 (recovery objectives: see the Backup and Recovery Standard §2). Owner: Routing team. Primary datastore: the PostgreSQL cluster routes-prod.

### 2.2 Shipment Tracker

Customer-facing tracking pages and webhooks. Tier 1 (recovery objectives: see the Backup and Recovery Standard §2). Owner: Tracking team.

### 2.3 Billing Service

Generates monthly invoices. Tier 2, because invoices can be issued manually from the finance console. Owner: Payments team.

### 2.4 Warehouse Scanner Gateway

Receives barcode scans from customer warehouses. Tier 2. Owner: Edge team.

### 2.5 Analytics Portal

Internal dashboards for operations staff. Tier 3. Owner: Data Platform team.

### 2.6 Driver App

Mobile app that drivers use to receive routes and confirm deliveries. It records driver GPS traces. Tier 1. Owner: Edge team.

### 2.7 Notification Service

Sends delivery SMS and email notifications to end customers through Twilio. Tier 2, because notifications can be resent in bulk from the admin console. Owner: Tracking team.

### 2.8 Route Planner (legacy)

The previous route planning tool, kept read-only for historical route lookups until it is retired. Tier 3. Owner: Routing team. Do not confuse it with the Route Optimizer.
