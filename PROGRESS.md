# AI QA Engineer — Loop Engineering Progress Ledger

> **Single source of truth for resuming work.** Read this first, find the first phase that is not
> `DONE`, continue from its note. Protocol per phase: BUILD → TEST → VERIFY → update this file.
> Never break a phase already marked DONE.

- **Repo root:** `C:\Users\Pavan.Kolla\Desktop\QA_AI_Agents`
- **Verification:** `python -m pytest tests -q` · `python -m scripts.selfcheck` · `python -m ruff check .`
  · `cd apps/vscode-extension && npm run compile`

---

## v1 — Foundation (COMPLETE)

All 16 phases done. 160 tests green. Delivered: monorepo, domain types, security guards, LLM
provider layer, observability + cost tracking, 31-tool execution layer, knowledge indexing,
10 agents, resumable orchestrator, FastAPI control plane, VS Code extension, web dashboard,
Docker/CI, docs.

---

## v2 — Cost-optimized architecture

Primary objective: **minimize LLM usage and cost without sacrificing automation quality.**
Paid models for high-value reasoning, free/local models for low-risk work, and *no LLM at all*
for anything deterministic. Do not re-ask an LLM for information the platform already knows.

| # | Phase | Status | Notes |
|---|-------|--------|-------|
| V1 | Model tiers, budgets, request accounting | DONE | 4 tiers, task-complexity routing, run budgets (requests/tokens/cost/retries), OpenRouter daily free-request ledger, fallback chains, prompt caching |
| V2 | Repository Map + incremental indexing | DONE | `repository_map.json`, git-hash change detection, per-file hashing, only re-index what changed |
| V3 | Application Map + component registry | DONE | persistent pages/components/locators with confidence + last_verified, staleness re-explore policy |
| V4 | Test Knowledge Store + QA Knowledge Graph | DONE | reuse prior implementations, requirement→feature→page→component→api→db→test edges |
| V5 | Standards system (`.aiqa/`) | DONE | config.yaml + standards/*.md + examples/, company→project→module priority, example-based learning, free-text → structured |
| V6 | Static-first validation pipeline | DONE | tsc + ESLint + Gherkin parser + AST rules; semantic LLM validation only when static passes and risk remains |
| V7 | Batch test design + reuse-first flow | DONE | one planning call for N scenarios; existing-artifact discovery before generation |
| V8 | LangGraph orchestrator | DONE | real LangGraph StateGraph, declared topology, compile-time validation, draw_mermaid(). **No checkpointer** — state holds a live AgentContext and durability is the `runs` row. Built-in orchestrator kept, with an equivalence test |
| V9 | Agent permissions | DONE | explicit per-agent capability grants; no unrestricted access |
| V10 | Cost + management dashboards, success metrics | DONE | cost/scenario, savings vs baseline, coverage, healing accuracy, intervention rate |
| V11 | VS Code sidebar expansion | DONE | 12 sidebar sections, 13 new commands |
| V12 | Docs + roadmap | DONE | cost-optimization.md, ROADMAP.md, README rewritten for v2 |

## Cost baseline

User-provided benchmark: **~30,000 AI credits for 110 test cases** (≈273 credits/test).
Targets: −50% initially, then −70…90%. `scripts/benchmark.py` measures against this; do not claim
savings that have not been measured.

## Verified (v2)

- `pytest tests -q` — 215 tests green
- `python -m scripts.selfcheck` — all checks passed
- `python -m scripts.benchmark --runs 5` — index cache 4/4, app map 4/4, 16 duplicate scenarios avoided
- `ruff check .` — clean · `npm run compile` — clean (34 commands / 12 views, manifest-consistent)

## Next action

v2 complete. `docs/ROADMAP.md` sequences the next increments; the highest
value-per-effort items are run-from-CI, coverage-gap analysis, and first-class
API/DB test generation — none of which need new architecture.
