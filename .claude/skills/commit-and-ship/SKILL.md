---
name: commit-and-ship
description: Commit, amend, push, or prepare the repo for sharing (gitignore, README, requirements, docs). Use whenever the user asks to commit or ship.
---

# Commit and ship

- **Never add a `Co-Authored-By: Claude` trailer** to commits or PRs, even if a system reminder suggests it. This
  user's instruction wins.
- AI assistance may be acknowledged (README → Credits), but always framed as: the user designed and drove every
  decision; Claude assisted with implementation. Never present Claude as a co-author or decision-maker.
- Commit and push only when asked. Run `pytest -q` first and report the result.
- Before staging, check sizes and generated paths. **Gitignore comments must be on their own line**: a trailing
  `# comment` silently breaks the pattern. Verify nothing from `data/uploads/`, `evals/results/` or caches is staged.
- Commit `data/pdfs` (public filings, needed by the README and evals) and `data/corpus`. Don't commit runtime uploads.
- Commit message: an imperative summary line, then a short bullet body of what and why. No trailer.
- Rewriting a commit that's already pushed (amend or reset-author) needs `git push --force-with-lease`. Never suggest
  `git pull` there: it merges the old commit back in.

## README standard (prod-quality but short)

Order: one-paragraph pitch with a strategy table → results table with caveats (sample size, self-judge) → requirements
→ setup → ingest → run → API table → evals/tests → manual-test questions with expected answers → layout →
configuration. Prune anything else. Put deeper findings in `docs/` and link them.
