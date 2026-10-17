---
name: eval-and-compare
description: Run the eval harness and compare retrieval strategies (hybrid / graph / agent) after any change to chunking, retrieval, graph, agent or prompts. Use before claiming an improvement.
---

# Evaluate and compare strategies

1. **Pick the set and tenant.** Use `evals/golden.jsonl` on tenant `default` (single-hop SEC lookups), or
   `evals/golden_ops.jsonl` on `ops` (multi-hop policies).
2. **Run each strategy you touched**, retrieval-only first because it's fast:
   ```bash
   python -m evals.run --golden evals/golden_ops.jsonl --tenant ops --strategy graph --retrieval-only
   python -m evals.run --golden evals/golden_ops.jsonl --tenant ops --strategy graph          # + answers + judge
   ```
   For a baseline that isn't helped by metadata, add `--no-filters`. `agent` scores retrieval on the sources it
   observed (`recall@observed`).
3. **Report a small table** (recall@8, key facts, judge, latency) for each strategy against the previous run. Include
   regressions.
4. **When a number moves, read the per-question rows and failures** before explaining it. The runner prints answer
   and judge reason for every failure.

## How to read results

- With 10 questions, ±0.05 is noise. One question moves an average by 0.1.
- **Key-fact match is the primary signal.** The judge is the same local 4B model: it has contradicted itself
  between runs, and it used to penalize correct extra detail (now fixed in its prompt).
- Before blaming retrieval, split each failure into **retrieval miss** (the evidence isn't in context; look at
  `retrieved`) or **generation slip** (the evidence was there and the 4B model misread it). Fix the right layer.
- "Ungrounded" can mean the evidence is too strict: filings state one figure several ways ("$3.2 billion" vs
  "3,208,323"). Add alternatives to `contains`, don't change the system.
- **Stop tuning when gains come from fitting specific golden questions.** Say so explicitly.

## Lessons that shaped the current design

- Ranking graph expansions by *their own* similarity to the question kills multi-hop: bridge sections (directories,
  definitions) don't resemble the question. Expansions inherit the score of the section that points to them.
- Second hops through entity mentions reach hub sections (noise); only explicit references chain to hop 2.
- The agent's answer must be written from the full text of what it gathered, not from loop snippets. The fact is
  often just past the cut.
