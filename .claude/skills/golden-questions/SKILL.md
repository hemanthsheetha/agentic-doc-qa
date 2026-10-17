---
name: golden-questions
description: Write or extend golden eval questions and demo questions, including ones that show graph or agent beating hybrid. Use when adding to evals/golden*.jsonl or demos/.
---

# Golden and demo questions

## Every golden question

- **Verify every fact and evidence string against the stored chunks** before adding it (query `chunks` for the
  tenant). Never write expected answers from memory.
- Line format: `id`, `tags`, `question`, `filters`, `reference`, `facts` (a list of accepted spellings per fact),
  `evidence` (`[{doc_id, contains}]`, where `contains` is a string or a list of alternative wordings).
  Add `"expect_not_found": true` for questions that should be refused.
- Run `pytest tests/test_eval_metrics.py` (it validates every `golden*.jsonl`), then check that each evidence string
  matches some chunk.
- Include traps that test reading, not retrieval: look-alikes (vendor access 7 days vs employee 90 days; Route Planner
  vs Route Optimizer), and a refusal question.

## Questions that discriminate strategies

- **Name the starting point, not the hop's vocabulary.** "How long are Billing Service backups kept?" lets hybrid
  find Backup §3 on keywords. "Is a 20-day-old copy of the Billing Service data still available to restore?" does
  not; only the graph's Data Retention §2 → Backup §3 reference reaches it.
- **Graph wins** on one or two hops it can make from a good seed: an entity resolved to a person, or "see X §N".
- **The agent wins** on dependent lookups ("the second service to fail over"), on per-item aggregation ("every Tier 1
  service and its owner's lead", where graph runs out of slots), and on justified reasoning (*why* it's SEV1).
- On this small corpus, hybrid often gets lucky from one dense section (the Team Directory). Check retrieval sets,
  not just final answers.
- If the question is too vague to seed anything relevant, every strategy fails. Graph expansion needs a good start.

## Procedure

1. Draft 2–3 candidates. Run all strategies and compare **retrieved sections** and **answers** (not just pass/fail).
2. Keep the one where the difference is visible in the answer itself (e.g. hybrid says "the name is not provided").
3. Save demos to `demos/*.json`: question, expected answer, hop chain (section, fact, how it was reached), talking
   points, and the captured run. Document notable findings in `docs/` and link them from the README.
