# QAgentic — Loop Engineering Progress Ledger

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

## v3 — Deeper cost cuts + automation features (in progress)

Measured per-run profile is the ruler: `python -m scripts.profile_run`.
Baseline before v3: 12,281 input / 2,883 tokens per scenario.

| # | Item | Status | Notes |
|---|------|--------|-------|
| C1 | Invert code generation: model plans, code renders | DONE | one call instead of two; codegen input 6,797 → 2,221 (−67%), total 12,281 → 7,646 (−38%), 1,993 tokens/scenario (−31%) |
| C2 | Merge the two codegen calls into one | DONE | folded into C1 |
| C3 | Prompt caching end to end | DONE | the hint now reaches the wire: `cache_control` on Anthropic (native + via OpenRouter), automatic elsewhere; standards moved into the design system prompt so there is a stable prefix to cache |
| C4 | Compress the test_design output schema | DONE | steps as strings, derived ids/tags/file names; output 1,333 → 1,025 while designing one *more* scenario (−37% per scenario) |
| C5 | Skip design entirely when memory already covers it | DONE | exact criterion traceability, not a similarity guess; a repeat run makes **0 design calls** |
| F1 | Auto-start the control plane from the extension | DONE | `ServerManager`: reuses a running server, only ever stops one it started, 4 new commands, 3 settings |
| F2 | API + DB tests as first-class outputs | DONE | endpoints derived from observed form actions; `tests/api/*.api.spec.ts` + `*.db.spec.ts`; an unobserved path is never called |
| F3 | Coverage-gap analysis | DONE | `services/knowledge_service/coverage.py` + `/api/projects/{id}/coverage` + `aiqa knowledge coverage` + a sidebar section where each gap is clickable into a run |
| F4 | Visual regression | DONE | `visual_renderer.py`; only routes whose DOM hash repeated across explorations are baselined; Playwright owns the pixel diff; opt-in via `standards.yaml` |
| F5 | Requirement ingestion at scale (epic → plan) | DONE | `jira.fetch_epic` + `services/agent_engine/batch.py`; plan and run are separate calls, priced from measured history, priority-ordered, with a batch-wide ceiling |
| F6 | Suite health / auto-quarantine | DONE | `services/execution_service/suite_health.py`; a test that never passes is `broken`, never a quarantine candidate |
| F7 | Autonomous exploratory testing | DONE | `services/execution_service/exploratory.py`; spec-free oracle limited to crashes, error pages, dead links and absent validation |

### Measured effect of C1-C5

`python -m scripts.profile_run`, offline deterministic provider:

| | Before C1 | After C5 |
|---|---|---|
| total tokens per run | 14,415 | **10,056** |
| scenarios produced | 5 | **6** |
| tokens per scenario | 2,883 | **1,676** (−42%) |

After F2 and F4 the same run costs 1,738 tokens/scenario (−40%) and produces six
files instead of four: the extra 62 tokens per scenario buy an API spec and a
database spec. Reported here rather than quoting the better mid-phase number.
| code_generation input | 6,797 (2 calls) | **2,323 (1 call)** |
| test_design output | 1,333 | **1,025** |

Repeat run of the same request (`design calls` is the headline):

| | run 1 (cold) | run 2 (warm) |
|---|---|---|
| LLM calls | 8 | **7** |
| input tokens | 8,044 | **6,644** |
| output tokens | 2,012 | **981** |
| design calls | 1 | **0** |

Offline mode proves request counts, token volume and reuse behaviour. It does
not prove dollars — that needs `--live` with real providers configured.

### Defects fixed during C1

Found by reading generated output, not by a failing test — each is now pinned in
`tests/unit/test_code_renderer.py`:

- Page Objects mixed locators from every route (login fields on a registration page).
- The same element captured on two routes rendered twice (`username` / `username2`).
- A `<select>` was driven with `fill()`; a checkbox was sent a string.
- `And` steps were registered as `Given`, turning assertions into preconditions.
- A step called a method with an argument it never declared — code that compiles
  and fails at runtime.
- `parameterise` counted `"<value>"` twice, declaring phantom parameters.
- A page's route was taken from the model's say-so rather than from its
  catalogue-verified locators.

## Verified (v2)

- `pytest tests -q` — 215 tests green
- `python -m scripts.selfcheck` — all checks passed
- `python -m scripts.benchmark --runs 5` — index cache 4/4, app map 4/4, 16 duplicate scenarios avoided
- `ruff check .` — clean · `npm run compile` — clean (34 commands / 12 views, manifest-consistent)

### Defects fixed during C3-C5

- Prompt caching was configured but no provider ever read the hint — the
  feature existed only in `models.yaml`.
- The offline provider's handlers were given a one-line summary of the prompt,
  so the design call never saw the acceptance criteria and could never cite
  them. This silently disabled C5 until an end-to-end run exposed it.
- The offline test plan cited "the operation is permission-controlled" for a
  *boundary validation* scenario, and left the permission criterion untested —
  a false coverage claim that would have made C5 skip work it should not have.
- Dropping tags from the design output left the critical path unmarked, because
  `_ensure_tags` only guaranteed `@regression`. Caught by the e2e suite.

### Defects fixed during F1-F3

- **Route coverage was a lie.** A remembered test recorded *every route
  exploration visited*, so one run of one feature reported 86% route coverage.
  A test now records the routes its page objects actually drive: the same run
  honestly reports 1 of 7.
- The naming detector concluded "lowercase" filenames from `login.feature` —
  a single word that is equally consistent with kebab-case. Once C4 made
  filenames derived rather than model-supplied, every generated feature file
  lost its hyphens. It now requires positive evidence.
- `TestKnowledge.apis` was handed structured check objects where it expected
  labels, crashing the next run's design prompt with a `TypeError`.
- API checks asserted `201` for `POST /login`, and posted the literal string
  `"<username>"`. Both produce a red test for a reason unrelated to the
  product, so an unstated status now asserts `response.ok()` and an unfilled
  body is `test.fixme` with the missing fields named.
- Nothing tied the VS Code manifest to the TypeScript, so a command could be
  declared and never registered. `tests/unit/test_extension_manifest.py` now
  checks commands, views and settings in both directions.

### Defects fixed during F4-F7

- A batch item that generated nothing reported plain `succeeded`, so a batch of
  ten could show ten green rows having produced two suites. It now reports
  `no_work`, and the summary reads "2 generated tests, 1 already covered".
- A feature whose scenarios were all dropped as duplicates still wrote a file
  containing nothing but `Feature: X`. An empty spec looks like coverage and
  runs nothing, so it is no longer written.
- The exploratory pass called `/residents/1/edit` a broken link because the
  application map stores it as the pattern `/residents/:id/edit`. Every
  id-bearing URL in any application would have been reported as dead.
- Six identical "missing security header" rows buried the one real finding;
  a site-wide observation is now reported once with the routes listed.

### What the exploratory pass found on the demo app

Run against `scripts/demo_app.py` with no requirement at all, it reported one
confirmed defect: the navigation links to `/logout`, which returns 404. That is
a genuine bug in the app under test, found with no spec and no LLM call.

## Verified (v3, so far)

- `pytest tests -q` — **354 tests green** (+139 this phase)
- `python -m scripts.selfcheck` — all checks passed
- `python -m scripts.profile_run` — 8 calls, 8,204 in / 2,221 out, **1,738 tokens/scenario**,
  6 files (feature, page object, steps, data, API spec, DB spec)
- `ruff check .` — clean
- repeat run of the same request — 0 design calls, 981 output tokens
- batch of 3 requirements — priority-ordered, 2 generated, 1 already covered
- exploratory pass on the demo app — 1 confirmed defect, 1 collapsed observation
- `npm run compile` + `vsce package` — clean, **43 commands**, 12 views, 41.6 KB,
  manifest checked against the TypeScript in both directions

## v4 — Running on free models

Configured against two OpenRouter keys, free models only.

| # | Item | Status | Notes |
|---|------|--------|-------|
| L1 | Qualify which free models actually work | DONE | `scripts/qualify_models.py` probes every free model with the two requests this platform makes; 6 of 17 passed both. Every model in the old `models.yaml` had stopped being free |
| L2 | Free-only enforcement | DONE | `free_only: true`; the router refuses any non-zero-price model. A cost ceiling of 0 *disables* the check, so it could not express this |
| L3 | Multi-key rotation | DONE | `packages/llm_provider/keyring.py`; burst vs daily vs invalid are distinguished |
| L4 | Per-model backoff | DONE | a busy model is parked, not the provider |
| L5 | Structured-output fallback | DONE | models that reject `response_format` are noted and retried without it |
| L6 | Truncation handling | DONE | a cut-off reply gets a bigger budget, not a lecture about JSON |
| L7 | VS Code deployment | DONE | installed, project registered, end-to-end run verified through the HTTP API |

### Defects the live run found that offline testing could not

- **A whole run silently used the offline stub.** One model's 429 disabled the
  entire provider, so every later tier entry was skipped. The run reported
  success having spoken to no model at all.
- **A burst 429 parked a key until midnight.** OpenRouter returns the same
  status for "slow down" and "you are out for the day"; treating them alike
  threw away a day's allowance over a few seconds of traffic.
- **The model-free fallback had the cross-route bug all over again.** Truncated
  model output dropped code generation to `deterministic_plan`, which had never
  been given the route-scoping fix — it emitted a registration page carrying the
  login form's fields, twice each. The test had hand-filtered the catalogue and
  so assumed the bug away.
- **`When I fill in valid resident details` called `submit()`.** The fallback
  mapped every `When` to submit, producing code that compiles, runs, and tests
  the wrong thing.
- **A crashed background run stayed `running` forever.** `execute` guards the
  orchestrator but not context setup; a fire-and-forget task's exception is
  never retrieved, so clients polled a dead run indefinitely.
- **Generated TypeScript that does not compile.** A plan may write
  `submitEmail()` for a method declared `submitEmail(email: string)`. Binding
  read only the planned call string, so it emitted the zero-argument call —
  inside a step that declared the argument and never passed it. Steps are now
  bound against the method's real signature.
- **No sign of progress during a run.** `current_agent` was written only when a
  run finished, so the status bar showed a nameless spinner for the whole run.
  That is tolerable at one minute and not at seven.
- **The extension would try to start the server from the wrong directory.** The
  workspace a QA engineer opens is their *test* repository, not this checkout,
  so the spawn failed several seconds later with a `ModuleNotFoundError` that
  explained nothing. It now checks first and names the setting to fix it.
- **18 scenarios for one form.** "Risk-based, not exhaustive" is easy for a
  model to ignore; the design overran its output limit twice and had to be
  retried. A stated budget (`max_scenarios_per_feature`) brought it to 5 in a
  single call.

### Code-generation defects only a live model exposed

All four produce TypeScript that looks right and is not. None could surface
offline, because the deterministic provider emits a fixed template that happens
to avoid every one of them.

- **A call that does not compile.** A plan writes `submitEmail()` for a method
  declared `submitEmail(email: string)`. Binding read the planned call string
  and not the signature, so it emitted the zero-argument call inside a step that
  declared the argument and never passed it.
- **A pattern with no matching parameter.** Models write Cucumber expressions
  directly — `with dateOfBirth {string}` — rather than a quoted value, so
  nothing was substituted, no parameter was declared, and Cucumber passed an
  argument to a callback that took none.
- **A page used before it was constructed.** A scenario can start on a `When`,
  so the setup step that instantiates the page may never run. The first step to
  touch it dereferenced an undeclared variable at runtime.
- **`When I fill in valid resident details` called `submit()`.** The model-free
  fallback mapped every `When` to submit — code that compiles, runs, and tests
  the wrong thing.

### Measured, live

- End-to-end through the HTTP API, exactly as the extension drives it:
  **succeeded, 5 files, 35,618 tokens, $0.000000**, every tier on a free model
- Progress visible throughout: requirement 8% → repository 18% → exploration
  30% → test_design 42% → code_generation 60% → 100%
- 5–12 minutes per run; free models take 60–170s per call
- `/logout` reported by the exploratory pass: a real 404 in the app under test
- 389 tests green, `ruff` clean, selfcheck passing, extension installed

## Next action

Every item on the v3 list (C1-C5, F1-F7) is complete and covered by tests.

Credentials are now configured and the platform has been verified live on free
models. Remaining work:

1. **Measure cost in dollars.** Every number above is from the offline
   deterministic provider, which proves request counts, token volume and reuse
   behaviour but not price. `python -m scripts.benchmark --runs 5 --live` with
   `OPENROUTER_API_KEY` set turns the −42% tokens/scenario figure into a real
   currency comparison against your 273-credits-per-test baseline.
2. **Verify prompt caching against a live provider.** The `cache_control`
   marker is now on the wire and `cached_tokens` is already discounted in the
   cost maths, but no run has yet come back with a cache hit to confirm it.
3. **Run the generated tests.** `npm install && npx playwright install` in a
   real project, so execution, failure analysis and self-healing exercise
   against a real browser rather than the HTTP fallback.
4. **Point it at a real Jira epic** to exercise `aiqa batch --epic`.
