# Where the agent shines

These are findings from manual runs on tenant `ops` (15 policy documents, local `qwen3:4b-instruct`, recorded
2026-10-17). They complement the scores in the [README](../README.md#results).

## Aggregation: the agent's clearest advantage

**Question:** *"List every Tier 1 service, and for each one, name the person who leads the team that owns it."*

**Expected answer:** Shipment Tracker: Aisha Okafor (Tracking team). Route Optimizer: Marcus Chen (Routing team).
Driver App: Sofia Martins (Edge team).

| Strategy | Result | How |
|---|---|---|
| **Agent** | 6/6, 38 s | Methodical: it finds the Tier 1 services, then looks up each service and each team one by one (8 steps), and cites every pair |
| **Graph** | 4/6, 4 s | Drops the Driver App. Fixed expansion has an 8-source budget, and the third service's chain didn't fit |
| **Hybrid** | 6/6, 18 s | Got lucky: the Team Directory is one dense section listing every team lead, so a single hit contained all three names |

The agent doesn't beat hybrid here either. **On this corpus, the agent's advantage is how well it reasons and shows its
work, not getting answers right that the others get wrong.**

## Where it shines today

- **Correct justification.** On the GPS question below, only the agent explained *why* the outage is SEV1 (a full outage
  of a Tier 1 service, Incident Response Runbook §1). Hybrid reached the right number despite a gap in its reasoning,
  and graph gave the wrong reason.
- **Repeating a lookup per item.** Graph expansion runs out of room after 2–3 items; the agent loops. That will matter
  more as lists get longer.
- **Traceability.** Every hop is an explicit, cited step you can follow in the trace.

## Why it can't pull further ahead here

- **The corpus is tiny:** 15 short documents with concentrated facts. One dense section, such as the Team Directory,
  hands hybrid search the answer.
- **Graph expansion already covers 2 hops,** and most questions in this corpus need only that.

## Where the agent should pull clearly ahead

- Larger corpora, where answers aren't sitting in one dense section.
- Chains longer than 2 hops.
- Questions where the next search depends on a value found earlier ("the second service", "the team that owns X").
- The SEC filings, which have no graph, so hybrid + graph has nothing to expand along.

## Demo questions for the agent

Run these in **Agent Chat** and compare with **Policy Graph Chat**. Open the 🧠 Agent trace to show the steps.

**1. Aggregation (agent vs graph)**

*"List every Tier 1 service, and for each one, name the person who leads the team that owns it."*

- **Expected:** Shipment Tracker: Aisha Okafor. Route Optimizer: Marcus Chen. Driver App: Sofia Martins.
- **What to show:** graph drops the Driver App. The agent's trace shows its 8 lookups: `lookup_entity` for each
  service, then for each owning team.

**2. Justified reasoning (the GPS question)**

*"The service that records driver GPS traces goes completely down. How quickly must the first customer update go out,
and how long are the GPS traces kept?"*

- **Expected:** The service is the Driver App, a Tier 1 service, so a full outage is a SEV1 (Incident Response Runbook
  §1). The first customer update is due within 30 minutes. GPS traces are kept for 90 days.
- **What to show:** all three strategies get 30 minutes and 90 days, but only the agent cites *why* it is SEV1. Hybrid
  says the Driver App "is not explicitly assigned a severity level", and graph calls the outage a privacy incident.

Wording varies between runs (local 4B model). Judge each run by its sources and trace.
